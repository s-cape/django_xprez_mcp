from django.core.exceptions import ImproperlyConfigured


def allow_any(ctx):
    return True


def allow_authenticated(ctx):
    user = ctx.user
    return user is not None and user.is_authenticated


def allow_staff(ctx):
    return allow_authenticated(ctx) and ctx.user.is_staff


def check_permission(permission, ctx):
    """``permission`` is None, a callable ``f(ctx) -> bool``, or Django permission
    names (``"app_label.codename"`` or a list of them, all required)."""
    if callable(permission):
        return bool(permission(ctx))
    return check_permission_names(permission, ctx)


def check_permission_names(permission, ctx):
    """Like ``check_permission`` but only for Django permission names."""
    if permission is None:
        return True
    if callable(permission):
        raise ImproperlyConfigured(
            f"Invalid permission {permission!r}: only Django permission names "
            "here; pass the callable as permission_callback."
        )
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
