import base64
import fnmatch
import inspect
import logging
import re
import weakref
from contextlib import nullcontext
from pathlib import Path

from django.apps import apps

from django.core.exceptions import (
    ImproperlyConfigured,
    ObjectDoesNotExist,
    PermissionDenied,
    ValidationError,
)
from django.core.signals import setting_changed
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.dispatch import receiver
from django.http import Http404
from django.urls import path
from django.utils.module_loading import import_string

from . import conf
from .core import results
from .core.context import CallContext
from .core.decorators import get_spec, prompt, resource, tool
from .core.items import Item
from .core.schema import coerce_strings
from .errors import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    RESOURCE_NOT_FOUND,
    AlreadyRegistered,
    InvalidArguments,
    InvalidName,
    NotRegistered,
    RpcError,
    ToolError,
)
from .integrations import drf
from .permissions import allows, permission_from_setting, validate_permission
from .providers.base import Provider

logger = logging.getLogger("xprez_mcp")

# Newest first. A new spec version has to be reviewed and added by hand.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
KINDS = ("tool", "resource", "prompt")
MAX_COMPLETIONS = 100
# What the Claude and OpenAI APIs accept; stricter than the MCP spec.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LONG_NAME = 40
OAUTH_APP = "xprez_mcp.contrib.oauth"
UPLOADS_APP = "xprez_mcp.contrib.uploads"

_servers = weakref.WeakSet()


class MCPServer:
    """A registry of providers and functions, served at ``server.urls``."""

    context_class = CallContext
    protocol_versions = PROTOCOL_VERSIONS

    def __init__(
        self,
        name=None,
        *,
        version=None,
        title=None,
        instructions=None,
        authentication=None,
        permission=None,
        allow_anonymous=False,
        allowed_origins=None,
        exclude=(),
        exclude_apps=(),
        exposure="tools",
        allow_writes=(),
        list_page_size=None,
        related_labels=None,
        atomic_writes=None,
    ):
        if exposure not in ("tools", "generic"):
            raise ValueError("exposure must be 'tools' or 'generic'.")
        if not (atomic_writes is None or isinstance(atomic_writes, (bool, str))):
            raise ValueError("atomic_writes must be True, False or a database alias.")
        self._name = name
        self._version = version
        self.title = title
        self.instructions = instructions
        self.authentication = authentication
        self._permission = permission
        self.allow_anonymous = allow_anonymous
        self._allowed_origins = allowed_origins
        self.exposure = exposure
        self.exclude = list(exclude)
        self.exclude_apps = list(exclude_apps)
        self.allow_writes = list(allow_writes)
        self._list_page_size = list_page_size
        self._related_labels = related_labels
        self._atomic_writes = atomic_writes
        self._registry = {}
        self._admin_sites = []
        self._trusted_providers = set()
        self._withheld_writes = []
        self._functions = []
        self._removed = set()
        self._error_handlers = []
        self._catalog = None
        self._view = None
        self.mounted = False
        _servers.add(self)

    def __repr__(self):
        return f"<{type(self).__name__} {self.name!r}>"

    @property
    def name(self):
        return self._name or conf.get("NAME")

    @property
    def version(self):
        return self._version or conf.get("VERSION")

    @property
    def permission(self):
        """Who may use the server: ``f(ctx) -> bool``, a bool, or None (nobody).

        ``MCPServer(permission=...)``, else ``XPREZ_MCP["PERMISSION"]`` (which
        may be a dotted path); also ``True`` (everyone) or ``False`` (nobody,
        on purpose).
        """
        if self._permission is None:
            return permission_from_setting(conf.get("PERMISSION"))
        return validate_permission(self._permission, "server permission")

    @property
    def list_page_size(self):
        """Page size of ``*/list`` methods; None or 0 sends everything at once."""
        if self._list_page_size is not None:
            return self._list_page_size
        return conf.get("LIST_PAGE_SIZE")

    @property
    def related_labels(self):
        """Related records as ``{"id", "display"}`` instead of a bare id."""
        if self._related_labels is not None:
            return self._related_labels
        return conf.get("RELATED_LABELS")

    @property
    def atomic_writes(self):
        """Run tools that are not read_only in a transaction: True (the default
        database), a database alias, or False."""
        if self._atomic_writes is not None:
            return self._atomic_writes
        return conf.get("ATOMIC_WRITES")

    @property
    def allowed_origins(self):
        if self._allowed_origins is not None:
            return self._allowed_origins
        return conf.get("ALLOWED_ORIGINS")

    # --- registration -------------------------------------------------------

    def register(self, target=None, provider_class=None):
        """``register(ProviderClass)``, ``register(Model, ProviderClass)``,
        ``@register`` on a provider class or ``@register(Model)``."""
        if target is None:
            return lambda cls: self.register(cls)
        if _is_provider_class(target):
            if provider_class is not None:
                raise TypeError("register(ProviderClass) takes no provider_class.")
            self._add(target, target(self))
            return target
        if provider_class is None:

            def decorator(cls):
                self.register(target, cls)
                return cls

            return decorator
        if not _is_provider_class(provider_class):
            raise TypeError(f"{provider_class!r} is not a Provider subclass.")
        self._add(target, provider_class(self, model=target))
        return provider_class

    def _add(self, key, provider):
        if key in self._registry:
            raise AlreadyRegistered(f"{key!r} is already registered on {self!r}.")
        self._registry[key] = provider
        self.clear_cache()

    def unregister(self, key):
        """Remove what was registered under a provider class or a model."""
        if key not in self._registry:
            raise NotRegistered(f"{key!r} is not registered on {self!r}.")
        del self._registry[key]
        self.clear_cache()

    def register_admin(
        self,
        site=None,
        *,
        exclude=(),
        actions=("list", "get"),
        model_actions=None,
        exposure=None,
        provider_class=None,
        name_template=None,
    ):
        """Expose every model of an ``AdminSite`` through an ``AdminProvider``.

        Providers are built with the catalog, so the site's registry is complete
        whatever the import order. Models registered on the server win."""
        if site is None:
            from django.contrib.admin import site
        self._admin_sites.append(
            {
                "site": site,
                "exclude": list(exclude),
                "actions": list(actions),
                "model_actions": dict(model_actions or {}),
                "exposure": exposure,
                "provider_class": provider_class,
                "name_template": name_template,
            }
        )
        self.clear_cache()

    def is_registered(self, key):
        return key in self._registry

    def get_provider(self, key):
        return self._registry[key]

    def tool(self, func=None, **options):
        return self._function(tool, func, **options)

    def prompt(self, func=None, **options):
        return self._function(prompt, func, **options)

    def resource(self, uri, **options):
        def decorator(func):
            resource(uri, **options)(func)
            self._functions.append(get_spec(func))
            self.clear_cache()
            return func

        return decorator

    def _function(self, decorator, func, **options):
        def register(func):
            decorator(**options)(func)
            self._functions.append(get_spec(func))
            self.clear_cache()
            return func

        return register(func) if func is not None else register

    def unregister_tool(self, name):
        self._remove("tool", name)

    def unregister_resource(self, uri):
        self._remove("resource", uri)

    def unregister_prompt(self, name):
        self._remove("prompt", name)

    def _remove(self, kind, identifier):
        self._removed.add((kind, identifier))
        self.clear_cache()

    def error_handler(self, exception_class):
        """Map an exception raised by a tool to a result: ``f(ctx, exc) -> value``."""

        def decorator(func):
            self._error_handlers.append((exception_class, func))
            return func

        return decorator

    # --- catalog ------------------------------------------------------------

    def clear_cache(self):
        self._catalog = None

    @property
    def catalog(self):
        if self._catalog is None:
            self._catalog = self.build_catalog()
        return self._catalog

    def get_providers(self):
        """Registered providers, then those built by ``register_admin``."""
        providers = list(self._registry.values())
        if self._admin_sites:
            from .providers.admin.provider import build_admin_providers

            providers += build_admin_providers(self, self._admin_sites)
        return providers

    def build_catalog(self):
        from .providers.model import ModelProvider

        self._trusted_providers = set()
        providers = self.get_providers()
        items = [item for provider in providers for item in provider.get_items()]
        items += [Item(spec) for spec in self._functions]
        generic = [
            provider
            for provider in providers
            if isinstance(provider, ModelProvider)
            and provider.get_exposure() == "generic"
        ]
        if generic:
            from .providers.generic import build_items

            items += build_items(self, generic)
        exclude = [*conf.get("EXCLUDE"), *self.exclude]
        exclude_apps = {*conf.get("EXCLUDE_APPS"), *self.exclude_apps}
        catalog = {kind: {} for kind in KINDS}
        self._withheld_writes = []
        invalid = []
        for item in items:
            identifier = item.identifier
            if (item.kind, identifier) in self._removed:
                continue
            if item.app_label in exclude_apps:
                continue
            if any(fnmatch.fnmatchcase(identifier, pattern) for pattern in exclude):
                continue
            if item.kind == "tool" and not item.spec.hints["readOnlyHint"]:
                if item.provider is not None:
                    allowed = self.provider_writes_allowed(item.provider)
                else:
                    allowed = self.module_writes_allowed(item.module)
                if not allowed:
                    self._withheld_writes.append(item)
                    continue
            if item.kind != "resource" and not NAME_PATTERN.match(identifier):
                invalid.append(f"{identifier!r} ({item.source})")
            bucket = catalog[item.kind]
            if identifier in bucket:
                raise ImproperlyConfigured(
                    f"Duplicate MCP {item.kind} {identifier!r} on {self!r}: "
                    f"{bucket[identifier].source} and {item.source}."
                )
            bucket[identifier] = item
        if invalid:
            raise InvalidName(
                f"Invalid MCP names on {self!r}: {', '.join(invalid)}. Tool and "
                f"prompt names must match {NAME_PATTERN.pattern}."
            )
        return catalog

    @property
    def withheld_writes(self):
        """Write tools of third-party apps left out by ``ALLOW_WRITES``."""
        self.catalog
        return list(self._withheld_writes)

    # --- third-party writes -------------------------------------------------

    def is_third_party(self, app_config):
        """An app installed as a package, not part of the project."""
        if app_config.name == "xprez_mcp":
            return False
        parts = Path(app_config.path).parts
        return "site-packages" in parts or "dist-packages" in parts

    def module_writes_allowed(self, module):
        """Whether write tools defined in ``module`` are exposed."""
        config = apps.get_containing_app_config(module) if module else None
        if config is None or not self.is_third_party(config):
            return True
        patterns = [*conf.get("ALLOW_WRITES"), *self.allow_writes]
        return any(fnmatch.fnmatchcase(config.label, pattern) for pattern in patterns)

    def provider_writes_allowed(self, provider):
        """Whether write tools of ``provider`` are exposed."""
        if provider in self._trusted_providers:
            return True
        else:
            return self.module_writes_allowed(type(provider).__module__)

    def trust_writes(self, provider):
        """Providers the server built itself (``register_admin``) count as the
        project's: their writes are opt-in through ``actions`` already."""
        self._trusted_providers.add(provider)

    def get_items(self, kind, ctx):
        return [item for item in self.catalog[kind].values() if item.is_allowed(ctx)]

    def get_item(self, kind, identifier, ctx):
        item = self.catalog[kind].get(identifier)
        if item is not None and item.is_allowed(ctx):
            return item
        return None

    # --- request handling ---------------------------------------------------

    @property
    def urls(self):
        self.mounted = True
        return self.get_urls(), "xprez_mcp", self.name

    def get_urls(self):
        urls = [path("", self.get_view(), name="endpoint")]
        if apps.is_installed(OAUTH_APP):
            from .contrib.oauth.urls import server_urls

            urls += server_urls(self)
        if apps.is_installed(UPLOADS_APP):
            from .contrib.uploads.urls import server_urls

            urls += server_urls(self)
        return urls

    def get_view(self):
        if self._view is None:
            from .views import MCPView

            self._view = MCPView.as_view(server=self)
        return self._view

    def get_authentication_backends(self):
        backends = self.authentication
        if backends is None:
            backends = conf.get("AUTHENTICATION")
        return [
            import_string(backend) if isinstance(backend, str) else backend
            for backend in backends
        ]

    def authenticate(self, request):
        """The user for this request, or None when it is not authenticated.
        Raises ``AuthenticationFailed`` when a backend rejects the request."""
        for backend in self.get_authentication_backends():
            user = backend(request)
            if user is not None:
                return user
        return None

    def has_access(self, ctx):
        """Who may use the server, checked before any method; without a
        permission, nobody gets in."""
        permission = self.permission
        return permission is not None and allows(permission, ctx)

    def get_context(self, request, user, protocol_version=None, meta=None):
        return self.context_class(
            server=self,
            request=request,
            user=user,
            protocol_version=protocol_version,
            meta=meta or {},
        )

    def get_methods(self):
        return {
            "initialize": self.initialize,
            "ping": lambda ctx, params: {},
            "tools/list": self.list_tools,
            "tools/call": self.call_tool,
            "resources/list": self.list_resources,
            "resources/templates/list": self.list_resource_templates,
            "resources/read": self.read_resource,
            "prompts/list": self.list_prompts,
            "prompts/get": self.get_prompt,
            "completion/complete": self.complete,
        }

    def dispatch(self, ctx, method, params):
        handler = self.get_methods().get(method)
        if handler is None:
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        return handler(ctx, params)

    # --- methods ------------------------------------------------------------

    def get_instructions(self, ctx):
        if callable(self.instructions):
            return self.instructions(ctx)
        return self.instructions

    def initialize(self, ctx, params):
        requested = params.get("protocolVersion")
        if requested in self.protocol_versions:
            version = requested
        else:
            version = self.protocol_versions[0]
        info = {"name": self.name, "version": self.version}
        if self.title:
            info["title"] = self.title
        result = {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
                "completions": {},
            },
            "serverInfo": info,
        }
        instructions = self.get_instructions(ctx)
        if instructions:
            result["instructions"] = instructions
        return result

    def describe_tool(self, item, ctx=None):
        spec = item.spec
        entry = {
            "name": item.name,
            "description": spec.description,
            "inputSchema": spec.get_input_schema(ctx),
        }
        if spec.title:
            entry["title"] = spec.title
        if spec.output_schema is not None:
            entry["outputSchema"] = spec.output_schema
        hints = {key: value for key, value in spec.hints.items() if value is not None}
        if hints:
            entry["annotations"] = hints
        return entry

    def paginate(self, items, params):
        """``(page, next_cursor)``: keyset paging by identifier when enabled."""
        size = self.list_page_size
        if not size:
            return items, None
        items = sorted(items, key=lambda item: item.identifier)
        cursor = params.get("cursor")
        if cursor is not None:
            after = _decode_list_cursor(cursor)
            items = [item for item in items if item.identifier > after]
        if len(items) <= size:
            return items, None
        page = items[:size]
        return page, _encode_list_cursor(page[-1].identifier)

    def list_result(self, key, entries, next_cursor):
        result = {key: entries}
        if next_cursor:
            result["nextCursor"] = next_cursor
        return result

    def list_tools(self, ctx, params):
        page, cursor = self.paginate(self.get_items("tool", ctx), params)
        tools = [self.describe_tool(item, ctx) for item in page]
        return self.list_result("tools", tools, cursor)

    def call_tool(self, ctx, params):
        name = params.get("name")
        item = self.get_item("tool", name, ctx)
        if item is None:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        using = self.get_tool_database(item)
        ctx.tool_database = using
        try:
            with transaction.atomic(using=using) if using else nullcontext():
                kwargs = item.spec.parse_arguments(params.get("arguments"))
                kwargs = item.resolve_lookups(ctx, kwargs)
                kwargs = item.resolve_files(ctx, kwargs)
                value = item.call(ctx, kwargs)
                failed = isinstance(value, results.ToolResult) and value.is_error
                # inside the transaction: a hook that raises rolls the call back
                ctx.end_call(not failed)
                if using and failed:
                    transaction.set_rollback(True, using=using)
        except Exception as error:
            ctx.end_call(False)
            result = self.handle_tool_error(ctx, item, error)
            logger.info("tool=%s user=%s result=error", name, _user_pk(ctx))
            return result
        logger.info("tool=%s user=%s result=ok", name, _user_pk(ctx))
        return results.tool_result(value)

    def get_tool_database(self, item):
        """The alias of the database a call of ``item`` runs in a transaction
        on, or None: ``@tool(atomic=...)``, else ``atomic_writes`` for tools
        that are not read_only."""
        atomic = item.spec.atomic
        if atomic is None:
            atomic = not item.spec.hints["readOnlyHint"] and self.atomic_writes
        if not atomic:
            return None
        using = DEFAULT_DB_ALIAS if atomic is True else atomic
        if not isinstance(using, str) or using not in connections.settings:
            raise ImproperlyConfigured(
                f"Tool {item.name}: atomic writes need True, False or a database "
                f"alias, not {atomic!r}."
            )
        return using

    def handle_tool_error(self, ctx, item, error):
        for exception_class, handler in self._error_handlers:
            if isinstance(error, exception_class):
                return results.tool_result(handler(ctx, error), is_error=True)
        if isinstance(error, ToolError):
            value = error.message
            if error.data is not None:
                value = {"error": error.message, **error.data}
            return results.tool_result(value, is_error=True)
        if isinstance(error, PermissionDenied):
            return results.tool_result(
                str(error) or "Permission denied.", is_error=True
            )
        if isinstance(error, (ObjectDoesNotExist, Http404)):
            return results.tool_result(str(error) or "Not found.", is_error=True)
        if isinstance(error, ValidationError):
            return results.tool_result(" ".join(error.messages), is_error=True)
        value = drf.error_value(error)
        if value is not None:
            return results.tool_result(value, is_error=True)
        logger.exception("Tool %s failed", item.name)
        return results.tool_result("Internal error.", is_error=True)

    def describe_resource(self, item):
        spec = item.spec
        key = "uriTemplate" if spec.is_template else "uri"
        entry = {key: spec.uri, "name": item.name}
        if spec.title:
            entry["title"] = spec.title
        if spec.description:
            entry["description"] = spec.description
        if spec.mime_type:
            entry["mimeType"] = spec.mime_type
        return entry

    def list_resources(self, ctx, params):
        items = [
            item
            for item in self.get_items("resource", ctx)
            if not item.spec.is_template
        ]
        page, cursor = self.paginate(items, params)
        resources = [self.describe_resource(item) for item in page]
        return self.list_result("resources", resources, cursor)

    def list_resource_templates(self, ctx, params):
        items = [
            item for item in self.get_items("resource", ctx) if item.spec.is_template
        ]
        page, cursor = self.paginate(items, params)
        templates = [self.describe_resource(item) for item in page]
        return self.list_result("resourceTemplates", templates, cursor)

    def resolve_resource(self, ctx, uri):
        items = self.get_items("resource", ctx)
        for item in items:
            if not item.spec.is_template and item.spec.uri == uri:
                return item, {}
        for item in items:
            if item.spec.is_template:
                values = item.spec.match(uri)
                if values is not None:
                    return item, values
        return None, None

    def read_resource(self, ctx, params):
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise RpcError(INVALID_PARAMS, "Missing uri.")
        item, values = self.resolve_resource(ctx, uri)
        if item is None:
            raise RpcError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})
        try:
            kwargs = coerce_strings(item.spec.func, values)
            value = item.call(ctx, kwargs)
        except (InvalidArguments, ObjectDoesNotExist, Http404, PermissionDenied):
            raise RpcError(
                RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri}
            ) from None
        return {
            "contents": [results.resource_contents(uri, value, item.spec.mime_type)]
        }

    def describe_prompt(self, item):
        spec = item.spec
        entry = {"name": item.name, "arguments": spec.arguments}
        if spec.title:
            entry["title"] = spec.title
        if spec.description:
            entry["description"] = spec.description
        return entry

    def list_prompts(self, ctx, params):
        page, cursor = self.paginate(self.get_items("prompt", ctx), params)
        prompts = [self.describe_prompt(item) for item in page]
        return self.list_result("prompts", prompts, cursor)

    def get_prompt(self, ctx, params):
        name = params.get("name")
        item = self.get_item("prompt", name, ctx)
        if item is None:
            raise RpcError(INVALID_PARAMS, f"Unknown prompt: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "Arguments must be an object.")
        known = {argument["name"]: argument for argument in item.spec.arguments}
        for key in arguments:
            if key not in known:
                raise RpcError(INVALID_PARAMS, f"Unknown argument: {key}.")
        for argument in known.values():
            if argument["required"] and arguments.get(argument["name"]) in (None, ""):
                raise RpcError(
                    INVALID_PARAMS, f"Missing required argument: {argument['name']}."
                )
        try:
            kwargs = coerce_strings(item.spec.func, arguments)
        except InvalidArguments as error:
            raise RpcError(INVALID_PARAMS, error.message) from None
        result = {"messages": results.prompt_messages(item.call(ctx, kwargs))}
        if item.spec.description:
            result["description"] = item.spec.description
        return result

    def complete(self, ctx, params):
        ref = params.get("ref") or {}
        argument = params.get("argument") or {}
        if ref.get("type") == "ref/prompt":
            item = self.get_item("prompt", ref.get("name"), ctx)
        elif ref.get("type") == "ref/resource":
            item, _ = self.find_template(ctx, ref.get("uri"))
        else:
            raise RpcError(INVALID_PARAMS, "Unknown reference type.")
        if item is None:
            raise RpcError(INVALID_PARAMS, "Unknown reference.")
        func = item.spec.completions.get(argument.get("name"))
        values = []
        if func is not None:
            options = {}
            if "arguments" in inspect.signature(func).parameters:
                options["arguments"] = (params.get("context") or {}).get(
                    "arguments", {}
                )
            values = [
                str(value)
                for value in item.invoke(
                    func, ctx, argument.get("value", ""), **options
                )
            ]
        return {
            "completion": {
                "values": values[:MAX_COMPLETIONS],
                "total": len(values),
                "hasMore": len(values) > MAX_COMPLETIONS,
            }
        }

    def find_template(self, ctx, uri_template):
        for item in self.get_items("resource", ctx):
            if item.spec.uri == uri_template:
                return item, {}
        return None, None


mcp = MCPServer()


# --- helpers -----------------------------------------------------------------


@receiver(setting_changed)
def _settings_changed(setting, **kwargs):
    if setting == "XPREZ_MCP":
        for server in list(_servers):
            server.clear_cache()


def _is_provider_class(value):
    return isinstance(value, type) and issubclass(value, Provider)


def _encode_list_cursor(identifier):
    return base64.urlsafe_b64encode(identifier.encode()).decode().rstrip("=")


def _decode_list_cursor(cursor):
    if not isinstance(cursor, str):
        raise RpcError(INVALID_PARAMS, "Invalid cursor.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return base64.b64decode(padded, altchars=b"-_", validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        raise RpcError(INVALID_PARAMS, "Invalid cursor.") from None


def _user_pk(ctx):
    return getattr(ctx.user, "pk", None)
