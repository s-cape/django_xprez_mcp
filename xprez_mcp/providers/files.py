"""``FileArgumentsMixin``: file arguments turned into files a form or a
serializer accepts, for any provider that writes model file fields."""

import logging
import mimetypes
import posixpath
from contextlib import contextmanager

from django.apps import apps
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models
from django.utils.datastructures import MultiValueDict

from ..core import files as file_arguments
from ..core.files import DEFAULT_SOURCES, FileValue
from ..errors import InvalidArguments, ToolError
from .fields import file_field, upload_prefix

logger = logging.getLogger("xprez_mcp")


class FileArgumentsMixin:
    """Resolve file arguments (``FileValue``) into files for model file fields.

    ``ModelProvider`` and ``AdminProvider`` inherit it; a plain ``Provider``
    whose tools write file fields through its own forms can too: ``file_value``,
    ``resolve_file``, then ``form_files`` for the form, inside
    ``removing_uploads_on_error``.
    """

    file_sources = DEFAULT_SOURCES  # ["data"]; "path" and "url" are opt-in
    max_file_size = 10 * 1024 * 1024  # bytes, uploads and fetches; None: no limit
    file_fetch_timeout = 10  # seconds

    def file_value(self, value, field, path):
        """A ``FileValue`` (or ``None`` for ``null``) from an untyped JSON value,
        as a typed file argument of ``field`` converts it: the shapes in
        ``file_sources``, ``null`` when the field allows blank. ``path`` names
        the argument in errors, like ``fields.media``.
        """
        if not isinstance(field, models.FileField):
            raise TypeError(f"file_value needs a FileField, not {field!r}.")
        if not self.file_sources:
            raise InvalidArguments(f"{path} is read-only.")
        argument = file_arguments.file_argument(self.file_sources, field.blank)
        return argument.coerce(value, path)

    def resolve_files(self, ctx, data, opts=None):
        """``(data, files)``: file arguments taken out of ``data`` as files a form
        accepts, ``False`` where the argument was ``null`` (clear).

        ``opts`` is the model's ``_meta``; it defaults to ``self.opts``, which
        only a ``ModelProvider`` has.
        """
        opts = opts or getattr(self, "opts", None)
        if opts is None:
            raise TypeError("resolve_files needs opts outside a ModelProvider.")
        data, files = dict(data), {}
        for name, value in list(data.items()):
            field = file_field(opts, name)
            if field is None or not (value is None or isinstance(value, FileValue)):
                continue
            del data[name]
            files[name] = (
                False if value is None else self.resolve_file(ctx, field, value)
            )
        return data, files

    def resolve_file(self, ctx, field, value):
        """An uploaded file (``data``, ``url``) or a stored one (``path``)."""
        if value.source == "path":
            return self.stored_file(ctx, field, value.value)
        content_type = None
        if value.source == "url":
            content, name, content_type = self.fetch_file(ctx, field, value.value)
            # the client did not choose this name, so it must not fail the form
            limit = field.max_length
            if limit is not None:
                limit = max(limit - len(upload_prefix(field) or ""), 8)
            name = file_arguments.shorten_name(name, limit)
        else:
            content, name = value.value, value.name
        if self.max_file_size is not None and len(content) > self.max_file_size:
            raise ToolError(
                f"{field.name}: the file is larger than {self.max_file_size} bytes."
            )
        if content_type is None:
            content_type = mimetypes.guess_type(name)[0]
        # a field file not yet saved, so a failed write can delete what the
        # save stored (see removing_uploads_on_error)
        upload = field.attr_class(None, field, name)
        upload.file = SimpleUploadedFile(name, content, content_type)
        upload._committed = False
        upload._mcp_upload = True
        return upload

    @contextmanager
    def removing_uploads_on_error(self, values):
        """Delete files a failed write already stored: the transaction rolls
        back the rows but not the storage."""
        uploads = [value for value in values if getattr(value, "_mcp_upload", False)]
        try:
            yield
        except BaseException:
            for upload in uploads:
                if upload._committed and upload.name:
                    try:
                        upload.storage.delete(upload.name)
                    except Exception:  # keep the error that failed the write
                        logger.exception("Could not delete %s", upload.name)
            raise

    def stored_file(self, ctx, field, path):
        """The file at ``path`` in the field's storage, assigned without a copy.

        Only paths ``allows_file_path`` accepts; remove ``"path"`` from
        ``file_sources`` to refuse them all.
        """
        if not self.allows_file_path(ctx, field, path):
            prefix = upload_prefix(field)
            if type(self).allows_file_path is not FileArgumentsMixin.allows_file_path:
                raise ToolError(f"{field.name}: path {path!r} is not allowed.")
            if prefix is None:
                raise ToolError(f"{field.name} does not accept stored paths.")
            if path.startswith(prefix):
                raise ToolError(
                    f"{field.name}: {path!r} is in another file field's folder."
                )
            raise ToolError(f"{field.name}: path must be under {prefix!r}.")
        storage = field.storage
        try:
            exists = storage.exists(path)
        except SuspiciousFileOperation:
            exists = False
        if exists:
            try:
                directories = storage.listdir(posixpath.dirname(path))[0]
            except (NotImplementedError, OSError):
                directories = []
            exists = posixpath.basename(path) not in directories
        if not exists:
            raise ToolError(f"{field.name}: no file at {path!r}.")
        # validation reads the file (an ImageField all of it), so bound it
        if self.max_file_size is not None and storage.size(path) > self.max_file_size:
            raise ToolError(
                f"{field.name}: the file is larger than {self.max_file_size} bytes."
            )
        return field.attr_class(None, field, path)

    def allows_file_path(self, ctx, field, path):
        """Whether ``path`` may be referenced. By default: under the static
        folder of the field's ``upload_to``, where its own uploads go, and not
        under a deeper folder of another file field (``docs/private`` inside
        ``docs``). A callable or empty ``upload_to`` accepts no path until
        overridden.

        The default does not know who owns a file: every file in that folder
        can be referenced, other records' included, and so can those of other
        fields that share the exact folder. Override for anything stricter.
        """
        prefix = upload_prefix(field)
        if prefix is None or not path.startswith(prefix):
            return False
        return not any(
            path.startswith(other)
            for other in _other_upload_prefixes(field)
            if len(other) > len(prefix) and other.startswith(prefix)
        )

    def fetch_file(self, ctx, field, url):
        """``(content, name, content_type)`` for a ``url`` argument.

        Only http(s) to public addresses, no proxies; override for an allowlist
        of hosts or a fetcher of your own.
        """
        return file_arguments.fetch(
            url, max_size=self.max_file_size, timeout=self.file_fetch_timeout
        )


def form_files(files):
    """``files`` for a form: a ``MultiValueDict``, as widgets taking several
    files call ``getlist``."""
    return MultiValueDict({name: [file] for name, file in files.items()})


@contextmanager
def closing_stored_files(files):
    """Close the stored files (``path``) that validation opened; uploads stay
    open for the save."""
    try:
        yield
    finally:
        for file in files.values():
            if file and getattr(file, "_committed", False):
                file.close()


def check_cleared(form, files, prefix=""):
    """Refuse a ``null`` the form ignored: a required form field keeps its
    file when sent ``False``."""
    for name, file in files.items():
        if file is False and form.cleaned_data.get(name):
            raise ToolError(f"{prefix}{name} is required and cannot be cleared.")


def _other_upload_prefixes(field):
    """The upload prefixes of every other file field of installed models."""
    prefixes = set()
    for model in apps.get_models():
        for other in model._meta.get_fields():
            if isinstance(other, models.FileField) and other is not field:
                prefix = upload_prefix(other)
                if prefix:
                    prefixes.add(prefix)
    return prefixes
