from datetime import timedelta

from django.contrib import auth
from django.urls import NoReverseMatch
from django.utils import timezone

from xprez_mcp.errors import AuthenticationFailed

from . import resources
from .models import ACCESS_PREFIX, AccessToken, Grant, hash_key


class AccessTokenBackend:
    """``Authorization: Bearer mcpoat_…``: an OAuth access token issued for
    this endpoint. Other bearer tokens are left to the next backend, so list
    this one before ``xprez_mcp.auth.bearer_token``. Sets
    ``request.mcp_oauth_grant``.
    """

    last_used_interval = timedelta(minutes=1)

    def get_user(self, request, user_id):
        """The first of ``AUTHENTICATION_BACKENDS`` that has the user."""
        for backend in auth.get_backends():
            get_user = getattr(backend, "get_user", None)
            user = get_user(user_id) if get_user else None
            if user is not None:
                return user
        return None

    def __call__(self, request):
        scheme, _, key = request.headers.get("Authorization", "").partition(" ")
        key = key.strip()
        if scheme.lower() != "bearer" or not key.startswith(ACCESS_PREFIX):
            return None
        token = (
            AccessToken.objects.select_related("grant")
            .filter(key_hash=hash_key(key))
            .first()
        )
        now = timezone.now()
        if token is None or token.expires <= now:
            raise AuthenticationFailed("Invalid token.")
        grant = token.grant
        # the audience: a token for another endpoint is not accepted here
        here = resources.base_url(request) + request.path
        if resources.canonical(grant.resource) != resources.canonical(here):
            raise AuthenticationFailed("Invalid token.")
        user = self.get_user(request, grant.user_id)
        if user is None or not user.is_active:
            raise AuthenticationFailed("Invalid token.")
        if grant.last_used is None or now - grant.last_used >= self.last_used_interval:
            Grant.objects.filter(pk=grant.pk).update(last_used=now)
        request.mcp_oauth_grant = grant
        return user

    def challenge(self, request, error=None):
        """Points clients to the protected resource metadata (RFC 9728)."""
        try:
            url = resources.url(request, "oauth-resource-metadata")
        except (AttributeError, NoReverseMatch):
            return None
        challenge = f'Bearer resource_metadata="{url}"'
        if error is not None:
            challenge += ', error="invalid_token"'
        return challenge


access_token = AccessTokenBackend()
