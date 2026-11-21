"""A project's server permission: a Django permission checked in a function."""


def can_view_users(ctx):
    return ctx.user is not None and ctx.user.has_perm("auth.view_user")


NOT_CALLABLE = "auth.view_user"
