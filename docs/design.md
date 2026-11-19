# django-xprez-mcp design

Admin-style MCP servers for Django. You register providers, models, admin
classes and plain functions on a server, and they become MCP tools, resources
and prompts, filtered by the calling user's permissions.

Status: phases 1 (core), 2 (models) and 3 (admin) implemented; phases 2 and
3 are not yet proven in a project. Sections marked *(planned)* describe API
that does not exist yet.

## Goals

- Class-based and registry-based, like `django.contrib.admin`: register,
  unregister, autodiscover a per-app `mcp.py`.
- One unit, the **provider**, contributes tools, resources and prompts. Every
  provider can be subclassed and extended.
- Providers generated from models (like DRF viewsets) and from `ModelAdmin`
  classes (reusing their permissions and querysets).
- The whole admin in one call, without exploding the tool count.
- Third-party apps can ship providers; the project can remove or replace any of
  them.
- What a user cannot use, they cannot see: `tools/list` is filtered per user.
- Plain WSGI Django. Stateless JSON over HTTP, no ASGI or extra process.
- Small dependency footprint: Django only.

## Non-goals (for now)

- SSE, server-to-client requests (elicitation, sampling), progress
  notifications. They need a stream; see phase 5.
- Async tools.
- STDIO transport.
- A token model in the core. Stored tokens are the optional
  `xprez_mcp.contrib.tokens` app; projects can plug in their own lookup.

## Concepts

| Concept | What it is |
|---|---|
| `MCPServer` | A registry plus an HTTP endpoint. `xprez_mcp.mcp` is the default instance; create more for curated audiences. |
| `Provider` | A class whose `@tool`, `@resource` and `@prompt` methods are exposed. Registered under a key (a provider class or a model). |
| `ModelProvider` | A provider generated from a model: list/get/create/update/delete, resources per object. |
| `AdminProvider` | A `ModelProvider` driven by a `ModelAdmin`: exactly the admin's permissions, plus its queryset, search, filters, form and history. |
| `CallContext` (`ctx`) | Passed to every call: server, request, user, protocol version. Projects can subclass it. |
| Item | One registered tool, resource or prompt. Identified by its name (tools, prompts) or URI (resources). |

## API

### Standalone functions

```python
from xprez_mcp import mcp

@mcp.tool(read_only=True)
def ping(ctx) -> str:
    """Check the server is up."""
    return "pong"

@mcp.resource("app://me", mime_type="application/json")
def me(ctx):
    return {"username": ctx.user.get_username()}
```

The first parameter (after `self` on methods) always receives the
`CallContext`. The rest are the item's arguments.

### Providers

```python
from typing import Annotated, Literal

from xprez_mcp import Provider, ToolError, mcp, prompt, resource, tool


@mcp.register
class CRMProvider(Provider):
    namespace = "crm"  # tool and prompt names become crm_<name>

    def has_permission(self, ctx):
        return ctx.user.is_staff

    @tool(read_only=True, permission="main.view_member")
    def search(self, ctx, query: Annotated[str, "Free text"], limit: int = 20):
        """Search members, clients and assignments at once."""
        ...

    @tool(destructive=True)
    def archive_assignment(self, ctx, id: int):
        if not can_archive(ctx.user, id):
            raise ToolError("You cannot archive this assignment.")
        ...

    @resource("crm://clients/{slug}", mime_type="application/json")
    def client(self, ctx, slug: str):
        return ClientSerializer(get_client(ctx.user, slug)).data

    @client.complete("slug")
    def _client_slugs(self, ctx, value):
        return Client.objects.filter(slug__startswith=value).values_list("slug", flat=True)

    @prompt
    def client_brief(self, ctx, slug: str, language: Literal["cs", "en"] = "en"):
        """Prepare a brief about a client before a meeting."""
        return f"Prepare a brief in {language} about crm://clients/{slug}."
```

### Extending

```python
class OurCRMProvider(CRMProvider):
    archive_assignment = None  # remove an inherited item

    @tool
    def search(self, ctx, query: str, limit: int = 10):  # replace it
        return super().search(ctx, query, limit)

    @tool
    def export(self, ctx): ...  # add one

mcp.unregister(CRMProvider)
mcp.register(OurCRMProvider)
```

Items are collected through the MRO in definition order. Redefining a method
without a decorator removes it from MCP; to keep it exposed, decorate the
override again.

### Models

```python
from xprez_mcp import ModelProvider, mcp, tool

@mcp.register(Client)
class ClientProvider(ModelProvider):
    fields = ["slug", "name", "industry"]  # or serializer = ClientSerializer (DRF)
    detail_serializer = ClientDetailSerializer
    lookup_field = "slug"
    search_fields = ["name", "group"]      # admin-style; or override search()
    filters = {
        "industry": "industry__name__icontains",
        "country": filter_country,         # f(ctx, queryset, value) -> queryset
    }
    ordering = ["name"]
    actions = ["list", "get"]              # opt in to create/update/delete
    resources = True                       # crm://clients, crm://clients/{slug}
    resource_scheme = "crm"                # default: the model's app label
    tool_names = {"list": "clients_search", "get": "client_get"}  # optional

    def get_queryset(self, ctx):
        return Client.objects.visible_to(ctx.user)

    def perform_update(self, ctx, instance, data):
        return services.update_client(ctx.user, instance, data)

    @tool
    def assignments(self, ctx, slug: str): ...
```

| Action | Arguments | Hints |
|---|---|---|
| `list` | `query`, filters, `limit`, `cursor` | read-only |
| `get` | lookup | read-only |
| `create` | writable fields | |
| `update` | lookup, partial fields | idempotent |
| `delete` | lookup | destructive |

- Only `list` and `get` are on by default.
- `name` (default: the slugified `verbose_name_plural`, `clients`) names the
  model in tool names (`clients_list`), in generic mode and in resource URIs.
  Without `name`, `name_template` builds it from `{plural}` (the default),
  `{app_label}` and `{model_name}`; `"{app_label}_{plural}"` keeps apps with
  the same plural names apart.
  `tool_names` overrides single actions, `descriptions` their descriptions.
- `lookup_field` defaults to `"pk"`, whose argument is called `id`.
- Reading needs `fields` or `serializer`, not both; with neither the provider
  raises `ImproperlyConfigured` instead of exposing every field.
  `detail_fields` / `detail_serializer` apply to `get` and to write results.
  `fields` may name model fields (a relation gives its pk, a to-many relation
  a list of pks, a file its URL), properties and methods.
- `serializer` is anything called as
  `serializer(instance, many=..., context=...)` with a `.data` attribute (DRF
  serializers). The context carries `ctx`, `request` and `user`. DRF is never
  imported by the library; when it is loaded, its `ValidationError` and other
  `APIException`s map to readable errors.
- `query` exists when `search_fields` is set or `search()` is overridden.
  Search is admin-style: every word must match one field (`^` starts with,
  `=` exact, `@` full text, default contains).
- `filters` is a list of lookups or `{argument: lookup or callable}`. A
  lookup's argument type follows the field and the lookup (`__in` takes a
  list, `__isnull` a boolean, `__icontains` a string). A callable is
  `f(ctx, queryset, value)`, typed by its `value` hint (default `str`).
  Lookups across to-many relations add `distinct()`.
- Ordering is `ordering`, then the model's `Meta.ordering`, then `pk`.
  `ordering_fields` adds an `ordering` argument.
- `list` returns `{"results", "count", "next_cursor"}`. The cursor is an
  opaque offset, so the server stays stateless. `page_size` (20) and
  `max_page_size` (100) bound `limit`.
- Related records are their ids by default. `related_labels = True` returns
  `{"id", "display"}` (`str()` of the record) for foreign keys, one-to-one and
  to-many relations, so an agent need not look each one up; lists and inlines
  prefetch them, one query per relation (an accessor that cannot be
  prefetched, like a parent link django-polymorphic replaces with a property,
  is read per row). `None` follows `MCPServer(related_labels=...)`, then
  `XPREZ_MCP["RELATED_LABELS"]`
  (default `False`). Generic foreign keys stay `str()`. Filters and writes
  still take ids.
- Writes are validated by `serializer` when there is one, otherwise by a
  `ModelForm` (`form`, default `ModelForm`) for just the fields sent. Writable
  fields are `write_fields`, by default the editable fields among `fields` (or
  the serializer's writable ones); without a serializer they must be editable
  model fields. `update` cannot change the lookup field. Validated data goes
  through `perform_create` / `perform_update` / `perform_delete` inside a
  transaction, so projects with a service layer keep it.
- `resources = True` adds `<scheme>://<name>` (the first page) and
  `<scheme>://<name>/{lookup}`, with completion when the lookup is a string.
- Generated tools are ordinary items. Excludes, `unregister_tool`, permissions
  and `mcp_inspect` treat them like decorated ones. Their schemas come from the
  model fields through the same machinery as type hints. Decorated methods on
  a model provider stay ordinary items.

#### Exposure: tools per model or generic

How model providers appear is a server option; the providers stay the same.

```python
mcp = MCPServer(models="tools")    # default: clients_list, clients_get, ...
staff_mcp = MCPServer(models="generic")
```

A provider can override the server default, so a few
curated models keep their own tools while the rest of the admin goes generic
on the same server:

```python
class ClientProvider(ModelProvider):
    exposure = "tools"  # "tools" | "generic" | None (the server's models=)
```

Tools per model have precise schemas and descriptions and suit a small,
curated set. With many models they flood the client's context, so generic mode
exposes a fixed set instead, however many models are registered:

| Tool | What it does |
|---|---|
| `models_list` | Models this user can use, with fields, filters and actions |
| `records_search(model, query, filters, limit, cursor)` | `list` of one model |
| `record_get(model, id)` | `get` of one model |
| `record_create` / `record_update` / `record_delete` | Present only when some model allows the action |

`model` is an enum built per user from the providers they pass, so
`tools/list` still hides what they cannot use. Filters and fields are checked
per call against that model's provider; an invalid one returns the valid
options so the model can retry. Decorated tools and generated resources of
model providers are exposed as usual. Two model providers with the same
`name` on one generic server raise `ImproperlyConfigured`.

### Admin

```python
from django.contrib import admin
from xprez_mcp import AdminProvider, mcp

# one model, from the ModelAdmin registered on admin.site
@mcp.register(Client)
class ClientAdminProvider(AdminProvider):
    actions = ["list", "get", "update"]
    admin_actions = ["mark_active"]  # ModelAdmin actions, opt-in
    # admin_site = my_site; admin_class = OtherClientAdmin
    # inlines = [ContactInline]; inline_names = {ContactInline: "people"}

# the whole admin
mcp.register_admin(
    admin.site,
    exclude=[LogEntry, "taggit"],  # models, app labels or "app.model"
    actions=["list", "get"],     # default; writes are opt-in, per model too:
    model_actions={Client: ["list", "get", "update"]},
    exposure="generic",          # the phase 2 generic tools
    name_template="{app_label}_{plural}",  # default "{plural}"
)
```

**Permissions are exactly the admin's.** A user can do through an
`AdminProvider` what they can do in that admin, never more; `AdminProvider`
adds no rules of its own and replaces `ModelProvider.permissions`:

| Layer | Delegates to |
|---|---|
| Provider (`has_permission`) | `admin_site.has_permission(request)` (active staff by default, or the site's override) and `model_admin.has_module_permission(request)` |
| `list`, `get` | `has_view_or_change_permission(request)` |
| `create` | `has_add_permission(request)` |
| `update`, `delete` | `has_change_permission` / `has_delete_permission(request)`, then again with `obj` after the fetch |
| Rows | `get_queryset(request)` |
| Fields | `get_fields` / `get_readonly_fields(request, obj)` per call |
| Admin actions | `get_actions(request)`, including each action's `allowed_permissions` |

Which actions are *exposed* is a separate, opt-in choice: only `list` and
`get` by default, as for `ModelProvider`. A permission granted in the admin
therefore never adds a write tool on its own.

**Mapping.** `AdminProvider` is a `ModelProvider` whose hooks call the
`ModelAdmin`:

- `fields` (list results): `list_display` entries that are model fields,
  properties or admin/model methods; `__str__` becomes `display`. The lookup
  field is always included. `get` and write results show
  `get_fields(request, obj)` (readonly fields included) unless
  `detail_fields` / `detail_serializer` are set. Values that are not JSON
  (model instances, HTML) are sent as strings.
- Hidden fields are never read or written: `hidden_fields` and the fields the
  admin's form only shows masked (`ReadOnlyPasswordHashField`, so a user
  admin never sends password hashes), on the model and its inlines.
- Search: `get_search_results(request, queryset, query)`, with `distinct()`
  when it may return duplicates. `query` exists when the admin has
  `search_fields`.
- Filters: `list_filter` entries. A field name becomes a filter typed by the
  field (choices as an enum, relations by pk, booleans, dates as `__gte` /
  `__lte` pairs); `(field, FieldListFilter)` filters by the field. A
  `SimpleListFilter` becomes a string argument named by its `parameter_name`,
  checked per call against `lookups(request, model_admin)` (an invalid value
  returns the options) and applied with `queryset(request, qs)`. Unsupported
  entries are skipped and reported by `mcp_inspect`.
- Ordering: `get_ordering(request)`, with `pk` added for stable pages.
- Writes: the schema comes from the admin's fields read without a user
  (`get_fields` / `get_readonly_fields`, or the static `fields` /
  `fieldsets` when an override needs a user), limited to editable model
  fields. Per call, sending a field that `get_fieldsets(request, obj)` or
  `get_readonly_fields(request, obj)` excludes is an error. Validation uses `get_form(request, obj, fields=sent)`; then
  `save_form`, `save_model`, `save_related` (no formsets; inline rows have
  their own tools, below), and
  `log_addition` / `log_change` with `construct_change_message`, so the
  admin history shows the change. `delete` first runs
  `get_deleted_objects`, so it fails when the cascade reaches registered
  models the user cannot delete or protected objects, then logs the deletion
  and calls `delete_model`. All inside a transaction.
- Admin actions (`admin_actions`, opt-in): one `<name>_action(action, ids)`
  tool per model, `record_action(model, action, ids)` in generic mode. The
  tool is visible when `get_actions(request)` offers the user one of them,
  and an action it does not offer is refused with the available ones. An
  action without `permissions` is offered to everyone who can see the model,
  as in the admin. An action that returns a response (an intermediate page,
  a download) is rolled back and reported as unsupported; `delete_selected`
  is never exposed.

**The request.** Admin methods get a copy of the MCP request
(`get_admin_request(ctx)`, one per call): `user` is the caller, `method` is `GET`
for reads and `POST` for writes, `GET` / `POST` are empty, `current_app` is
the site's name. Its message storage collects what `message_user` /
`messages.*` add, and those messages are returned in the result
(`"messages": [{"level": "info", "message": "..."}]`). `request.session` is whatever the middleware set;
admin code that needs one fails without session middleware.

**`register_admin`** creates an `AdminProvider` subclass for every model in
the site's registry, when the catalog is built (so it does not depend on the
order of autodiscovery). Models registered on the server explicitly win;
`exclude` removes models or apps; `provider_class` sets the base class;
`exposure` and `name_template` set the providers' attributes. The generated
classes take the `ModelAdmin`'s module, so `EXCLUDE_APPS` applies by the
admin's app.
`mcp_exposed = False` on a `ModelAdmin`, an inline or a model keeps it out of
`register_admin` and of the parent's inline tools, wherever it is
registered (credentials, for example).

**Inlines.** Inline rows are edited one at a time through tools scoped to
the parent, never nested in the parent's payload (large nested schemas are
where models make mistakes, and "missing row" would be ambiguous):

| Tool | Generic mode |
|---|---|
| `clients_contacts_list(parent, limit, cursor)` | `record_inline_list(model, id, inline, ...)` |
| `clients_contacts_create(parent, ...)` | `record_inline_create(model, id, inline, data)` |
| `clients_contacts_update(parent, id, ...)` | `record_inline_update(model, id, inline, row, data)` |
| `clients_contacts_delete(parent, id)` | `record_inline_delete(model, id, inline, row)` |

- The inlines are those of `get_formsets_with_inlines(request, obj)`, so
  per-user and per-object rules hold; `inlines = [ContactInline]` limits
  which are exposed. An inline is named by its model's slugified
  `verbose_name_plural` (`inline_names` overrides). Inlines without a foreign
  key to the model (generic relations) are skipped.
- `get` includes each inline's rows (up to `max_page_size`, with a count);
  the list tool pages the rest. Reading needs the parent's `get`.
- Writes need the parent's `update` to be exposed (editing inlines is part
  of changing the parent in the admin), the parent's
  `has_change_permission(request, obj)` and the inline's
  `has_add/change/delete_permission(request, obj)`.
- Validation runs the inline's whole formset (the existing rows plus the one
  change), so `min_num` / `max_num`, the formset's `clean()` and uniqueness
  across rows still apply; rows the user may only view are not validated,
  as in the change form. Then `save_formset` and a `log_change` on the
  parent, in one transaction per call.

Not in phase 3: admin views beyond the changelist and change form,
`list_editable`.

### Unregistering and third-party apps

```python
# some_lib/mcp.py, discovered automatically
mcp.register(TagProvider)

# the project
mcp.unregister(TagProvider)             # the whole provider (or a model key)
mcp.unregister_tool("tags_delete")      # one item
mcp.unregister_resource("tags://{slug}")
mcp.unregister_prompt("tags_cleanup")
```

```python
# settings.py: applied when the catalog is built, so app order does not matter
XPREZ_MCP = {
    "EXCLUDE": ["tags_delete", "filer_*"],  # glob on tool/prompt names and resource URIs
    "EXCLUDE_APPS": ["some_noisy_lib"],
}
```

Writes from third-party apps are opt-in. A tool that is
not `read_only` and comes from an app installed as a package (its path under
`site-packages` / `dist-packages`) is left out until the app is allowed:

```python
XPREZ_MCP = {"ALLOW_WRITES": ["taggit", "filer"]}  # app labels, globs allowed
```

Reads, resources and prompts are exposed by default. Per server,
`MCPServer(allow_writes=[...])` adds to the setting. Model providers of such
apps lose their write actions in generic mode too. `server.held_back` and
`mcp_inspect` list the held back tools. `register_admin` providers count as
the project's own: their writes are already opt-in through `actions`.
`MCPServer.is_third_party(app_config)` can be overridden.

### Several servers

```python
from xprez_mcp import MCPServer

sales_mcp = MCPServer(name="crm-sales", instructions="...")
sales_mcp.register(ClientProvider)

urlpatterns = [
    path("mcp/", mcp.urls),
    path("mcp/sales/", sales_mcp.urls),
]
```

Autodiscovered modules register on `mcp`. A curated server imports providers
and registers them itself, the way a custom `AdminSite` does.

## Permissions

Closed by default: a server nobody configured lets nobody in.

A permission is always one of: a permission string (`user.has_perm`), a list
of them (all required), a callable `permission(ctx) -> bool`, or a shorthand:
`"authenticated"`, `"staff"` or `"any"` (Django permission names always contain
a dot, so they cannot clash; an unknown dotless name raises
`ImproperlyConfigured`). The shorthands are also importable as
`xprez_mcp.permissions.allow_authenticated`, `allow_staff` and `allow_any`.

### Server access

```python
XPREZ_MCP = {"PERMISSION": "crm.use_mcp"}  # required, no default

sales_mcp = MCPServer(permission=lambda ctx: ctx.user.is_salesperson)
```

- Checked after authentication and before any method, `initialize` included.
  A user who fails it gets `403`, so the client fails when it connects
  instead of seeing an empty catalog.
- Without a permission (neither the setting nor the argument), a system check
  error (`xprez_mcp.E001`) fails `manage.py check`, `runserver` and tests, and
  the server answers `403` in case checks are skipped.
- Opening up is explicit and visible in review: `"authenticated"`, `"staff"`
  or `"any"`. `allow_anonymous=True` also needs `"any"` (or a callable that
  accepts `ctx.user` being None); on its own it no longer opens the server.
- The checks cover servers mounted in the URLconf, so an unused default `mcp`
  does not fail them.
- No bundled `use_mcp` permission: that would need a model and a migration.
  Projects use one of their own.

### Items

Past the server check, an item is visible and callable when both hold:

1. `provider.has_permission(ctx)` is true (providers only; default `True`).
2. The item's `permission` passes (default: none).

Items that fail are left out of `*/list` and behave as unknown on call, so
their existence does not leak. Row-level rules belong in `get_queryset` or in
the item's own code.

### Models

Models sit below the admin and views, so `ModelProvider` adds no permission
check of its own, Django's model permissions included. The server permission,
`get_queryset` (which rows exist for the user) and opting in to writes are
the defaults. `create` is not limited by the queryset, so enabling it without
a permission lets every user who passes the server check create records.

```python
class ClientProvider(ModelProvider):
    actions = ["list", "get", "update", "delete"]
    permissions = {                    # per action, hides the tool
        "update": "clients.change_client",
        "delete": lambda ctx: ctx.user.is_superuser,
    }
    # permissions = ModelProvider.django_model_permissions  # view/add/change/delete

    def has_action_permission(self, ctx, action):
        return super().has_action_permission(ctx, action)

    def has_object_permission(self, ctx, action, instance):  # after the fetch
        return action == "get" or instance.owner_id == ctx.user.pk
```

A failed action permission hides the tool. A failed object permission is a
readable `PermissionDenied`: the user can already see the row. In generic
mode the `model` enum and the presence of each record tool follow the same
checks.

### Order of checks

Authentication (`401`) → server permission (`403`) → `provider.has_permission`
and the item or action permission (hidden) → `get_queryset` (not found) →
`has_object_permission` (`PermissionDenied`) → the call or `perform_*`. Each
layer can only narrow the one before it.

## Names

Tool and prompt names must match `^[A-Za-z0-9_-]{1,64}$`. That is stricter
than the MCP spec (128 characters, dots allowed) but is what the Claude and
OpenAI APIs accept, so names survive the phase 4 export and every client.
Names are checked when the catalog is built, because namespaces, model names
and `tool_names` are only final then: an invalid name raises
`InvalidName` (an `ImproperlyConfigured`), and the system check error `xprez_mcp.E003` reports
it at `manage.py check`. Names are never rewritten. A warning
(`xprez_mcp.W001`) flags names over 40 characters, as clients that prefix
the server name can hit their own limits. Resource URIs follow URI rules
only.

## Schemas and arguments

The input schema is derived from type hints: `str`, `int`, `float`, `bool`,
`Decimal`, `date`, `datetime`, `UUID`, `Enum`, `Literal`, `list[X]`,
`dict[str, X]`, `X | None`. `Annotated[X, "text"]` adds a description.
Parameters without a default are required; unknown arguments are rejected.
Arguments are validated and converted before the call (an ISO string becomes
a `date`, a value becomes an `Enum` member).

`@tool(input_schema={...})` replaces the derived schema with a hand-written
one (validated with a small JSON Schema subset, passed through unconverted).

Resource URI parameters and prompt arguments arrive as strings and are
converted with the same type hints.

## Results and errors

- A tool returning `str` gives a text block. Anything else is JSON-encoded
  (Django's encoder); a `dict` is also sent as `structuredContent`.
  `ToolResult` gives full control.
- `ToolError` and argument errors become a result with `isError: true`, so the
  model can read the message and retry.
- Django's `PermissionDenied`, `ObjectDoesNotExist` / `Http404` and
  `ValidationError` map to readable errors. Anything else is logged and
  reported as an internal error.
- `@mcp.error_handler(VersionConflict)` maps project exceptions to a result,
  for example a version conflict returning the current record.
- Unknown tools, prompts and resources are JSON-RPC errors.

## Transport

- One Django view per server: `POST` with a single JSON-RPC message, JSON
  response. `GET` returns 405 (no SSE stream). Batches are rejected.
- Methods: `initialize`, `ping`, `tools/list`, `tools/call`,
  `resources/list`, `resources/templates/list`, `resources/read`,
  `prompts/list`, `prompts/get`, `completion/complete`. Notifications and
  client responses get `202`.
- `tools/list`, `resources/list`, `resources/templates/list` and
  `prompts/list` page with the spec's `cursor` / `nextCursor`. When paging is
  on, the per-user list is sorted by name or URI and the cursor is the last
  identifier seen (opaque, keyset), so the server stays
  stateless and a catalog that changes between pages neither skips nor
  repeats items. Off by default, because not every client follows
  `nextCursor`: `XPREZ_MCP["LIST_PAGE_SIZE"]` (default `None`, one page) or
  `MCPServer(list_page_size=...)` (`0` turns it off).
- Protocol versions: `2025-06-18`, `2025-03-26`, `2024-11-05`. A newer spec
  version is reviewed and added by hand.
- Requests must be `application/json`. A browser `Origin` must match the host
  or `XPREZ_MCP["ALLOWED_ORIGINS"]` (DNS rebinding protection; per server
  `MCPServer(allowed_origins=[...])`). Together these
  also stop cross-site form posts when session auth is enabled.

## Authentication

`XPREZ_MCP["AUTHENTICATION"]` (or `MCPServer(authentication=[...])`) is a list
of callables `backend(request) -> user | None`, tried in order. A backend
raises `AuthenticationFailed` to reject a request outright.

- `xprez_mcp.auth.bearer_token`: reads `Authorization: Bearer <token>` and calls
  `XPREZ_MCP["TOKEN_RESOLVER"]` (`resolver(request, token) -> user | None`).
  Without it, the stored tokens of `xprez_mcp.contrib.tokens` when that app
  is installed; otherwise `ImproperlyConfigured`.
- `xprez_mcp.auth.session`: `request.user` from the session.

Without a user the endpoint answers `401` with `WWW-Authenticate: Bearer`,
unless the server was created with `allow_anonymous=True`.

OAuth (protected resource metadata, RFC 9728) is planned for later.

### Stored tokens (`xprez_mcp.contrib.tokens`)

The default for projects without a token model of their own: add the app to
`INSTALLED_APPS`, migrate, and users create tokens under "MCP tokens" in
the admin.

- `Token`: `user`, `name` (where it is used), `expires` (optional),
  `created`, `last_used`
  (updated at most once a minute). A user can have any number of tokens.
- The key is `mcp_<key_id>_<secret>`: 12 hex characters to find the row, 32
  random bytes of secret. Only its SHA-256 hash is stored (the key is random,
  so a slow password hash is not needed). It is shown once, after creating the
  token, and never again; a lost key means a new token. Deleting a token
  revokes it.
- `TokenResolver` (`xprez_mcp.contrib.tokens.auth.resolve`) compares the hash
  in constant time and rejects expired tokens and
  inactive users. It loads the user as the session would (the first of
  `AUTHENTICATION_BACKENDS` whose `get_user` returns one; override `get_user`)
  and sets `request.mcp_token`. Who may use a server stays the server
  `permission`.
- `TokenAdmin` (registered on `admin.site`) works under the model
  permissions (`add_token`, `view_token`, `change_token`, `delete_token`).
  Non-superusers see only their own tokens and create tokens only for
  themselves (`get_queryset`, `formfield_for_foreignkey`); superusers manage
  everyone's. The key is shown once, in the admin message after "Save". The
  message storage holds it for that one redirect (a signed cookie with the
  default storage; `MESSAGE_STORAGE` session storage keeps it server-side).
  Project rules go in the usual `ModelAdmin` methods of a subclass, for
  example refusing `has_add_permission` while an administrator acts as
  another user:

```python
admin.site.unregister(Token)

@admin.register(Token)
class ProjectTokenAdmin(TokenAdmin):
    def has_add_permission(self, request):
        return not request.session.get("impersonating") and super().has_add_permission(request)
```

`Token` and `TokenAdmin` set `mcp_exposed = False`, which keeps them out of
`AdminProvider`: an MCP client does not manage the credentials it could
authenticate with.

## Settings

```python
XPREZ_MCP = {
    "NAME": "django",              # serverInfo.name of the default server
    "VERSION": "0.0.0",
    "PERMISSION": None,            # required; see Permissions
    "AUTODISCOVER": True,
    "AUTODISCOVER_MODULE": "mcp",
    "AUTHENTICATION": ["xprez_mcp.auth.bearer_token"],
    "TOKEN_RESOLVER": None,        # "myapp.tokens.resolve"; None: contrib.tokens
    "ALLOWED_ORIGINS": [],
    "EXCLUDE": [],
    "EXCLUDE_APPS": [],
    "ALLOW_WRITES": [],            # third-party apps whose writes are exposed
    "LIST_PAGE_SIZE": None,        # page size of */list; None sends everything
    "RELATED_LABELS": False,       # related records as {"id", "display"}
}
```

With several servers, the settings are defaults:

- `NAME`, `VERSION`, `AUTHENTICATION`, `PERMISSION`, `ALLOWED_ORIGINS`,
  `LIST_PAGE_SIZE` and `RELATED_LABELS` are replaced by the matching `MCPServer` argument.
  `ALLOW_WRITES` is combined with the server's `allow_writes`.
- `EXCLUDE` and `EXCLUDE_APPS` are combined with the server's `exclude` /
  `exclude_apps`. A server can exclude more but never bring back what the
  project excluded.
- `TOKEN_RESOLVER` feeds the shared `auth.bearer_token`. A server with its own
  resolver uses `authentication=[BearerToken(resolver=...)]`.
- `AUTODISCOVER` and `AUTODISCOVER_MODULE` are global and fill the default
  `mcp` only.

The server name is also its URL namespace, so every server needs a unique
name. A system check error (`xprez_mcp.E002`) reports
servers that share a name, including two unnamed ones falling back to `NAME`.

## Management commands

- `mcp_inspect [--server dotted.path] [--user username]` lists every item, its
  source, and with `--user` which ones that user cannot see. It also lists
  write tools held back by `ALLOW_WRITES` and admin entries (`list_filter`,
  inlines) that `AdminProvider` could not map.

## Phases

1. **Core** (in progress): `MCPServer`, `Provider`, `@tool` / `@resource` /
   `@prompt` / completions, `CallContext`, schemas from type hints, error
   mapping, stateless view, pluggable auth, Origin check, autodiscovery,
   excludes, `mcp_inspect`. Proven in a project.
2. **Models** (implemented, not yet proven): required server permission,
   `ModelProvider`, `get_queryset(ctx)`, `perform_*` hooks, serializers (DRF
   optional), generated resources, pagination, generic mode. Released as 0.2.0;
   the required permission breaks 0.1.0 configurations.
3. **Admin and third-party** (implemented, not yet proven): `AdminProvider`
   with the admin's permissions, querysets, search, `list_filter`, forms,
   `save_model` and `LogEntry`; inline tools; admin actions (opt-in);
   `register_admin`; per-provider `exposure`; opt-in writes for third-party
   apps (`ALLOW_WRITES`); `*/list` pagination; the portable name rule. Released
   as 0.3.0; the name rule breaks names outside `[A-Za-z0-9_-]` or over 64
   characters.
4. **Advanced**: audiences (one catalog, per-audience wording), composite tools
   generated from other items (a `batch_update` over several tools),
   confirmation policies (`flag`, `deferred` approval, `elicit`), export as
   Anthropic API tool definitions for in-app agents.
5. **Streaming** (optional): SSE transport for elicitation, sampling and
   progress.

## Open questions

None at the moment.

## Prior art

Read before starting; none covered the whole design.

- django-mcpz: stateless WSGI transport, bearer tokens and OAuth; tools only.
- django-mcp-server: class-based `ModelQueryToolset` / `MCPToolset`, several
  servers; no admin, no unregister.
- django-admin-mcp (7tg): per-`ModelAdmin` mixin with CRUD, actions, resources
  and prompts. Reference for phase 3 edge cases.
- django-admin-mcp-api: whole admin through generic tools. Reference for the
  generic mode.
- django-mcp-kit: class-based tools, `ModelToolset`, DRF on-ramp; global
  self-registration, no per-user querysets, ASGI only.
- django-mcp-framework: derives everything from admin, views and DRF with no
  opt-in.
