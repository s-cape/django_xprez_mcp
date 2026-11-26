import hashlib
import posixpath
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import conf

KEY_PREFIX = "mcpup_"


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


class Upload(models.Model):
    """One file a client was asked to ``PUT``. ``path`` is the client's label
    within the batch, never a storage name; the file is stored as
    ``stored_name`` (random) until a tool uses it."""

    mcp_exposed = False

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mcp_uploads",
    )
    batch = models.CharField(max_length=32, db_index=True)
    path = models.CharField(max_length=500)
    size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    # what the batch accepts, and what the received file turned out to be
    content_types = models.JSONField(default=list)
    content_type = models.CharField(max_length=100, blank=True)
    # in the URL: not secret; the key goes in the Authorization header
    key_id = models.CharField(max_length=16, unique=True, editable=False)
    key_hash = models.CharField(max_length=64, editable=False)
    created = models.DateTimeField(default=timezone.now, editable=False)
    expires = models.DateTimeField(db_index=True)
    received_at = models.DateTimeField(null=True, blank=True, db_index=True)
    stored_name = models.CharField(max_length=200, blank=True, editable=False)
    failure = models.CharField(max_length=300, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["batch", "path"]
        verbose_name = "MCP upload"
        verbose_name_plural = "MCP uploads"
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "path"], name="xprez_mcp_upload_batch_path"
            )
        ]

    def __str__(self):
        return self.path

    def set_key(self):
        """A new key; returned once, only its hash is stored."""
        self.key_id = secrets.token_hex(8)
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        self.key_hash = hash_key(key)
        return key

    def check_key(self, key):
        return bool(key) and secrets.compare_digest(hash_key(key), self.key_hash)

    @property
    def name(self):
        """The file name: the last part of ``path``."""
        return posixpath.basename(self.path)

    def is_expired(self, now=None):
        """The URL expired before the file came, or the file waited too long."""
        now = now or timezone.now()
        if self.received_at is None:
            return self.expires <= now
        return self.received_at <= now - timedelta(seconds=conf.get("RETAIN"))

    @property
    def state(self):
        """``"failed"``, ``"consumed"``, ``"expired"``, ``"received"`` or ``"missing"``."""
        if self.failure:
            return "failed"
        if self.consumed_at is not None:
            return "consumed"
        if self.is_expired():
            return "expired"
        return "received" if self.received_at is not None else "missing"

    def open(self):
        """The received file, binary, from the staging storage."""
        if not self.stored_name:
            raise ValueError(f"{self.path} has no file.")
        return conf.get_storage().open(self.stored_name, "rb")
