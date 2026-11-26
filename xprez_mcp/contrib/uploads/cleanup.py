"""What ``mcp_cleanup`` deletes for the uploads app."""

import logging
from datetime import timedelta

from django.db.models import Q

from . import conf
from .models import Upload

logger = logging.getLogger("xprez_mcp")
BATCH = 1000


class CleanupError(Exception):
    pass


def cleanup(now):
    """Uploads whose URL expired unused, used ones, failed ones and received
    ones older than ``RETAIN``, with their files; then staged files no row
    knows (a crash between storing and saving the row)."""
    storage = conf.get_storage()
    retain = now - timedelta(seconds=conf.get("RETAIN"))
    done = (
        Q(received_at__isnull=True, expires__lte=now)
        | Q(consumed_at__isnull=False)
        | Q(received_at__lte=retain)
        | (~Q(failure="") & Q(created__lte=retain))
    )
    counts = {"uploads": 0, "files": 0}
    errors = []
    deletable = []
    for upload in Upload.objects.filter(done).only("pk", "stored_name").iterator():
        if upload.stored_name:
            try:
                storage.delete(upload.stored_name)
            except Exception as error:  # keep the row: the next run retries
                errors.append(error)
                continue
            counts["files"] += 1
        deletable.append(upload.pk)
    for start in range(0, len(deletable), BATCH):
        chunk = deletable[start : start + BATCH]
        counts["uploads"] += Upload.objects.filter(pk__in=chunk).delete()[0]
    counts["files"] += delete_orphans(storage, now)
    if errors:
        raise CleanupError(
            f"{len(errors)} staged files could not be deleted, first: {errors[0]!r}"
        )
    return counts


def delete_orphans(storage, now):
    """Staged files without a row, older than an upload URL lives."""
    try:
        names = storage.listdir(conf.PREFIX.rstrip("/"))[1]
    except (FileNotFoundError, NotImplementedError):
        return 0
    known = set(
        Upload.objects.exclude(stored_name="").values_list("stored_name", flat=True)
    )
    cutoff = now - timedelta(seconds=conf.get("TTL"))
    count = 0
    for name in names:
        stored = conf.PREFIX + name
        if stored in known:
            continue
        try:
            if storage.get_modified_time(stored) > cutoff:
                continue  # may be an upload being saved right now
            storage.delete(stored)
        except NotImplementedError:
            return count
        except Exception:
            logger.exception("Could not delete orphaned upload %s", stored)
            continue
        count += 1
    return count
