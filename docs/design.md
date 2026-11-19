# django-xprez-mcp design

Admin-style MCP servers for Django. You register providers, models, admin
classes and plain functions on a server, and they become MCP tools, resources
and prompts, filtered by the calling user's permissions.

Status: phase 1 (core) in progress. Sections marked *(planned)* describe API
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
- A token model. Projects plug in their own token lookup.

## Concepts

| Concept | What it is |
|---|---|
| `MCPServer` | A registry plus an HTTP endpoint. `xprez_mcp.mcp` is the default instance; create more for curated audiences. |
| `Provider` | A class whose `@tool`, `@resource` and `@prompt` methods are exposed. Registered under a key (a provider class or a model). |
| `ModelProvider` *(planned)* | A provider generated from a model: list/get/create/update/delete, resources per object. |
| `AdminProvider` *(planned)* | A provider generated from a `ModelAdmin`, reusing its permissions, queryset, search, filters and form. |
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

### Models *(planned, phase 2)*

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
- Default tool names are `<plural>_<action>` (`clients_list`, `clients_get`),
  the plural being the slugified `verbose_name_plural`. `tool_names`
  overrides single actions.
- `fields` or `serializer` is required. Without either the provider raises
  `ImproperlyConfigured` instead of exposing every field.
- `serializer` is anything called as
  `serializer(instance, many=..., context=...)` with a `.data` attribute (DRF
  serializers). The context carries `ctx`,
  `request` and `user`. DRF is imported only when used, and its
  `ValidationError`, `NotFound` and `PermissionDenied` map to readable errors.
- `list` returns `{"results", "count", "next_cursor"}`. The cursor is an
  opaque offset, so the server stays stateless. Default limit 20, maximum 100.
- Writes are validated by the DRF serializer when there is one, otherwise by a
  `ModelForm` built from `fields`. Cleaned data goes through `perform_create` /
  `perform_update` / `perform_delete`, so projects with a service layer keep it.
- Generated tools are ordinary items. Excludes, `unregister_tool`, permissions
  and `mcp_inspect` treat them like decorated ones. Their schemas come from the
  model fields through the same machinery as type hints.

#### Exposure: tools per model or generic

How model providers appear is a server option; the providers stay the same.

```python
mcp = MCPServer(models="tools")    # default: clients_list, clients_get, ...
staff_mcp = MCPServer(models="generic")
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
options so the model can retry.

### Admin *(planned, phase 3)*

```python
from django.contrib import admin
from xprez_mcp import AdminProvider, mcp

mcp.register(Client, AdminProvider.from_admin(ClientAdmin, actions=["list", "get", "update"]))

mcp.register_admin(
    admin.site,
    exclude=[LogEntry],
    mode="generic",  # the generic model tools from phase 2 instead of tools per model
)
```

`AdminProvider` builds a request-like object for the caller and delegates to
`get_queryset`, `has_view/add/change/delete_permission`, `get_search_results`,
`list_filter`, the admin form, `save_model` / `save_related` and `LogEntry`.
The admin permissions are applied here, not in `ModelProvider`.
Admin code that needs more of a real request (sessions, messages) needs a
fallback; to be designed in phase 3.

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

Policy for third-party writes (opt-in vs. opt-out) is open; see open questions.

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
of them (all required) or a callable `permission(ctx) -> bool`.

### Server access *(planned, phase 2)*

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
- Opening up is explicit and visible in review:
  `xprez_mcp.permissions.allow_authenticated`, `allow_staff` and `allow_any`.
  `allow_anonymous=True` also needs `allow_any`; on its own it no longer
  opens the server.
- No bundled `use_mcp` permission: that would need a model and a migration.
  Projects use one of their own.

### Items

Past the server check, an item is visible and callable when both hold:

1. `provider.has_permission(ctx)` is true (providers only; default `True`).
2. The item's `permission` passes (default: none).

Items that fail are left out of `*/list` and behave as unknown on call, so
their existence does not leak. Row-level rules belong in `get_queryset` or in
the item's own code.

### Models *(planned, phase 2)*

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
- Protocol versions: `2025-06-18`, `2025-03-26`, `2024-11-05`. A newer spec
  version is reviewed and added by hand.
- Requests must be `application/json`. A browser `Origin` must match the host
  or `XPREZ_MCP["ALLOWED_ORIGINS"]` (DNS rebinding protection). Together these
  also stop cross-site form posts when session auth is enabled.

## Authentication

`XPREZ_MCP["AUTHENTICATION"]` (or `MCPServer(authentication=[...])`) is a list
of callables `backend(request) -> user | None`, tried in order. A backend
raises `AuthenticationFailed` to reject a request outright.

- `xprez_mcp.auth.bearer_token`: reads `Authorization: Bearer <token>` and calls
  `XPREZ_MCP["TOKEN_RESOLVER"]` (`resolver(request, token) -> user | None`).
  Projects keep their own token models.
- `xprez_mcp.auth.session`: `request.user` from the session.

Without a user the endpoint answers `401` with `WWW-Authenticate: Bearer`,
unless the server was created with `allow_anonymous=True`.

OAuth (protected resource metadata, RFC 9728) is planned for later.

## Settings

```python
XPREZ_MCP = {
    "NAME": "django",              # serverInfo.name of the default server
    "VERSION": "0.0.0",
    "PERMISSION": None,            # required (phase 2); see Permissions
    "AUTODISCOVER": True,
    "AUTODISCOVER_MODULE": "mcp",
    "AUTHENTICATION": ["xprez_mcp.auth.bearer_token"],
    "TOKEN_RESOLVER": None,        # "myapp.tokens.resolve"
    "ALLOWED_ORIGINS": [],
    "EXCLUDE": [],
    "EXCLUDE_APPS": [],
}
```

## Management commands

- `mcp_inspect [--server dotted.path] [--user username]` lists every item, its
  source, and with `--user` which ones that user cannot see.

## Phases

1. **Core** (in progress): `MCPServer`, `Provider`, `@tool` / `@resource` /
   `@prompt` / completions, `CallContext`, schemas from type hints, error
   mapping, stateless view, pluggable auth, Origin check, autodiscovery,
   excludes, `mcp_inspect`. Proven in a project.
2. **Models**: required server permission, `ModelProvider`,
   `get_queryset(ctx)`, `perform_*` hooks, serializers (DRF optional),
   generated resources, pagination, generic mode. Released as 0.2.0; the
   required permission breaks 0.1.0 configurations. Steps, one PR each:
   1. Server permission, `allow_*` helpers, system check.
   2. Generated items; `ModelProvider` with `list` / `get`, `fields`,
      `get_queryset`, lookup; test models.
   3. Search, filters, ordering, pagination.
   4. Generic mode.
   5. Generated resources and completion.
   6. Writes: `ModelForm` validation, `perform_*`, action and object
      permissions.
   7. DRF serializers and error mapping; a CI job with DRF installed.
   8. Docs, release.
3. **Admin and third-party**: `AdminProvider`, `register_admin` (reusing the
   generic mode), write policy for third-party providers.
4. **Advanced**: audiences (one catalog, per-audience wording), composite tools
   generated from other items (a `batch_update` over several tools),
   confirmation policies (`flag`, `deferred` approval, `elicit`), export as
   Anthropic API tool definitions for in-app agents.
5. **Streaming** (optional): SSE transport for elicitation, sampling and
   progress.

## Open questions

- Third-party providers: expose reads by default and require opt-in for
  writes, or require opt-in for everything?
- Should admin-based providers be limited to staff by default?
- Mixing exposures on one server (a few models as their own tools, the rest
  generic)?
- Pagination for `*/list` methods once catalogs get large.
- Tool name rules: enforce the MCP character set at registration?

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
