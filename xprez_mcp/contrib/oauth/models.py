"""OAuth clients, the user's connections (grants) and their codes and tokens.

Codes and tokens are random, so only their SHA-256 hash is stored, as in
``xprez_mcp.contrib.tokens``.
"""

import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import models
from django.utils import timezone

ACCESS_PREFIX = "mcpoat_"
REFRESH_PREFIX = "mcport_"


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(prefix=""):
    """``(key, hash)``: the key is returned once, the hash is stored."""
    key = prefix + secrets.token_urlsafe(32)
    return key, hash_key(key)


def expiry(seconds):
    return timezone.now() + timedelta(seconds=seconds)


class ClientManager(models.Manager):
    def create_client(self, name, redirect_uris, public=True):
        """``(client, secret)`` of a pre-registered client; the secret (None
        for a public client) exists only here, the database keeps its hash."""
        client = self.model(
            client_id=f"mcpc_{secrets.token_urlsafe(16)}",
            kind=self.model.MANUAL,
            name=name,
            redirect_uris=list(redirect_uris),
            auth_method="none" if public else "client_secret_basic",
        )
        secret = None if public else client.set_secret()
        client.save()
        return client, secret


class Client(models.Model):
    METADATA_DOCUMENT = "metadata"
    REGISTERED = "registered"
    MANUAL = "manual"
    KINDS = [
        (METADATA_DOCUMENT, "Client ID metadata document"),
        (REGISTERED, "Dynamic registration"),
        (MANUAL, "Manual"),
    ]

    mcp_exposed = False

    client_id = models.CharField(max_length=500, unique=True)
    kind = models.CharField(max_length=20, choices=KINDS)
    name = models.CharField(max_length=200, blank=True)
    redirect_uris = models.JSONField(default=list)
    # empty for public clients (token_endpoint_auth_method "none")
    secret_hash = models.CharField(max_length=64, blank=True, editable=False)
    auth_method = models.CharField(max_length=40, default="none")
    metadata = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(default=timezone.now, editable=False)
    last_used = models.DateTimeField(null=True, blank=True, editable=False)
    # when a metadata document is fetched again
    refresh_after = models.DateTimeField(null=True, blank=True, editable=False)

    objects = ClientManager()

    class Meta:
        ordering = ["-created"]
        verbose_name = "MCP OAuth client"
        verbose_name_plural = "MCP OAuth clients"

    def __str__(self):
        return self.name or self.client_id

    @property
    def is_public(self):
        return self.auth_method == "none"

    @property
    def host(self):
        """Who vouches for the client: the metadata document's host."""
        if self.kind == self.METADATA_DOCUMENT:
            return urlsplit(self.client_id).hostname
        return None

    def set_secret(self):
        secret, self.secret_hash = new_key()
        return secret

    def check_secret(self, secret):
        return bool(self.secret_hash) and secrets.compare_digest(
            hash_key(secret), self.secret_hash
        )


class Grant(models.Model):
    """One connection: a user allowed a client to use one MCP endpoint.
    Deleting it disconnects the client (its tokens go with it)."""

    mcp_exposed = False

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mcp_oauth_grants",
    )
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="grants")
    resource = models.CharField(max_length=500)
    created = models.DateTimeField(default=timezone.now, editable=False)
    last_used = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-created"]
        verbose_name = "MCP connection"
        verbose_name_plural = "MCP connections"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "client", "resource"],
                name="xprez_mcp_oauth_grant_unique",
            )
        ]

    def __str__(self):
        return f"{self.client} ({self.user})"


class AuthorizationCode(models.Model):
    mcp_exposed = False

    code_hash = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    resource = models.CharField(max_length=500)
    # as sent in the authorization request ("" if left out)
    redirect_uri = models.CharField(max_length=2000, blank=True)
    code_challenge = models.CharField(max_length=128)
    expires = models.DateTimeField()
    used = models.BooleanField(default=False)
    # set when exchanged: a code used twice revokes what it issued
    grant = models.ForeignKey(Grant, null=True, on_delete=models.CASCADE)


class AccessToken(models.Model):
    mcp_exposed = False

    key_hash = models.CharField(max_length=64, unique=True)
    grant = models.ForeignKey(
        Grant, on_delete=models.CASCADE, related_name="access_tokens"
    )
    expires = models.DateTimeField()


class RefreshToken(models.Model):
    """Rotated on every use; presenting a used one again (after
    ``REFRESH_REUSE_INTERVAL``) revokes the grant."""

    mcp_exposed = False

    key_hash = models.CharField(max_length=64, unique=True)
    grant = models.ForeignKey(
        Grant, on_delete=models.CASCADE, related_name="refresh_tokens"
    )
    expires = models.DateTimeField()
    used = models.DateTimeField(null=True, blank=True)
