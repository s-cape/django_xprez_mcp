from datetime import timedelta

from django.contrib import auth
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from .models import PREFIX, Token, hash_key


class TokenResolver:
    """``resolver(request, key) -> user | None`` for ``Token`` keys.

    A key works until it expires or is deleted, and the user is
    loaded as the session would load them. Sets ``request.mcp_token``
    (``request`` may be None outside a request).
    """

    last_used_interval = timedelta(minutes=1)

    def get_user(self, request, token):
        """The first of ``AUTHENTICATION_BACKENDS`` that has the user."""
        for backend in auth.get_backends():
            get_user = getattr(backend, "get_user", None)
            user = get_user(token.user_id) if get_user else None
            if user is not None:
                return user
        return None

    def __call__(self, request, key):
        head, _, rest = key.partition("_")
        key_id, _, secret = rest.partition("_")
        if head != PREFIX or not key_id or not secret:
            return None
        token = Token.objects.filter(key_id=key_id).first()
        if token is None or not constant_time_compare(hash_key(key), token.key_hash):
            return None
        now = timezone.now()
        if token.is_expired(now):
            return None
        user = self.get_user(request, token)
        if user is None:
            return None
        if token.last_used is None or now - token.last_used >= self.last_used_interval:
            Token.objects.filter(pk=token.pk).update(last_used=now)
        if request is not None:
            request.mcp_token = token
        return user


resolve = TokenResolver()
