# django-mcpkit

Admin-style MCP servers for Django. Register providers, models, admin classes
and plain functions on a server; they become MCP tools, resources and prompts,
filtered by the calling user's permissions.

Early development: phase 1 (core) only. See [docs/design.md](docs/design.md)
for the full design and roadmap.

```python
# settings.py
INSTALLED_APPS = [..., "mcpkit"]
MCPKIT = {"TOKEN_RESOLVER": "myapp.tokens.resolve"}  # (request, token) -> user | None

# urls.py
from mcpkit import mcp

urlpatterns = [path("mcp/", mcp.urls)]
```

```python
# myapp/mcp.py: discovered automatically, like admin.py
from mcpkit import Provider, mcp, prompt, resource, tool


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

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m django test mcpkit.tests --settings=mcpkit.tests.settings
.venv/bin/black --check .
```
