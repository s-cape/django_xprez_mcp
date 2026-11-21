from django.core.exceptions import ImproperlyConfigured


def allow_authenticated(ctx):
    user = ctx.user
    return user is not None and user.is_authenticated


def allow_staff(ctx):
    return allow_authenticated(ctx) and ctx.user.is_staff


def check_permission(permission, ctx):
    """``permission`` is None, a callable ``f(ctx) -> bool``, or Django permission
    names (``"app_label.codename"`` or a list of them, all required)."""
    if permission is None:
        return True
    if callable(permission):
        return bool(permission(ctx))
    names = [permission] if isinstance(permission, str) else list(permission)
    for name in names:
        if not isinstance(name, str) or "." not in name:
            raise ImproperlyConfigured(
                f"Invalid permission {name!r}: strings are Django permission "
                "names ('app_label.codename'); pass a callable for other checks, "
                "e.g. xprez_mcp.permissions.allow_staff."
            )
    if not names:
        return True
    user = ctx.user
    if user is None:
        return False
    return user.has_perms(names)


def resolve_server_permission(permission):
    """A server's permission as a callable: ``f(ctx) -> bool``, ``True``
    (everyone), ``False`` (nobody, on purpose) or None (nobody, reported by
    the system check).

    No strings: a Django permission name is checked in a function, e.g.
    ``lambda ctx: ctx.user.has_perm("crm.use_mcp")``.
    """
    if permission is True:
        return _everyone
    if permission is False:
        return _nobody
    if permission is None or callable(permission):
        return permission
    raise ImproperlyConfigured(
        f"Invalid server permission {permission!r}: use True, False or a "
        "function f(ctx) -> bool, such as xprez_mcp.permissions.allow_staff. "
        "To require a Django permission: "
        "lambda ctx: ctx.user.has_perm('app_label.codename')."
    )


def _everyone(ctx):
    return True


def _nobody(ctx):
    return False
