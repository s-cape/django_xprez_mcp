"""``PUT <endpoint>/uploads/<id>/``: receives one file, once.

The key in ``Authorization: Bearer`` is the only authorization; the URL
holds only the upload's id, so logs of URLs reveal nothing that grants access.
"""

import codecs
import hashlib
import logging
import secrets
import tempfile

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import router, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from . import conf
from .content import HEAD_SIZE, allowed, detect, name_problem
from .models import Upload

logger = logging.getLogger("xprez_mcp")


@method_decorator(csrf_exempt, name="dispatch")
class UploadView(View):
    server = None
    http_method_names = ["put"]
    chunk_size = 64 * 1024

    def put(self, request, key_id):
        upload = Upload.objects.filter(key_id=key_id).first()
        scheme, _, key = request.headers.get("Authorization", "").partition(" ")
        if (
            upload is None
            or scheme.lower() != "bearer"
            or not upload.check_key(key.strip())
        ):
            return respond(None, 404, "Unknown upload.")
        if upload.failure:
            return respond(upload, 410, f"This upload failed: {upload.failure}")
        if upload.received_at is not None:
            return respond(upload, 409, "This file was already uploaded.")
        if upload.expires <= timezone.now():
            return respond(upload, 410, "This upload URL expired.")
        length = request.headers.get("Content-Length", "")
        if not length.isdigit():
            # a chunked body reads as empty here; the client can retry
            return respond(upload, 411, "Send the file with a Content-Length.")
        if int(length) > upload.size:
            return fail(upload, 413, f"The file is larger than {upload.size} bytes.")
        with tempfile.TemporaryFile() as temporary:
            problem = self.receive(request, upload, temporary)
            if problem is not None:
                return fail(upload, *problem)
            return self.store(upload, temporary)

    def receive(self, request, upload, temporary):
        """Stream the body to ``temporary``; ``(status, reason)`` if it is not
        the file that was announced."""
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")()
        size, head, is_text = 0, b"", True
        while True:
            chunk = request.read(self.chunk_size)
            if not chunk:
                break
            size += len(chunk)
            if size > upload.size:
                return 413, f"The file is larger than {upload.size} bytes."
            digest.update(chunk)
            temporary.write(chunk)
            if len(head) < HEAD_SIZE:
                head += chunk[: HEAD_SIZE - len(head)]
            if is_text:
                try:
                    decoder.decode(chunk)
                except UnicodeDecodeError:
                    is_text = False
                is_text = is_text and b"\x00" not in chunk
        if is_text:
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                is_text = False
        if size != upload.size:
            return 400, f"The file has {size} bytes, not {upload.size}."
        if digest.hexdigest() != upload.sha256:
            return 400, "The file does not match its sha256."
        content_type = detect(head, is_text, upload.path)
        if not allowed(content_type, upload.content_types):
            return 400, f"The file is {content_type}, which is not accepted here."
        problem = name_problem(upload.path, content_type)
        if problem:
            return 400, problem
        upload.content_type = content_type
        validator = conf.get_validator()
        if validator is not None:
            temporary.seek(0)
            try:
                validator(temporary, upload)
            except ValidationError as error:
                return 400, " ".join(error.messages)
        return None

    def store(self, upload, temporary):
        storage = conf.get_storage()
        using = router.db_for_write(Upload)
        stored = None
        try:
            with transaction.atomic(using=using):
                # two PUTs at once: the first to lock the row wins
                current = Upload.objects.select_for_update().get(pk=upload.pk)
                if current.failure:
                    return respond(upload, 410, "This upload failed.")
                if current.received_at is not None:
                    return respond(upload, 409, "This file was already uploaded.")
                temporary.seek(0)
                stored = storage.save(
                    f"{conf.PREFIX}{secrets.token_hex(16)}", File(temporary)
                )
                Upload.objects.filter(pk=upload.pk).update(
                    received_at=timezone.now(),
                    stored_name=stored,
                    content_type=upload.content_type,
                )
        except Exception:
            if stored:
                storage.delete(stored)
            raise
        return respond(upload, 201, None)


def fail(upload, status, reason):
    """The file is not what was announced: the upload cannot be used anymore."""
    Upload.objects.filter(pk=upload.pk, received_at__isnull=True, failure="").update(
        failure=reason
    )
    return respond(upload, status, reason)


def respond(upload, status, error):
    # the upload id only: never the key
    logger.info(
        "upload=%s user=%s status=%s",
        getattr(upload, "pk", None),
        getattr(upload, "user_id", None),
        status,
    )
    if error is not None:
        return JsonResponse({"error": error}, status=status)
    return JsonResponse(
        {
            "path": upload.path,
            "size": upload.size,
            "content_type": upload.content_type,
        },
        status=status,
    )
