"""Preparing uploads, reading batches, and using uploads as file arguments."""

import logging
import posixpath
import re
import secrets
from datetime import timedelta

from django.core.files import File
from django.db import router, transaction
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone

from xprez_mcp.errors import ToolError
from xprez_mcp.views import base_url

from . import conf
from .content import DEFAULT_CONTENT_TYPES
from .models import Upload

logger = logging.getLogger("xprez_mcp")

MAX_PATH = 500
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def normalize_path(path, label="path"):
    """A batch label like ``images/a.png``: relative, ``/``-separated, no
    ``..``, backslashes or control characters. Never used as a storage name."""
    if not isinstance(path, str) or not path:
        raise ToolError(f"{label} must be a non-empty string.")
    if len(path) > MAX_PATH:
        raise ToolError(f"{label} is longer than {MAX_PATH} characters.")
    if "\\" in path or _CONTROL.search(path):
        raise ToolError(f"{label} contains a backslash or a control character.")
    if path.startswith("/") or ".." in path.split("/"):
        raise ToolError(f"{label} must be a relative path without '..'.")
    normalized = posixpath.normpath(path)
    if normalized in ("", "."):
        raise ToolError(f"{label} must name a file.")
    return normalized


def prepare_uploads(
    ctx,
    files,
    *,
    content_types=None,
    max_size=None,
    max_files=None,
    ttl=None,
    batch=None,
):
    """One-time ``PUT`` URLs for ``files`` (``[{"path", "sha256", "size"}]``),
    as a dict to return from a tool.

    ``content_types`` (default: images, PDF and text) may use ``image/*``
    wildcards; SVG and HTML only by name. ``batch`` adds to (or replaces paths
    in) a batch of the same user. ``max_size``, ``max_files`` and ``ttl``
    default to the settings.
    """
    user = _user(ctx)
    max_size = conf.get("MAX_FILE_SIZE") if max_size is None else max_size
    max_files = conf.get("MAX_FILES") if max_files is None else max_files
    ttl = conf.get("TTL") if ttl is None else ttl
    if content_types is None:
        content_types = DEFAULT_CONTENT_TYPES
    if isinstance(content_types, str) or not all(
        isinstance(item, str) and item for item in content_types
    ):
        raise TypeError("content_types must be a list of content types.")
    content_types = list(content_types)
    entries = _entries(files, max_size)
    now = timezone.now()
    expires = now + timedelta(seconds=ttl)
    using = router.db_for_write(Upload)
    with transaction.atomic(using=using):
        if batch is None:
            batch = secrets.token_hex(16)
            existing = []
        else:
            existing = list(
                Upload.objects.select_for_update().filter(user=user, batch=batch)
            )
            if not existing:
                raise ToolError(f"Unknown batch {batch!r}.")
        paths = {entry["path"] for entry in entries}
        replaced = [upload for upload in existing if upload.path in paths]
        total = len(existing) - len(replaced) + len(entries)
        if total > max_files:
            raise ToolError(f"A batch holds at most {max_files} files, not {total}.")
        limit = conf.get("MAX_PENDING_BYTES")
        if limit is not None:
            pending = pending_bytes(user, now, exclude=[u.pk for u in replaced])
            requested = sum(entry["size"] for entry in entries)
            if pending + requested > limit:
                raise ToolError(
                    f"Uploads of {_megabytes(pending)} are already waiting; "
                    f"{_megabytes(requested)} more would pass the limit of "
                    f"{_megabytes(limit)}. Use them or let them expire first."
                )
        if replaced:
            Upload.objects.filter(pk__in=[u.pk for u in replaced]).delete()
            _delete_files_on_commit([u.stored_name for u in replaced], using)
        uploads, keys = [], []
        for entry in entries:
            upload = Upload(
                user=user,
                batch=batch,
                content_types=content_types,
                expires=expires,
                **entry,
            )
            keys.append(upload.set_key())
            uploads.append(upload)
        Upload.objects.bulk_create(uploads)
    return {
        "batch_id": batch,
        "expires_at": expires.isoformat(),
        "uploads": [
            {
                "path": upload.path,
                "url": upload_url(ctx, upload),
                "method": "PUT",
                "headers": {"Authorization": f"Bearer {key}"},
            }
            for upload, key in zip(uploads, keys)
        ],
        "upload_hint": (
            "Send each file from disk with its own request, several at a time: "
            'curl -sS --fail-with-body -X PUT -H "Authorization: Bearer <token>" '
            "--data-binary @<path> <url>. Then pass batch_id to the next tool."
        ),
    }


def upload_url(ctx, upload):
    request = ctx.request
    match = getattr(request, "resolver_match", None)
    namespace = match.namespace if match is not None else ctx.server.name
    return base_url(request) + reverse(f"{namespace}:upload", args=[upload.key_id])


def pending_bytes(user, now=None, exclude=()):
    """Bytes ``user`` has waiting: prepared or received, not used, failed or
    expired."""
    now = now or timezone.now()
    retain = now - timedelta(seconds=conf.get("RETAIN"))
    waiting = Upload.objects.filter(
        user=user, consumed_at__isnull=True, failure=""
    ).filter(Q(received_at__isnull=True, expires__gt=now) | Q(received_at__gt=retain))
    return waiting.exclude(pk__in=exclude).aggregate(total=Sum("size"))["total"] or 0


class Batch:
    """The uploads of one batch, sorted by path. ``received`` can be used;
    ``missing`` are still awaited; ``expired``, ``failed`` and ``consumed``
    cannot be used anymore."""

    def __init__(self, batch_id, uploads):
        self.id = batch_id
        self.uploads = list(uploads)
        by_state = {}
        for upload in self.uploads:
            by_state.setdefault(upload.state, []).append(upload)
        self.received = by_state.get("received", [])
        self.missing = by_state.get("missing", [])
        self.expired = by_state.get("expired", [])
        self.failed = by_state.get("failed", [])
        self.consumed = by_state.get("consumed", [])

    def __repr__(self):
        return f"<Batch {self.id} received={len(self.received)}>"

    def consume(self, uploads=None):
        """Mark ``uploads`` (default: all received) used; their files are
        deleted when the transaction commits, so read them first. Returns how
        many this call consumed."""
        return consume(self.received if uploads is None else uploads)


def get_batch(ctx, batch_id):
    """The ``Batch`` ``batch_id`` of the calling user."""
    user = _user(ctx)
    uploads = Upload.objects.filter(user=user, batch=batch_id)
    if not isinstance(batch_id, str) or not uploads.exists():
        raise ToolError(f"Unknown batch {batch_id!r}.")
    return Batch(batch_id, uploads)


def consume(uploads):
    """Mark received ``uploads`` used, once: a row another call consumed is
    not counted. Their files go when the transaction commits."""
    uploads = list(uploads)
    if not uploads:
        return 0
    using = router.db_for_write(Upload)
    now = timezone.now()
    count = (
        Upload.objects.filter(
            pk__in=[upload.pk for upload in uploads],
            consumed_at__isnull=True,
            received_at__isnull=False,
            failure="",
        )
        .exclude(stored_name="")
        .update(consumed_at=now, stored_name="")
    )
    _delete_files_on_commit([upload.stored_name for upload in uploads], using)
    return count


# --- the "upload" file source -----------------------------------------------


class StagedFile(File):
    """A received upload, opened from the staging storage on first read."""

    def __init__(self, storage, stored_name, name, size):
        self.storage = storage
        self.stored_name = stored_name
        self._opened = None
        self.name = name
        self.size = size
        self.mode = "rb"

    @property
    def file(self):
        if self._opened is None:
            self._opened = self.storage.open(self.stored_name, "rb")
        return self._opened

    @file.setter
    def file(self, value):
        self._opened = value

    @property
    def closed(self):
        return self._opened is None or self._opened.closed

    def open(self, mode=None):
        self.close()
        self._opened = self.storage.open(self.stored_name, "rb")
        return self

    def close(self):
        if self._opened is not None:
            self._opened.close()
            self._opened = None


def resolve(ctx, value):
    """Fill a ``FileValue`` of source ``"upload"`` from its row; the upload is
    consumed when the tool succeeds (inside its transaction, if it has one)."""
    reference = value.value
    path = normalize_path(reference["path"], "upload.path")
    upload = (
        Upload.objects.filter(user=_user(ctx), batch=reference["batch"], path=path)
        .order_by()
        .first()
    )
    label = f"Upload {path!r}"
    if upload is None:
        raise ToolError(f"{label} is not in batch {reference['batch']!r}.")
    if upload.failure:
        raise ToolError(f"{label} failed: {upload.failure} Prepare it again.")
    if upload.consumed_at is not None:
        raise ToolError(f"{label} was already used.")
    if upload.is_expired():
        raise ToolError(f"{label} expired. Prepare it again.")
    if upload.received_at is None:
        raise ToolError(f"{label} has not been uploaded yet.")
    staged = StagedFile(
        conf.get_storage(), upload.stored_name, upload.name, upload.size
    )
    value.value = staged
    value.name = upload.name
    value.size = upload.size
    value.sha256 = upload.sha256
    value.content_type = upload.content_type
    value.upload = upload
    _use(ctx, upload, staged)
    return value


def _use(ctx, upload, staged):
    """Consume ``upload`` when the call succeeds, once even when the call
    takes it twice (as two arguments)."""
    used = ctx.__dict__.get("_mcp_uploads")
    if used is None:
        used = ctx.__dict__["_mcp_uploads"] = {"uploads": {}, "files": []}

        def finish(ok):
            for file in used["files"]:
                file.close()
            if ok:
                _consume_used(ctx, list(used["uploads"].values()))

        ctx.on_call_end(finish)
    used["uploads"][upload.pk] = upload
    used["files"].append(staged)


def _consume_used(ctx, uploads):
    if consume(uploads) == len(uploads):
        return
    # another call used one first; inside the call's transaction (on the
    # uploads' database) that call wins and this one rolls back
    if ctx.tool_database is not None and ctx.tool_database == router.db_for_write(
        Upload
    ):
        raise ToolError(
            f"Upload {', '.join(repr(u.path) for u in uploads)} was already used."
        )
    logger.warning("An upload was used by two calls at once")


# --- helpers -----------------------------------------------------------------


def _user(ctx):
    user = getattr(ctx, "user", None)
    if user is None or not user.is_authenticated:
        raise ToolError("Uploads need a signed-in user.")
    return user


def _entries(files, max_size):
    if not isinstance(files, (list, tuple)) or not files:
        raise ToolError("files must be a non-empty list.")
    entries, seen = [], set()
    for index, item in enumerate(files):
        label = f"files[{index}]"
        if not isinstance(item, dict):
            raise ToolError(f"{label} must be an object with path, sha256 and size.")
        unknown = sorted(set(item) - {"path", "sha256", "size"})
        if unknown:
            raise ToolError(f"{label}: unknown keys {unknown}.")
        path = normalize_path(item.get("path"), f"{label}.path")
        if path in seen:
            raise ToolError(f"{label}.path {path!r} is listed twice.")
        seen.add(path)
        sha256 = item.get("sha256")
        if not isinstance(sha256, str) or not _SHA256.match(sha256.lower()):
            raise ToolError(f"{label}.sha256 must be 64 hexadecimal characters.")
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ToolError(f"{label}.size must be a whole number of bytes.")
        if max_size is not None and size > max_size:
            raise ToolError(f"{label}: {path} is larger than {max_size} bytes.")
        entries.append({"path": path, "sha256": sha256.lower(), "size": size})
    return entries


def _delete_files_on_commit(names, using):
    names = [name for name in names if name]
    if not names:
        return

    def delete():
        storage = conf.get_storage()
        for name in names:
            try:
                storage.delete(name)
            except Exception:  # cleanup deletes it later
                logger.exception("Could not delete staged upload %s", name)

    transaction.on_commit(delete, using=using)


def _megabytes(size):
    return f"{size / 1024 / 1024:.1f} MB"
