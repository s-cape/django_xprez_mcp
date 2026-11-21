from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string


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
    """A server's permission as a callable: ``f(ctx) -> bool``, a dotted path to
    one, ``True`` (everyone), ``False`` (nobody, on purpose) or None (nobody,
    reported by the system check).

    Only a function: in settings a Django permission name reads like a dotted
    path, so names are checked in a function of the project's own, e.g.
    ``def can_use(ctx): return ctx.user.has_perm("crm.use_mcp")``.
    """
    if permission is True:
        return _everyone
    if permission is False:
        return _nobody
    if permission is None or callable(permission):
        return permission
    hint = (
        "The server permission is True, False, a function f(ctx) -> bool or its "
        "dotted path, such as 'xprez_mcp.permissions.allow_staff'. To require a Django "
        "permission, write one: def can_use(ctx): return "
        "ctx.user.has_perm('app_label.codename')."
    )
    if not isinstance(permission, str):
        raise ImproperlyConfigured(f"Invalid server permission {permission!r}. {hint}")
    try:
        function = import_string(permission)
    except ImportError:
        raise ImproperlyConfigured(
            f"Cannot import the server permission {permission!r}. {hint}"
        ) from None
    if not callable(function):
        raise ImproperlyConfigured(f"{permission!r} is not callable. {hint}")
    return function


def _everyone(ctx):
    return True


def _nobody(ctx):
    return False
