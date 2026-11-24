import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

PREFIX = "mcp"


class TokenManager(models.Manager):
    def create_token(self, user, name, expires=None):
        """``(token, key)``: the key exists only here, the database keeps its hash."""
        token = self.model(user=user, name=name, expires=expires)
        key = token.set_key()
        token.save()
        return token, key


class Token(models.Model):
    """A bearer token ``mcp_<key_id>_<secret>``; only its SHA-256 hash is stored."""

    mcp_exposed = False  # never through AdminProvider: MCP clients don't manage keys

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mcp_tokens"
    )
    name = models.CharField(
        max_length=100, help_text="Where it is used, e.g. a laptop."
    )
    key_id = models.CharField(max_length=16, unique=True, editable=False)
    key_hash = models.CharField(max_length=64, editable=False)
    created = models.DateTimeField(default=timezone.now, editable=False)
    expires = models.DateTimeField(null=True, blank=True)
    last_used = models.DateTimeField(null=True, blank=True, editable=False)

    objects = TokenManager()

    class Meta:
        ordering = ["-created"]
        verbose_name = "MCP token"
        verbose_name_plural = "MCP tokens"
        permissions = [("manage_others_token", "Can manage other users' tokens")]

    def __str__(self):
        return self.name

    def set_key(self):
        """A new key for this token; returned once, never stored."""
        self.key_id = secrets.token_hex(6)
        key = f"{PREFIX}_{self.key_id}_{secrets.token_urlsafe(32)}"
        self.key_hash = hash_key(key)
        return key

    @property
    def masked_key(self):
        return f"{PREFIX}_{self.key_id}_…" if self.key_id else ""

    masked_key.fget.short_description = "Key"

    def is_expired(self, now=None):
        return self.expires is not None and self.expires <= (now or timezone.now())


def hash_key(key):
    # keys are random and long, so a fast hash is enough (no password hashing)
    return hashlib.sha256(key.encode()).hexdigest()
