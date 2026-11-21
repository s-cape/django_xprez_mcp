"""Permissions: a callable ``f(ctx) -> bool``, ``True`` or ``False``, the same
for the server, items and model actions. A Django permission is checked in
the callable, e.g. ``lambda ctx: ctx.user is not None and
ctx.user.has_perm("crm.use_mcp")`` (``ctx.user`` is None on anonymous calls).

Nothing here reads settings or models at import time, so settings can import
``allow_staff`` and ``allow_authenticated``.
"""

from django.core.exceptions import ImproperlyConfigured


def allow_authenticated(ctx):
    user = ctx.user
    return user is not None and user.is_authenticated


def allow_staff(ctx):
    return allow_authenticated(ctx) and ctx.user.is_staff


def validate_permission(permission, where="permission"):
    """``permission`` unchanged when it is None, a bool or a callable;
    anything else (a string, a list) raises ``ImproperlyConfigured``."""
    if permission is None or isinstance(permission, bool) or callable(permission):
        return permission
    raise ImproperlyConfigured(
        f"Invalid {where} {permission!r}: use True, False or a function "
        "f(ctx) -> bool, such as xprez_mcp.permissions.allow_staff. To require "
        "a Django permission: lambda ctx: ctx.user is not None and "
        "ctx.user.has_perm('app_label.codename')."
    )


def allows(permission, ctx):
    """Whether ``permission`` (validated) lets ``ctx`` through; None allows."""
    if permission is None or isinstance(permission, bool):
        return permission is not False
    return bool(permission(ctx))
