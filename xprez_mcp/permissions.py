"""Permissions: a callable ``f(ctx) -> bool``, ``True`` or ``False``, the same
for the server, items and model actions. A Django permission is checked in
the callable, e.g. ``lambda ctx: ctx.user is not None and
ctx.user.has_perm("crm.use_mcp")`` (``ctx.user`` is None on anonymous calls).

``XPREZ_MCP["PERMISSION"]`` also takes a dotted path to such a callable,
imported when first needed. Nothing here reads settings or models at import
time, so settings can import ``allow_staff`` and ``allow_authenticated``.
"""

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

SETTING = 'XPREZ_MCP["PERMISSION"]'


def allow_authenticated(ctx):
    user = ctx.user
    return user is not None and user.is_authenticated


def allow_staff(ctx):
    # custom user models need not have is_staff
    return allow_authenticated(ctx) and getattr(ctx.user, "is_staff", False)


def validate_permission(permission, where="permission"):
    """``permission`` unchanged when it is None, a bool or a callable;
    anything else (a string, a list) raises ``ImproperlyConfigured``."""
    if permission is None or isinstance(permission, bool) or callable(permission):
        return permission
    note = (
        f" A dotted path works only in {SETTING}."
        if isinstance(permission, str)
        else ""
    )
    raise ImproperlyConfigured(
        f"Invalid {where} {permission!r}: use True, False or a function "
        "f(ctx) -> bool, such as xprez_mcp.permissions.allow_staff. To require "
        "a Django permission: lambda ctx: ctx.user is not None and "
        f"ctx.user.has_perm('app_label.codename').{note}"
    )


def permission_from_setting(permission):
    """``XPREZ_MCP["PERMISSION"]``: what ``validate_permission`` accepts, or a
    dotted path to a callable, imported here rather than at settings load."""
    if not isinstance(permission, str):
        return validate_permission(permission, SETTING)
    try:
        target = import_string(permission)
    except ImportError as error:
        if permission.count(".") == 1:  # app_label.codename
            raise ImproperlyConfigured(
                f"{SETTING} {permission!r} looks like a Django permission, not "
                "a path to a function. Check it in a function instead: "
                "lambda ctx: ctx.user is not None and "
                f"ctx.user.has_perm({permission!r})."
            ) from error
        raise ImproperlyConfigured(
            f"{SETTING} {permission!r} cannot be imported ({error}). Use True, "
            "False, a function f(ctx) -> bool or a dotted path to one, such as "
            '"xprez_mcp.permissions.allow_staff".'
        ) from error
    if not callable(target):
        raise ImproperlyConfigured(
            f"{SETTING} {permission!r} is {target!r}, not a function f(ctx) -> bool."
        )
    return target


def allows(permission, ctx):
    """Whether ``permission`` (validated) lets ``ctx`` through; None allows."""
    if permission is None or isinstance(permission, bool):
        return permission is not False
    return bool(permission(ctx))
