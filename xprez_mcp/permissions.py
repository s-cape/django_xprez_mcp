def check_permission(permission, ctx):
    """``permission`` is None, a permission string, a list of them, or ``f(ctx)``."""
    if permission is None:
        return True
    if callable(permission):
        return bool(permission(ctx))
    user = ctx.user
    if user is None:
        return False
    perms = (permission,) if isinstance(permission, str) else tuple(permission)
    return user.has_perms(perms)
