# django-xprez-mcp

Admin-style MCP servers for Django. Register providers, models, admin classes
and plain functions on a server; they become MCP tools, resources and prompts,
filtered by the calling user's permissions.

Early development: phases 1 (core), 2 (models) and 3 (admin). See
[docs/design.md](docs/design.md) for the full design and roadmap.

Guides: [getting started](docs/getting-started.md),
[permissions](docs/permissions.md), [security checklist](docs/security.md).
Changes per release: [changelog](CHANGELOG.md).

```python
# settings.py
INSTALLED_APPS = [..., "xprez_mcp"]  # plus one or both sign-in apps below

XPREZ_MCP = {
    # required: who may use the server at all
    "PERMISSION": "xprez_mcp.permissions.allow_staff",
}

# urls.py
from xprez_mcp import mcp

urlpatterns = [path("mcp/", mcp.urls)]
```

Clients sign in one of two ways; enable one or both (details in
[getting started](docs/getting-started.md#choose-how-clients-authenticate)).

**OAuth sign-in** (`xprez_mcp.contrib.oauth`): the user pastes
`https://<site>/mcp/` into the assistant (claude.ai and ChatGPT connectors,
Claude Code, Cursor), signs in with the site's login and clicks "Allow".

1. Add `"xprez_mcp.contrib.oauth"` to `INSTALLED_APPS` and run `migrate`.
2. Add `path("", include("xprez_mcp.contrib.oauth.well_known"))` to the root
   `urls.py`.
3. Set `XPREZ_MCP["AUTHENTICATION"] = ["xprez_mcp.contrib.oauth.auth.access_token"]`.

Override `xprez_mcp/oauth/base.html` to style the consent page, and schedule
`manage.py mcp_oauth_cleanup` daily.

**Bearer tokens** (`xprez_mcp.contrib.tokens`): the user creates a key under
"MCP tokens" in the admin and the client sends `Authorization: Bearer <key>`.
Suits scripts, CI and clients without OAuth.

1. Add `"xprez_mcp.contrib.tokens"` to `INSTALLED_APPS` and run `migrate`.
2. Nothing else: `xprez_mcp.auth.bearer_token` is the default
   `AUTHENTICATION`.

Keys are shown once, stored hashed and revoked by deleting. Projects with
their own token model leave the app out and set
`XPREZ_MCP["TOKEN_RESOLVER"] = "myapp.tokens.resolve"`
(`(request, token) -> user | None`).

**Both:** list OAuth first,
`["xprez_mcp.contrib.oauth.auth.access_token", "xprez_mcp.auth.bearer_token"]`,
since `bearer_token` rejects keys it does not know.

```python
# myapp/mcp.py: discovered automatically, like admin.py
from xprez_mcp import Provider, mcp, prompt, resource, tool


@mcp.register
class NotesProvider(Provider):
    namespace = "notes"

    @tool(read_only=True, permission=lambda ctx: ctx.user.has_perm("myapp.view_note"))
    def search(self, ctx, query: str, limit: int = 20):
        """Search notes by title."""
        notes = Note.objects.filter(owner=ctx.user, title__icontains=query)
        return {"notes": list(notes.values("id", "title")[:limit])}

    @resource("notes://{id}", mime_type="text/markdown")
    def note(self, ctx, id: int):
        return Note.objects.get(owner=ctx.user, pk=id).text

    @prompt
    def summary(self, ctx, id: int):
        """Summarize one note."""
        return f"Summarize the note notes://{id}."


@mcp.tool
def ping(ctx) -> str:
    return "pong"
```

```python
# myapp/mcp.py: tools generated from a model
from xprez_mcp import ModelProvider, mcp


@mcp.register(Note)
class NoteProvider(ModelProvider):
    fields = ["id", "title", "created"]
    search_fields = ["title"]
    filters = {"created_after": "created__gte"}
    actions = ["list", "get", "update"]  # notes_list, notes_get, notes_update

    def get_queryset(self, ctx):
        return Note.objects.filter(owner=ctx.user)
```

With many models, `MCPServer(exposure="generic")` exposes them through a few
shared tools (`models_list`, `records_search`, `record_get`, ...) instead of
tools per model. A provider can choose for itself with `exposure`.

```python
# the admin, with exactly the permissions each user has there
from django.contrib import admin
from xprez_mcp import AdminProvider, mcp


@mcp.register(Note)
class NoteAdminProvider(AdminProvider):  # uses the registered NoteAdmin
    actions = ["list", "get", "update"]  # writes are opt-in
    admin_actions = ["publish"]          # ModelAdmin actions, opt-in


# the rest of admin.site (models registered above keep their own provider)
mcp.register_admin(admin.site, exposure="generic")
```

Write tools from installed third-party apps are held back until listed in
`XPREZ_MCP["ALLOW_WRITES"]`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m django test xprez_mcp.tests --settings=xprez_mcp.tests.settings
.venv/bin/black --check .
```

Tests run on SQLite in memory. To run them on PostgreSQL, install
`psycopg[binary]` and set `POSTGRES_HOST` (plus `POSTGRES_PORT`, `POSTGRES_DB`,
`POSTGRES_USER`, `POSTGRES_PASSWORD` as needed).
