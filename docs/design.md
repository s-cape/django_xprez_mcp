# django-mcpkit design

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
| `MCPServer` | A registry plus an HTTP endpoint. `mcpkit.mcp` is the default instance; create more for curated audiences. |
| `Provider` | A class whose `@tool`, `@resource` and `@prompt` methods are exposed. Registered under a key (a provider class or a model). |
| `ModelProvider` *(planned)* | A provider generated from a model: list/get/create/update/delete, resources per object. |
| `AdminProvider` *(planned)* | A provider generated from a `ModelAdmin`, reusing its permissions, queryset, search, filters and form. |
| `CallContext` (`ctx`) | Passed to every call: server, request, user, protocol version. Projects can subclass it. |
| Item | One registered tool, resource or prompt. Identified by its name (tools, prompts) or URI (resources). |

## API

### Standalone functions

```python
from mcpkit import mcp

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

from mcpkit import Provider, ToolError, mcp, prompt, resource, tool


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
from mcpkit import ModelProvider, mcp, tool

@mcp.register(Client)
class ClientProvider(ModelProvider):
    serializer = ClientSerializer          # or fields = [...]
    detail_serializer = ClientDetailSerializer
    lookup_field = "slug"
    search_fields = ["name", "group"]
    filters = ["industry__name", "country__name"]
    actions = ["list", "get"]              # opt in to create/update/delete
    resources = True                       # crm://clients, crm://clients/{slug}

    def get_queryset(self, ctx):
        return Client.objects.visible_to(ctx.user)

    def perform_update(self, ctx, instance, data):
        return services.update_client(ctx.user, instance, data)

    @tool
    def assignments(self, ctx, slug: str): ...
```

Writes go through `perform_create` / `perform_update` / `perform_delete`, so
projects with a service layer keep it.

### Admin *(planned, phase 3)*

```python
from django.contrib import admin
from mcpkit import AdminProvider, mcp

mcp.register(Client, AdminProvider.from_admin(ClientAdmin, actions=["list", "get", "update"]))

mcp.register_admin(
    admin.site,
    exclude=[LogEntry],
    mode="generic",  # list_models / describe_model / query_model instead of tools per model
)
```

`AdminProvider` builds a request-like object for the caller and delegates to
`get_queryset`, `has_view/add/change/delete_permission`, `get_search_results`,
`list_filter`, the admin form, `save_model` / `save_related` and `LogEntry`.
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
MCPKIT = {
    "EXCLUDE": ["tags_delete", "filer_*"],  # glob on tool/prompt names and resource URIs
    "EXCLUDE_APPS": ["some_noisy_lib"],
}
```

Policy for third-party writes (opt-in vs. opt-out) is open; see open questions.

### Several servers

```python
from mcpkit import MCPServer

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

An item is visible and callable when both hold:

1. `provider.has_permission(ctx)` is true (providers only; default `True`).
2. The item's `permission` passes: a permission string (`user.has_perm`), a
   list of them (all required) or a callable `permission(ctx) -> bool`.

Items that fail are left out of `*/list` and behave as unknown on call, so
their existence does not leak. Row-level rules belong in `get_queryset` (phase
2) or in the item's own code.

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
  or `MCPKIT["ALLOWED_ORIGINS"]` (DNS rebinding protection). Together these
  also stop cross-site form posts when session auth is enabled.

## Authentication

`MCPKIT["AUTHENTICATION"]` (or `MCPServer(authentication=[...])`) is a list
of callables `backend(request) -> user | None`, tried in order. A backend
raises `AuthenticationFailed` to reject a request outright.

- `mcpkit.auth.bearer_token`: reads `Authorization: Bearer <token>` and calls
  `MCPKIT["TOKEN_RESOLVER"]` (`resolver(request, token) -> user | None`).
  Projects keep their own token models.
- `mcpkit.auth.session`: `request.user` from the session.

Without a user the endpoint answers `401` with `WWW-Authenticate: Bearer`,
unless the server was created with `allow_anonymous=True`.

OAuth (protected resource metadata, RFC 9728) is planned for later.

## Settings

```python
MCPKIT = {
    "NAME": "django",              # serverInfo.name of the default server
    "VERSION": "0.0.0",
    "AUTODISCOVER": True,
    "AUTODISCOVER_MODULE": "mcp",
    "AUTHENTICATION": ["mcpkit.auth.bearer_token"],
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
2. **Models**: `ModelProvider`, `get_queryset(ctx)`, `perform_*` hooks,
   serializers (DRF optional), generated resources, pagination.
3. **Admin and third-party**: `AdminProvider`, `register_admin`, generic mode,
   write policy for third-party providers.
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
