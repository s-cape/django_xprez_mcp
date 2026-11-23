# Permissions

What a user cannot use, they cannot see. Each layer below can only narrow
the one before it; none can widen it. The reference is the Permissions
section of [design.md](design.md).

| Layer | Fails with | Configure with |
|---|---|---|
| Authentication | `401` | `AUTHENTICATION`, `TOKEN_RESOLVER` |
| Server permission | `403`, before `initialize` | `PERMISSION`, `MCPServer(permission=...)` (a function) |
| Provider | items hidden | `Provider.has_permission(ctx)` |
| Item or action | item hidden | `@tool(permission=...)`, `ModelProvider.permissions` |
| Rows | not found | `get_queryset(ctx)` |
| Object | readable `PermissionDenied` | `has_object_permission(ctx, action, instance)` |

Hidden items are left out of `tools/list` and answer as unknown when called,
so their existence does not leak. Check the result for a user with
`./manage.py mcp_inspect --user <username>`.

## Writing a permission

A permission is the same everywhere (server access, items, model actions):
a callable `f(ctx) -> bool`, `True` or `False`. No strings in code: check a
Django permission inside the callable,
`lambda ctx: ctx.user.has_perm("crm.change_client")`. The `PERMISSION`
setting also takes a dotted path to the callable, imported when first
needed. `xprez_mcp.permissions` ships `allow_staff` and `allow_authenticated`.
`ctx.user` is None on anonymous calls (`allow_anonymous=True`), so there
check it first: `ctx.user is not None and ctx.user.has_perm(...)`.

## Recipes

Only staff with a project permission may connect:

```python
# crm/mcp.py
def can_connect(ctx):
    return ctx.user.is_staff and ctx.user.has_perm("crm.use_mcp")


# settings.py
XPREZ_MCP = {
    "PERMISSION": "crm.mcp.can_connect",
}
```

A whole provider for one group:

```python
class ReportsProvider(Provider):
    def has_permission(self, ctx):
        return ctx.user.groups.filter(name="analysts").exists()
```

One tool behind a Django permission:

```python
@tool(destructive=True, permission=lambda ctx: ctx.user.has_perm("crm.delete_client"))
def purge(self, ctx, id: int): ...
```

Users see only their own rows:

```python
class NoteProvider(ModelProvider):
    def get_queryset(self, ctx):
        return Note.objects.filter(owner=ctx.user)
```

`ModelProvider` checks no Django model permissions by default; the server
permission and `get_queryset` are the gate. To follow the model permissions:

```python
class NoteProvider(ModelProvider):
    actions = ["list", "get", "update", "delete"]
    model_permissions = True  # view (or change) to read, add/change/delete to write
```

Or per action, and per object after the fetch:

```python
class NoteProvider(ModelProvider):
    actions = ["list", "get", "update"]
    permissions = {"update": lambda ctx: ctx.user.has_perm("notes.change_note")}

    def has_object_permission(self, ctx, action, instance):
        return action == "get" or instance.owner_id == ctx.user.pk
```

`create` is not limited by `get_queryset`. Enabling it without a permission
lets everyone who passes the server check create records.

## The admin

`AdminProvider` and `register_admin` add no rules of their own: a user can do
exactly what they can do in that admin (`has_view_permission`,
`has_change_permission(request, obj)`, `get_queryset`, `get_readonly_fields`,
action permissions and so on). Which actions exist as tools is still opt-in
(`list` and `get` by default), so granting a permission in the admin never
adds a write tool on its own.

## Writes

- Model providers expose `list` and `get` only, unless `actions` says more.
- Admin actions are exposed only when listed in `admin_actions`;
  `delete_selected` never is.
- Write tools from third-party apps are held back until the app is listed in
  `XPREZ_MCP["ALLOW_WRITES"]`.
- Credentials (`mcp_exposed = False`, set on `Token`) are never exposed.
