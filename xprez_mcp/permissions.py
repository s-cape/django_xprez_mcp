from django.core.exceptions import ImproperlyConfigured


def allow_any(ctx):
    return True


def allow_authenticated(ctx):
    user = ctx.user
    return user is not None and user.is_authenticated


def allow_staff(ctx):
    return allow_authenticated(ctx) and ctx.user.is_staff


# Django permission names always contain a dot, so these cannot clash.
SHORTHANDS = {
    "any": allow_any,
    "authenticated": allow_authenticated,
    "staff": allow_staff,
}


def check_permission(permission, ctx):
    """``permission`` is None, a permission string, a list of them, ``f(ctx)``,
    or one of the shorthands ``"any"``, ``"authenticated"``, ``"staff"``."""
    if permission is None:
        return True
    if callable(permission):
        return bool(permission(ctx))
    perms = (permission,) if isinstance(permission, str) else tuple(permission)
    for perm in perms:
        if "." not in perm:
            if perm not in SHORTHANDS:
                raise ImproperlyConfigured(
                    f"Unknown permission {perm!r}: use 'app_label.codename' or "
                    f"one of {sorted(SHORTHANDS)}."
                )
            if not SHORTHANDS[perm](ctx):
                return False
    names = [perm for perm in perms if "." in perm]
    if not names:
        return True
    user = ctx.user
    if user is None:
        return False
    return user.has_perms(names)
