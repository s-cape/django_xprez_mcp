"""Generic mode: a fixed set of tools covering every ``ModelProvider``."""

import copy
from typing import Annotated, Any, Optional, Union

from django.core.exceptions import ImproperlyConfigured

from . import schema
from .errors import InvalidArguments
from .items import Item, ToolSpec
from .model_provider import HINTS

MODEL = Annotated[str, "A model from models_list"]
ID = Annotated[Union[str, int], "The record's lookup value (see models_list)"]

# The functions only carry the signatures and descriptions; GenericToolSpec.run
# does the work.


def models_list(ctx):
    """List the models you can use, with their fields, filters and actions.
    Call this first to learn what the other record tools accept."""


def records_search(
    ctx,
    model: MODEL,
    query: Annotated[Optional[str], "Free text search"] = None,
    filters: Annotated[Optional[dict[str, Any]], "Filters from models_list"] = None,
    limit: Annotated[Optional[int], "Results per page"] = None,
    cursor: Annotated[Optional[str], "next_cursor from the previous page"] = None,
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


def record_action(
    ctx,
    model: MODEL,
    action: Annotated[str, "An admin action from models_list"],
    ids: Annotated[list[Union[str, int]], "Lookup values of the records"],
):
    """Run an admin action on records. models_list shows the actions."""


INLINE = Annotated[str, "An inline from models_list"]
ROW = Annotated[Union[str, int], "The inline row's id"]


def record_inline_list(
    ctx,
    model: MODEL,
    parent: ID,
    inline: INLINE,
    limit: Annotated[Optional[int], "Results per page"] = None,
    cursor: Annotated[Optional[str], "next_cursor from the previous page"] = None,
):
    """List the rows of a record's inline. Pass next_cursor as cursor for the
    next page."""


def record_inline_create(
    ctx,
    model: MODEL,
    parent: ID,
    inline: INLINE,
    data: Annotated[dict[str, Any], "Field values"],
):
    """Add a row to a record's inline."""


def record_inline_update(
    ctx,
    model: MODEL,
    parent: ID,
    inline: INLINE,
    row: ROW,
    data: Annotated[dict[str, Any], "Fields to change"],
):
    """Change a row of a record's inline. Only the fields given change."""


def record_inline_delete(ctx, model: MODEL, parent: ID, inline: INLINE, row: ROW):
    """Remove a row from a record's inline."""


def _hints(action):
    if action is None:
        return HINTS["list"]
    else:
        return HINTS[action.removeprefix("inline_")]


TOOLS = [
    (models_list, None),
    (records_search, "list"),
    (record_get, "get"),
    (record_create, "create"),
    (record_update, "update"),
    (record_delete, "delete"),
    (record_action, "admin_action"),
    (record_inline_list, "inline_list"),
    (record_inline_create, "inline_create"),
    (record_inline_update, "inline_update"),
    (record_inline_delete, "inline_delete"),
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

    def run(self, ctx, model=None, **kwargs):
        if self.action is None:
            providers = self.available(ctx).values()
            return {"models": [provider.describe(ctx) for provider in providers]}
        else:
            provider = self.resolve(ctx, model)
            return provider.run(
                ctx, self.action, **self.provider_arguments(provider, kwargs)
            )

    def provider_arguments(self, provider, kwargs):
        if self.action == "list":
            arguments = dict(kwargs.get("filters") or {})
            arguments.update(
                {
                    key: value
                    for key, value in kwargs.items()
                    if key != "filters" and value is not None
                }
            )
            return parse(provider, "list", arguments)
        elif self.action == "admin_action":
            return {
                "action": kwargs["action"],
                "ids": [parse_lookup(provider, value) for value in kwargs["ids"]],
            }
        elif self.action.startswith("inline_"):
            inline = kwargs["inline"]
            provider.get_exposed_inline(inline)
            operation = self.action.removeprefix("inline_")
            arguments = {
                "inline": inline,
                "parent": parse_lookup(provider, kwargs["parent"]),
            }
            if operation == "list":
                arguments.update(
                    {
                        key: value
                        for key, value in kwargs.items()
                        if key not in ("inline", "parent") and value is not None
                    }
                )
                return arguments
            else:
                if operation != "create":
                    arguments["row"] = kwargs["row"]
                if operation != "delete":
                    arguments.update(
                        parse_function(
                            provider.inline_argument_function(inline, operation),
                            kwargs.get("data"),
                        )
                    )
                return arguments
        else:
            arguments = {}
            if self.action in ("create", "update"):
                arguments.update(parse(provider, self.action, kwargs.get("data")))
            if self.action != "create":
                arguments[provider.lookup_argument] = parse_lookup(
                    provider, kwargs["id"]
                )
            return arguments


class GenericItem(Item):
    def call(self, ctx, kwargs):
        return self.spec.run(ctx, **kwargs)


def parse_lookup(provider, value):
    if isinstance(value, str) and provider.lookup_annotation is not str:
        return schema.from_string(provider.lookup_annotation, value, "id")
    return schema.coerce(provider.lookup_annotation, value, "id")


def parse(provider, action, arguments):
    """Validate ``arguments`` against the provider's own schema for the action."""
    return parse_function(provider.argument_function(action), arguments)


def parse_function(func, arguments):
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
                "ModelProvider.name on one of them, or name_template (for "
                'example register_admin(name_template="{app_label}_{plural}")).'
            )
        by_name[name] = provider
    items = []
    for func, action in TOOLS:
        if action is not None and not any(
            action in provider.generic_actions() for provider in providers
        ):
            continue
        items.append(
            GenericItem(GenericToolSpec(func, action, by_name, **_hints(action)))
        )
    return items
