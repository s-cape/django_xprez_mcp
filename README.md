# django-xprez-mcp

Admin-style MCP servers for Django. Register providers, models, admin classes
and plain functions on a server; they become MCP tools, resources and prompts,
filtered by the calling user's permissions.

Early development: phases 1 (core), 2 (models) and 3 (admin). See
[docs/design.md](docs/design.md) for the full design and roadmap.

```python
# settings.py
INSTALLED_APPS = [..., "xprez_mcp", "xprez_mcp.contrib.tokens"]
XPREZ_MCP = {
    "PERMISSION": "myapp.use_mcp",  # required: who may use the server at all
}

# urls.py
from xprez_mcp import mcp

urlpatterns = [path("mcp/", mcp.urls)]
```

Clients send `Authorization: Bearer <key>`. With `xprez_mcp.contrib.tokens`
users create keys under "MCP tokens" in the admin (shown once, stored hashed,
revoked by deleting). Projects with their own token model leave the app out
and set `XPREZ_MCP["TOKEN_RESOLVER"] = "myapp.tokens.resolve"`
(`(request, token) -> user | None`).

```python
# myapp/mcp.py: discovered automatically, like admin.py
from xprez_mcp import Provider, mcp, prompt, resource, tool


@mcp.register
class NotesProvider(Provider):
    namespace = "notes"

    @tool(read_only=True, permission="myapp.view_note")
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

With many models, `MCPServer(models="generic")` exposes them through a few
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
