"""Authentication backends: ``backend(request) -> user | None``.

A backend returns None to let the next one try, or raises
``AuthenticationFailed`` to reject the request.
"""

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from . import conf
from .errors import AuthenticationFailed


def session(request):
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user
    return None


class BearerToken:
    """``Authorization: Bearer <token>`` resolved by ``resolver(request, token)``.

    Without an explicit resolver, ``MCPKIT["TOKEN_RESOLVER"]`` is used.
    """

    def __init__(self, resolver=None):
        self.resolver = resolver

    def get_resolver(self):
        if self.resolver is not None:
            return self.resolver
        path = conf.get("TOKEN_RESOLVER")
        if not path:
            raise ImproperlyConfigured(
                'Bearer authentication needs MCPKIT["TOKEN_RESOLVER"].'
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
