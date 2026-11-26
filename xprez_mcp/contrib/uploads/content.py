"""What a received file is, judged by its bytes, and whether its name agrees."""

import fnmatch
import posixpath

SIGNATURES = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
]
# text is typed by its extension; the active ones are refused unless named
TEXT_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "text/html",
    ".shtml": "text/html",
    ".svg": "image/svg+xml",
    ".xml": "application/xml",
    ".xsl": "application/xml",
    ".xslt": "application/xml",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
}
BINARY_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
# what browsers render or run: only accepted when named exactly, never by a
# wildcard
ACTIVE_TYPES = ("image/svg+xml", "text/html", "application/xml", "text/javascript")
DEFAULT_CONTENT_TYPES = [
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "application/pdf",
    "text/markdown",
    "application/json",
    "text/csv",
    "text/plain",
]
HEAD_SIZE = 4096
_MARKUP = ("<!doctype html", "<html", "<head", "<body", "<script", "<iframe")


def detect(head, is_text, path):
    """The content type of a file starting with ``head``. ``is_text``: the whole
    file is UTF-8 without NUL bytes. Text files are named by ``path``'s
    extension (``.md``, ``.json``...) unless they start like SVG or HTML."""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    for signature, content_type in SIGNATURES:
        if head.startswith(signature):
            return content_type
    if not is_text:
        return "application/octet-stream"
    start = head.decode("utf-8", "ignore").lstrip("﻿ \t\r\n").lower()
    if start.startswith("<svg") or (start.startswith("<?xml") and "<svg" in start):
        return "image/svg+xml"
    if start.startswith(_MARKUP):
        return "text/html"
    extension = posixpath.splitext(path)[1].lower()
    return TEXT_TYPES.get(extension, "text/plain")


def name_problem(path, content_type):
    """Why ``path`` cannot carry ``content_type`` (a known extension of another
    type, like a PNG named ``.html``), or None."""
    extension = posixpath.splitext(path)[1].lower()
    named = TEXT_TYPES.get(extension) or BINARY_TYPES.get(extension)
    if named is None or named == content_type:
        return None
    return f"The file is {content_type}, but its name ends in {extension}."


def allowed(content_type, accepted):
    """Whether ``content_type`` is one of ``accepted`` (``"image/*"`` style
    wildcards allowed; SVG and HTML only by name)."""
    if content_type in accepted:
        return True
    if content_type in ACTIVE_TYPES:
        return False
    return any(fnmatch.fnmatchcase(content_type, pattern) for pattern in accepted)
