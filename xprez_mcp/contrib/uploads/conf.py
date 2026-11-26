from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import Storage
from django.utils.module_loading import import_string

from xprez_mcp import conf

DEFAULTS = {
    # required: where uploads wait until a tool uses them; keep it private
    "STORAGE": None,
    "TTL": 15 * 60,  # seconds an upload URL is valid
    "RETAIN": 24 * 3600,  # seconds a received file waits to be used
    "MAX_FILE_SIZE": 15 * 1024 * 1024,
    "MAX_FILES": 2000,  # per batch
    "MAX_PENDING_BYTES": 1024**3,  # per user, prepared and not used yet
    "VALIDATOR": None,  # f(file, upload); raises ValidationError
}
# staged files are stored as PREFIX + a random name
PREFIX = "mcp-uploads/"


def get(key):
    return {**DEFAULTS, **(conf.get("UPLOADS") or {})}[key]


def get_storage():
    """``STORAGE``: a ``Storage``, a ``STORAGES`` alias (Django 4.2+) or a
    dotted path to a storage or a function returning one. Not a class: built
    without arguments, ``FileSystemStorage`` would store under MEDIA_ROOT."""
    value = get("STORAGE")
    if value is None:
        raise ImproperlyConfigured(
            'xprez_mcp.contrib.uploads needs XPREZ_MCP["UPLOADS"]["STORAGE"]: '
            "a storage that is not served to the public."
        )
    if isinstance(value, str):
        if value in (getattr(settings, "STORAGES", None) or {}):
            from django.core.files.storage import storages

            return storages[value]
        value = import_string(value)
    if isinstance(value, type):
        raise ImproperlyConfigured(
            'XPREZ_MCP["UPLOADS"]["STORAGE"] is a class; give a configured '
            "storage (or a STORAGES alias) instead."
        )
    if not isinstance(value, Storage) and callable(value):
        value = value()
    if not isinstance(value, Storage):
        raise ImproperlyConfigured(
            f'XPREZ_MCP["UPLOADS"]["STORAGE"] is not a storage: {value!r}.'
        )
    return value


def get_validator():
    value = get("VALIDATOR")
    if isinstance(value, str):
        value = import_string(value)
    return value
