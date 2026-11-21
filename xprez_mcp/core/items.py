"""Tool, resource and prompt definitions (specs) and their registered form (items)."""

import inspect
import re
from functools import cached_property

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured

from . import schema
from ..permissions import check_permission

URI_PARAMETER = re.compile(r"\{(\w+)\}")


class Spec:
    kind = None

    def __init__(
        self, func, *, name=None, title=None, description=None, permission=None
    ):
        if inspect.iscoroutinefunction(func):
            raise ImproperlyConfigured(
                f"{func.__qualname__}: async functions are not supported yet."
            )
        schema.wire_parameters(func)
        self.func = func
        self.name = name or func.__name__
        self.title = title
        if description is None:
            description = inspect.getdoc(func) or ""
        self.description = inspect.cleandoc(description)
        self.permission = permission


class CompletionMixin:
    def complete(self, argument):
        """Decorator registering argument autocomplete: ``f(ctx, value) -> values``."""

        def decorator(func):
            self.completions[argument] = func
            return func

        return decorator


class ToolSpec(Spec):
    kind = "tool"

    def __init__(
        self,
        func,
        *,
        input_schema=None,
        output_schema=None,
        read_only=None,
        destructive=None,
        idempotent=None,
        open_world=None,
        **options,
    ):
        super().__init__(func, **options)
        self.raw_input_schema = input_schema
        self.output_schema = output_schema
        self.hints = {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": open_world,
        }

    @cached_property
    def input_schema(self):
        return self.raw_input_schema or schema.signature_schema(self.func)

    def get_input_schema(self, ctx):
        """The schema shown to ``ctx``'s user; generated tools may vary it."""
        return self.input_schema

    def parse_arguments(self, arguments):
        if self.raw_input_schema is not None:
            schema.validate_json(self.raw_input_schema, arguments or {})
            return dict(arguments or {})
        return schema.coerce_arguments(self.func, arguments)


class ResourceSpec(CompletionMixin, Spec):
    kind = "resource"

    def __init__(self, func, uri, *, mime_type=None, **options):
        super().__init__(func, **options)
        self.uri = uri
        self.mime_type = mime_type
        self.completions = {}
        self.parameters = URI_PARAMETER.findall(uri)
        accepted = {param.name for param in schema.wire_parameters(func)}
        missing = [name for name in self.parameters if name not in accepted]
        if missing and not schema.accepts_extra(func):
            raise ImproperlyConfigured(
                f"{func.__qualname__} does not accept URI parameters {missing}."
            )
        parts = URI_PARAMETER.split(uri)
        self.pattern = re.compile(
            "^"
            + "".join(
                re.escape(part) if index % 2 == 0 else f"(?P<{part}>[^/]+)"
                for index, part in enumerate(parts)
            )
            + "$"
        )

    @property
    def is_template(self):
        return bool(self.parameters)

    def match(self, uri):
        found = self.pattern.match(uri)
        return found.groupdict() if found else None


class PromptSpec(CompletionMixin, Spec):
    kind = "prompt"

    def __init__(self, func, **options):
        super().__init__(func, **options)
        self.completions = {}

    @cached_property
    def arguments(self):
        result = []
        for param in schema.wire_parameters(self.func):
            argument = {"name": param.name, "required": param.default is param.empty}
            description = schema.parameter_description(self.func, param.name)
            if description:
                argument["description"] = description
            result.append(argument)
        return result


class Item:
    """A spec as registered on a server, bound to its provider (if any)."""

    def __init__(self, spec, provider=None):
        self.spec = spec
        self.provider = provider
        namespace = getattr(provider, "namespace", None)
        if namespace and spec.kind != "resource":
            self.name = f"{namespace}_{spec.name}"
        else:
            self.name = spec.name
        source = type(provider) if provider is not None else spec.func
        self.module = source.__module__
        self.source = f"{source.__module__}.{source.__qualname__}"

    @property
    def kind(self):
        return self.spec.kind

    @property
    def identifier(self):
        return self.spec.uri if self.kind == "resource" else self.name

    @cached_property
    def app_label(self):
        config = apps.get_containing_app_config(self.module)
        return config.label if config else None

    def is_allowed(self, ctx):
        if self.provider is not None and not self.provider.has_permission(ctx):
            return False
        return check_permission(self.spec.permission, ctx)

    def invoke(self, func, ctx, *args, **kwargs):
        if self.provider is not None:
            return func(self.provider, ctx, *args, **kwargs)
        return func(ctx, *args, **kwargs)

    def call(self, ctx, kwargs):
        return self.invoke(self.spec.func, ctx, **kwargs)
