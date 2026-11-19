"""Authentication backends: ``backend(request) -> user | None``.

A backend returns None to let the next one try, or raises
``AuthenticationFailed`` to reject the request.
"""

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from . import conf
from .errors import AuthenticationFailed

TOKENS_APP = "xprez_mcp.contrib.tokens"


def session(request):
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user
    return None


class BearerToken:
    """``Authorization: Bearer <token>`` resolved by ``resolver(request, token)``.

    Without an explicit resolver, ``XPREZ_MCP["TOKEN_RESOLVER"]`` is used, and
    without that the stored tokens of ``xprez_mcp.contrib.tokens`` if installed.
    """

    def __init__(self, resolver=None):
        self.resolver = resolver

    def get_resolver(self):
        if self.resolver is not None:
            return self.resolver
        path = conf.get("TOKEN_RESOLVER")
        if not path and apps.is_installed(TOKENS_APP):
            path = f"{TOKENS_APP}.auth.resolve"
        if not path:
            raise ImproperlyConfigured(
                'Bearer authentication needs XPREZ_MCP["TOKEN_RESOLVER"] or '
                f"{TOKENS_APP!r} in INSTALLED_APPS."
            )
        return import_string(path)

    def __call__(self, request):
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        user = self.get_resolver()(request, token)
        if user is None or not user.is_active:
            raise AuthenticationFailed("Invalid token.")
        return user


bearer_token = BearerToken()
