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
    # and one or both ways to sign in, see "Choose how clients authenticate"
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

`PERMISSION` decides who may use the server: a dotted path to a function
(like `AUTHENTICATION`), the function itself, `True` or `False`. It has no
default: without it, `manage.py check` fails with `xprez_mcp.E001` and the
endpoint answers `403`. A path that does not import fails the check too
(`xprez_mcp.E004`).

| `PERMISSION` | Who gets in |
|---|---|
| `"xprez_mcp.permissions.allow_staff"` | staff users |
| `"xprez_mcp.permissions.allow_authenticated"` | every authenticated user |
| `lambda ctx: ctx.user.has_perm("myapp.use_mcp")` | users with that Django permission |
| `"myapp.mcp.can_connect"`, a function of your own | whoever it returns `True` for |
| `True` | everyone who authenticates (and anonymous callers with `allow_anonymous=True`) |
| `False` | nobody: the server is closed on purpose |

A function takes the call context and returns a boolean. Name your own by
its dotted path; it is imported when first needed, so its module may import
models:

```python
# myapp/mcp.py
def can_connect(ctx):
    return ctx.user.is_staff and ctx.user.has_perm("myapp.use_mcp")


# settings.py
XPREZ_MCP = {
    "PERMISSION": "myapp.mcp.can_connect",
}
```

A Django permission name is not a path: `"myapp.use_mcp"` fails the check
with a hint to wrap it in `lambda ctx: ctx.user.has_perm("myapp.use_mcp")`.

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

A server of your own takes a function, `True` or `False` (in code, no
strings):

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

## Choose how clients authenticate

Two options, each an optional app. Enable one or both.

| | OAuth sign-in | Bearer tokens |
|---|---|---|
| App | `xprez_mcp.contrib.oauth` | `xprez_mcp.contrib.tokens` |
| The user | pastes the server URL, signs in with the site's login, clicks "Allow" | creates a key in the admin and pastes it into the client |
| Suits | assistants that sign in: claude.ai and ChatGPT connectors, Claude Code, Cursor, MCP Inspector | scripts, CI, clients without OAuth, `curl` |
| Lifetime | access tokens 1 h, refresh tokens rotated on every use | until it expires (`expires`) or is deleted |
| Revoking | "Disconnect" per connected client | deleting the token |

Either way, only hashes are stored, and the server `PERMISSION` decides
what a signed-in user may do.

### OAuth sign-in

```python
# settings.py
INSTALLED_APPS = [..., "xprez_mcp", "xprez_mcp.contrib.oauth"]
XPREZ_MCP = {
    "PERMISSION": "xprez_mcp.permissions.allow_staff",
    "AUTHENTICATION": ["xprez_mcp.contrib.oauth.auth.access_token"],
}
```

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    ...,
    path("", include("xprez_mcp.contrib.oauth.well_known")),
    path("mcp/", mcp.urls),
]
```

```bash
./manage.py migrate
```

The root include serves the metadata clients look for at
`/.well-known/...`. Sign-in uses the site's login (`LOGIN_URL`), so users
must be able to log in there. Then:

- Override `xprez_mcp/oauth/base.html` to give the consent page the site's
  layout; its strings are translatable.
- Show connected clients on the account page with
  `xprez_mcp.contrib.oauth.views.ConnectionsView`, or with
  `connections.for_user()` / `connections.disconnect()` in a view of your
  own.
- Schedule `./manage.py mcp_oauth_cleanup` daily (cron, a systemd timer,
  Celery beat): nothing runs it for you. Without it, expired rows and
  clients that registered but never connected pile up.
- Behind a proxy, make sure the request knows the public `https://` origin
  (`SECURE_PROXY_SSL_HEADER`, or `XPREZ_MCP["OAUTH"]["BASE_URL"]`):
  clients compare it with the URL they were given.

### Bearer tokens

```python
# settings.py
INSTALLED_APPS = [..., "xprez_mcp", "xprez_mcp.contrib.tokens"]
```

```bash
./manage.py migrate
```

`xprez_mcp.auth.bearer_token` is the default `AUTHENTICATION`, so nothing
else is needed. A user opens "MCP tokens" in the admin and adds one. The key
(`mcp_...`) is shown once, after "Save"; only its hash is stored. Deleting
the token revokes it.

Projects with their own token model leave the app out and set
`XPREZ_MCP["TOKEN_RESOLVER"] = "myapp.tokens.resolve"`, a callable
`(request, token) -> user | None`.

### Both

Install both apps, include the well-known URLs, and list OAuth first:
`bearer_token` rejects keys it does not know, OAuth access tokens included
(`manage.py check` warns with `xprez_mcp_oauth.W002` otherwise).

```python
XPREZ_MCP = {
    "PERMISSION": "xprez_mcp.permissions.allow_staff",
    "AUTHENTICATION": [
        "xprez_mcp.contrib.oauth.auth.access_token",
        "xprez_mcp.auth.bearer_token",
    ],
}
```

## Connect a client

The endpoint is plain HTTP (the "Streamable HTTP" transport, JSON responses
only) at `https://example.com/mcp/`.

### With OAuth

Give the client only the URL. It finds the sign-in on its own, opens the
site's login and consent page, and keeps its tokens fresh.

- claude.ai: add `https://example.com/mcp/` as a custom connector.
- Claude Code: `claude mcp add --transport http myproject https://example.com/mcp/`,
  then sign in when it asks.
- Cursor, MCP Inspector and other OAuth-capable clients: the URL, without a
  header.

### With a bearer token

Clients send the key as `Authorization: Bearer <key>`.

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
  how OAuth works, settings.
