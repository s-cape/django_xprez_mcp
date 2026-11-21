# Getting started

This guide takes a Django project from nothing to an MCP server that an
agent can connect to. [design.md](design.md) is the full reference.

## Install

```bash
pip install django-xprez-mcp
```

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "xprez_mcp",
    "xprez_mcp.contrib.tokens",  # optional: bearer tokens managed in the admin
]

XPREZ_MCP = {
    # required: who may use the server at all
    "PERMISSION": "xprez_mcp.permissions.allow_staff",
}
```

```python
# urls.py
from django.urls import path
from xprez_mcp import mcp

urlpatterns = [
    ...,
    path("mcp/", mcp.urls),
]
```

```bash
./manage.py migrate
```

`PERMISSION` is a function deciding who may use the server, given by its
dotted path, or `True` / `False`. It has no default: without it, `manage.py check` fails with
`xprez_mcp.E001` and the endpoint answers `403`.

| `PERMISSION` | Who gets in |
|---|---|
| `"xprez_mcp.permissions.allow_staff"` | staff users |
| `"xprez_mcp.permissions.allow_authenticated"` | every authenticated user |
| `"myapp.mcp_access.can_use_mcp"` | whoever your function returns `True` for |
| `True` | everyone who authenticates (and anonymous callers with `allow_anonymous=True`) |
| `False` | nobody: the server is closed on purpose |

A function of your own takes the call context and returns a boolean. To
require a Django permission, check it there:

```python
# myapp/mcp_access.py
def can_use_mcp(ctx):
    return ctx.user.is_staff and ctx.user.has_perm("myapp.use_mcp")
```

A Django permission (`"<app_label>.<codename>"`) is granted in the admin like
any other; superusers have it implicitly. The library ships none, so declare
it on a model of your app and migrate:

```python
# myapp/models.py
class Note(models.Model):
    ...

    class Meta:
        permissions = [("use_mcp", "Can use the MCP server")]
```

A server of your own takes the function itself, or its dotted path:

```python
from xprez_mcp import MCPServer
from xprez_mcp.permissions import allow_staff

staff_mcp = MCPServer(name="staff", permission=allow_staff)
```

Users who fail the check get `403` when the client connects. Which tools
and rows they see past that point is covered in
[Permissions](permissions.md).

## Expose something

Create `mcp.py` in any app. It is discovered like `admin.py`.

```python
# myapp/mcp.py
from xprez_mcp import ModelProvider, Provider, mcp, tool

from .models import Note


@mcp.tool(read_only=True)
def ping(ctx) -> str:
    """Check the server is up."""
    return "pong"


@mcp.register(Note)
class NoteProvider(ModelProvider):
    fields = ["id", "title", "created"]
    search_fields = ["title"]

    def get_queryset(self, ctx):
        return Note.objects.filter(owner=ctx.user)
```

This gives `ping`, `notes_list` and `notes_get`. Writes (`create`,
`update`, `delete`) are opt-in through `actions`.

Every function and method receives `ctx` first: `ctx.user` is the caller,
`ctx.request` the Django request. Argument schemas come from type hints;
the docstring is the tool's description, which is what the agent reads, so
write it for the agent.

Check what the server exposes, and to whom:

```bash
./manage.py mcp_inspect
./manage.py mcp_inspect --user alice   # marks what alice cannot see
```

## Create a token

With `xprez_mcp.contrib.tokens`, a user opens "MCP tokens" in the admin and
adds one. The key (`mcp_...`) is shown once, after "Save"; only its hash is
stored. Deleting the token revokes it.

Projects with their own token model leave the app out and set
`XPREZ_MCP["TOKEN_RESOLVER"] = "myapp.tokens.resolve"`, a callable
`(request, token) -> user | None`.

## Connect a client

The endpoint is plain HTTP (the "Streamable HTTP" transport, JSON responses
only). Clients send the key as `Authorization: Bearer <key>`.

Cursor (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "myproject": {
      "url": "https://example.com/mcp/",
      "headers": {"Authorization": "Bearer mcp_..."}
    }
  }
}
```

Claude Code:

```bash
claude mcp add --transport http myproject https://example.com/mcp/ \
  --header "Authorization: Bearer mcp_..."
```

To try requests by hand:

```bash
curl -s https://example.com/mcp/ \
  -H "Authorization: Bearer mcp_..." \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'
```

## Next

- [Permissions](permissions.md): who sees which tools and rows.
- [Security](security.md): a checklist before exposing a server.
- [design.md](design.md): admin providers, generic mode, several servers,
  settings.
