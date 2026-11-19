"""Generic mode: a fixed set of tools covering every ``ModelProvider``."""

import copy
from typing import Annotated, Any

from django.core.exceptions import ImproperlyConfigured

from . import schema
from .errors import InvalidArguments
from .items import Item, ToolSpec

MODEL = Annotated[str, "A model from models_list"]
ID = Annotated[str | int, "The record's lookup value (see models_list)"]

# The functions only carry the signatures and descriptions; GenericToolSpec.run
# does the work.


def models_list(ctx):
    """List the models you can use, with their fields, filters and actions.
    Call this first to learn what the other record tools accept."""


def records_search(
    ctx,
    model: MODEL,
    query: Annotated[str | None, "Free text search"] = None,
    filters: Annotated[dict[str, Any] | None, "Filters from models_list"] = None,
    limit: Annotated[int | None, "Results per page"] = None,
    cursor: Annotated[str | None, "next_cursor from the previous page"] = None,
):
    """Search or list records of one model. Pass next_cursor as cursor for the
    next page."""


def record_get(ctx, model: MODEL, id: ID):
    """Get one record of a model."""


def record_create(ctx, model: MODEL, data: Annotated[dict[str, Any], "Field values"]):
    """Create a record. models_list shows the writable fields."""


def record_update(
    ctx, model: MODEL, id: ID, data: Annotated[dict[str, Any], "Fields to change"]
):
    """Update a record. Only the fields given change."""


def record_delete(ctx, model: MODEL, id: ID):
    """Delete a record."""


TOOLS = [
    (models_list, None, {"read_only": True}),
    (records_search, "list", {"read_only": True}),
    (record_get, "get", {"read_only": True}),
    (record_create, "create", {}),
    (record_update, "update", {"idempotent": True}),
    (record_delete, "delete", {"destructive": True}),
]


class GenericToolSpec(ToolSpec):
    def __init__(self, func, action, providers, **options):
        super().__init__(func, permission=self.allowed, **options)
        self.action = action
        self.providers = providers  # {name: provider}

    def available(self, ctx):
        """``{name: provider}`` this user can use for the action."""
        found = {}
        for name, provider in self.providers.items():
            if self.action is None:
                usable = provider.allows(ctx, "list") or provider.allows(ctx, "get")
            else:
                usable = provider.allows(ctx, self.action)
            if usable:
                found[name] = provider
        return found

    def allowed(self, ctx):
        return bool(self.available(ctx))

    def get_input_schema(self, ctx):
        """The ``model`` enum lists only what this user can use."""
        if ctx is None or "model" not in self.input_schema["properties"]:
            return self.input_schema
        result = copy.deepcopy(self.input_schema)
        result["properties"]["model"]["enum"] = sorted(self.available(ctx))
        return result

    def resolve(self, ctx, name):
        available = self.available(ctx)
        if name not in available:
            raise InvalidArguments(
                f"Unknown model {name!r}. Available: {sorted(available)}."
            )
        return available[name]

    def run(self, ctx, model=None, id=None, data=None, filters=None, **options):
        if self.action is None:
            providers = self.available(ctx).values()
            return {"models": [provider.describe(ctx) for provider in providers]}
        provider = self.resolve(ctx, model)
        if self.action == "list":
            arguments = dict(filters or {})
            arguments.update({k: v for k, v in options.items() if v is not None})
            return provider.run(ctx, "list", **parse(provider, "list", arguments))
        arguments = {}
        if self.action in ("create", "update"):
            arguments.update(parse(provider, self.action, data))
        if self.action != "create":
            arguments[provider.lookup_argument] = parse_lookup(provider, id)
        return provider.run(ctx, self.action, **arguments)


class GenericItem(Item):
    def call(self, ctx, kwargs):
        return self.spec.run(ctx, **kwargs)


def parse_lookup(provider, value):
    if isinstance(value, str) and provider.lookup_annotation is not str:
        return schema.from_string(provider.lookup_annotation, value, "id")
    return schema.coerce(provider.lookup_annotation, value, "id")


def parse(provider, action, arguments):
    """Validate ``arguments`` against the provider's own schema for the action."""
    func = provider.argument_function(action)
    try:
        return schema.coerce_arguments(func, arguments)
    except InvalidArguments as error:
        if error.message.startswith("Unknown argument"):
            valid = [param.name for param in schema.wire_parameters(func)]
            raise InvalidArguments(f"{error.message} Valid: {valid}.") from None
        raise


def build_items(server, providers):
    by_name = {}
    for provider in providers:
        name = provider.get_name()
        if name in by_name:
            raise ImproperlyConfigured(
                f"Two model providers on {server!r} are named {name!r}; set "
                "ModelProvider.name on one of them."
            )
        by_name[name] = provider
    items = []
    for func, action, hints in TOOLS:
        if action is not None and not any(
            action in provider.actions for provider in providers
        ):
            continue
        items.append(GenericItem(GenericToolSpec(func, action, by_name, **hints)))
    return items
