import fnmatch
import inspect
import logging
import weakref

from django.core.exceptions import (
    ImproperlyConfigured,
    ObjectDoesNotExist,
    PermissionDenied,
    ValidationError,
)
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.http import Http404
from django.urls import path
from django.utils.module_loading import import_string

from . import conf, results
from .context import CallContext
from .decorators import get_spec, prompt, resource, tool
from .errors import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    RESOURCE_NOT_FOUND,
    AlreadyRegistered,
    AuthenticationFailed,
    InvalidArguments,
    NotRegistered,
    RpcError,
    ToolError,
)
from .items import Item
from .providers import Provider
from .schema import coerce_strings

logger = logging.getLogger("mcpkit")

# Newest first. A new spec version has to be reviewed and added by hand.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
KINDS = ("tool", "resource", "prompt")
MAX_COMPLETIONS = 100

_servers = weakref.WeakSet()


@receiver(setting_changed)
def _settings_changed(setting, **kwargs):
    if setting == "MCPKIT":
        for server in list(_servers):
            server.clear_cache()


def _is_provider_class(value):
    return isinstance(value, type) and issubclass(value, Provider)


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
        allow_anonymous=False,
        exclude=(),
        exclude_apps=(),
    ):
        self._name = name
        self._version = version
        self.title = title
        self.instructions = instructions
        self.authentication = authentication
        self.allow_anonymous = allow_anonymous
        self.exclude = list(exclude)
        self.exclude_apps = list(exclude_apps)
        self._registry = {}
        self._functions = []
        self._removed = set()
        self._error_handlers = []
        self._catalog = None
        self._view = None
        _servers.add(self)

    def __repr__(self):
        return f"<{type(self).__name__} {self.name!r}>"

    @property
    def name(self):
        return self._name or conf.get("NAME")

    @property
    def version(self):
        return self._version or conf.get("VERSION")

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

    def build_catalog(self):
        items = [
            item
            for provider in self._registry.values()
            for item in provider.get_items()
        ]
        items += [Item(spec) for spec in self._functions]
        exclude = [*conf.get("EXCLUDE"), *self.exclude]
        exclude_apps = {*conf.get("EXCLUDE_APPS"), *self.exclude_apps}
        catalog = {kind: {} for kind in KINDS}
        for item in items:
            identifier = item.identifier
            if (item.kind, identifier) in self._removed:
                continue
            if item.app_label in exclude_apps:
                continue
            if any(fnmatch.fnmatchcase(identifier, pattern) for pattern in exclude):
                continue
            bucket = catalog[item.kind]
            if identifier in bucket:
                raise ImproperlyConfigured(
                    f"Duplicate MCP {item.kind} {identifier!r} on {self!r}: "
                    f"{bucket[identifier].source} and {item.source}."
                )
            bucket[identifier] = item
        return catalog

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
        return [path("", self.get_view(), name="endpoint")], "mcpkit", self.name

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
        """The user for this request, or None when it is not authenticated."""
        for backend in self.get_authentication_backends():
            try:
                user = backend(request)
            except AuthenticationFailed:
                return None
            if user is not None:
                return user
        return None

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

    def describe_tool(self, item):
        spec = item.spec
        entry = {
            "name": item.name,
            "description": spec.description,
            "inputSchema": spec.input_schema,
        }
        if spec.title:
            entry["title"] = spec.title
        if spec.output_schema is not None:
            entry["outputSchema"] = spec.output_schema
        hints = {key: value for key, value in spec.hints.items() if value is not None}
        if hints:
            entry["annotations"] = hints
        return entry

    def list_tools(self, ctx, params):
        return {
            "tools": [self.describe_tool(item) for item in self.get_items("tool", ctx)]
        }

    def call_tool(self, ctx, params):
        name = params.get("name")
        item = self.get_item("tool", name, ctx)
        if item is None:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        try:
            kwargs = item.spec.parse_arguments(params.get("arguments"))
            value = item.call(ctx, kwargs)
        except Exception as error:
            result = self.handle_tool_error(ctx, item, error)
            logger.info("tool=%s user=%s result=error", name, _user_pk(ctx))
            return result
        logger.info("tool=%s user=%s result=ok", name, _user_pk(ctx))
        return results.tool_result(value)

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
        items = self.get_items("resource", ctx)
        return {
            "resources": [
                self.describe_resource(item)
                for item in items
                if not item.spec.is_template
            ]
        }

    def list_resource_templates(self, ctx, params):
        items = self.get_items("resource", ctx)
        return {
            "resourceTemplates": [
                self.describe_resource(item) for item in items if item.spec.is_template
            ]
        }

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
        return {
            "prompts": [
                self.describe_prompt(item) for item in self.get_items("prompt", ctx)
            ]
        }

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


def _user_pk(ctx):
    return getattr(ctx.user, "pk", None)


class DefaultMCPServer(MCPServer):
    pass


mcp = DefaultMCPServer()
