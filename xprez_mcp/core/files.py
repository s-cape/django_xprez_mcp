"""File field arguments: a file sent as a JSON value.

The transport is JSON only, so a ``FileField`` / ``ImageField`` argument is one
of ``{"data": <base64>, "name": ...}``, ``{"path": ...}`` (a file already in the
field's storage), ``{"url": ...}`` (fetched by the server, opt-in) or ``null``
(clear the field, when it allows blank).
"""

import base64
import binascii
import http.client
import ipaddress
import mimetypes
import posixpath
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache

from ..errors import InvalidArguments, ToolError

SOURCES = ("data", "path", "url")
DEFAULT_SOURCES = ("data",)  # "path" and "url" are opt-in

_SHAPES = {
    "data": {
        "type": "object",
        "properties": {
            "data": {"type": "string", "description": "The content, base64"},
            "name": {"type": "string", "description": "File name, like hero.jpg"},
        },
        "required": ["data", "name"],
        "additionalProperties": False,
    },
    "path": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "A file already in storage"}
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "url": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "An http(s) URL to fetch"}
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}
_WORDS = {
    "data": "{data, name} uploads base64 content",
    "path": "{path} uses a file already in storage",
    "url": "{url} fetches a file",
}


class FileValue:
    """A file argument whose shape is checked: ``source`` is ``"data"`` (``value``
    is the decoded bytes), ``"path"`` or ``"url"`` (``value`` is the string)."""

    def __init__(self, source, value, name=None):
        self.source = source
        self.value = value
        self.name = name

    def __eq__(self, other):
        return isinstance(other, FileValue) and (
            (self.source, self.value, self.name)
            == (other.source, other.value, other.name)
        )

    def __repr__(self):
        shown = f"<{len(self.value)} bytes>" if self.source == "data" else self.value
        return f"FileValue({self.source!r}, {shown!r}, name={self.name!r})"


class FileArgument:
    """The annotation of a file field's argument; see ``file_argument``."""

    sources = DEFAULT_SOURCES
    nullable = False

    @classmethod
    def json_schema(cls):
        shapes = [_SHAPES[source] for source in cls.sources]
        words = [_WORDS[source] for source in cls.sources]
        if cls.nullable:
            shapes.append({"type": "null"})
            words.append("null clears it")
        schema = shapes[0] if len(shapes) == 1 else {"anyOf": shapes}
        return {**schema, "description": f"A file: {', '.join(words)}."}

    @classmethod
    def coerce(cls, value, path):
        if value is None:
            if cls.nullable:
                return None
            raise InvalidArguments(f"{path} is required and cannot be cleared.")
        expected = " or ".join(f"{{{source}: ...}}" for source in cls.sources)
        if not isinstance(value, dict):
            raise InvalidArguments(f"{path} must be {expected}.")
        given = [source for source in SOURCES if source in value]
        if len(given) != 1:
            raise InvalidArguments(f"{path} must be {expected}.")
        source = given[0]
        if source not in cls.sources:
            raise InvalidArguments(
                f"{path}: {source} is not accepted here. Use {expected}."
            )
        keys = {"data", "name"} if source == "data" else {source}
        unknown = sorted(set(value) - keys)
        if unknown:
            raise InvalidArguments(f"{path}: unknown keys {unknown}.")
        for key in keys:
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise InvalidArguments(f"{path}.{key} must be a non-empty string.")
        if source == "data":
            return FileValue(
                "data", _decode(value["data"], path), _file_name(value["name"], path)
            )
        if source == "path":
            return FileValue("path", _storage_path(value["path"], path))
        parts = urllib.parse.urlsplit(value["url"])
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise InvalidArguments(f"{path}.url must be an http or https URL.")
        return FileValue("url", value["url"])


def file_argument(sources=DEFAULT_SOURCES, nullable=False):
    """The ``FileArgument`` accepting ``sources``, and ``null`` when ``nullable``."""
    return _file_argument(tuple(sources), bool(nullable))


@lru_cache(maxsize=None)
def _file_argument(sources, nullable):
    if not sources:
        raise ValueError("A file argument needs at least one source.")
    unknown = set(sources) - set(SOURCES)
    if unknown:
        raise ValueError(f"Unknown file sources {sorted(unknown)}.")
    return type(
        "FileArgument",
        (FileArgument,),
        {"sources": tuple(sources), "nullable": nullable},
    )


def _decode(data, path):
    if data.startswith("data:") and ";base64," in data:  # a data URL
        data = data.split(";base64,", 1)[1]
    try:
        return base64.b64decode("".join(data.split()), validate=True)
    except (binascii.Error, ValueError):
        raise InvalidArguments(f"{path}.data is not valid base64.") from None


def _file_name(name, path):
    name = posixpath.basename(name.replace("\\", "/")).strip()
    if name in ("", ".", ".."):
        raise InvalidArguments(f"{path}.name must be a file name.")
    return name


def _storage_path(value, path):
    parts = value.split("/")
    if (
        value.startswith("/")
        or "\\" in value
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise InvalidArguments(f"{path}.path must be a relative path in storage.")
    return value


# --- fetching ----------------------------------------------------------------


_NAT64 = ipaddress.ip_network("64:ff9b::/96")  # embeds any IPv4 address
_LOCAL_NAT64 = ipaddress.ip_network("64:ff9b:1::/48")


def is_public_address(address):
    """Whether the server may connect to ``address`` when fetching a URL.

    IPv6 forms that carry an IPv4 address (mapped, 6to4, Teredo, NAT64) are
    judged by that address: a gateway forwards them to it, internal or not.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        if ip in _LOCAL_NAT64:
            return False
        embedded = [ip.ipv4_mapped, ip.sixtofour, *(ip.teredo or ())]
        if ip in _NAT64:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        embedded = [address for address in embedded if address is not None]
        if embedded:
            return all(address.is_global for address in embedded)
    return ip.is_global


# the fetch running in this thread: its sockets and whether its time is up
_fetching = threading.local()


def _public_connection(address, timeout=None, source_address=None, *args, **kwargs):
    """``socket.create_connection`` that refuses addresses that are not public.

    The check runs on the addresses actually connected to, so redirects and DNS
    answers that change between lookups cannot reach internal hosts.
    """
    expired = getattr(_fetching, "expired", None)
    if expired is not None and expired.is_set():
        raise OSError("timed out")
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for *_, sockaddr in infos:
        if not is_public_address(sockaddr[0]):
            raise ToolError(f"{host} is not a public address.")
    error = OSError(f"Cannot connect to {host}.")
    for family, kind, proto, _, sockaddr in infos:
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            sockets = getattr(_fetching, "sockets", None)
            if sockets is not None:
                # a duplicate shares the connection but not the descriptor
                # number, so shutting it down at the deadline is safe
                sockets.append(sock.dup())
            return sock
        except OSError as exc:
            error = exc
            sock.close()
    raise error


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


def _opener():
    """http(s) only, no proxies: a proxy would be the address checked."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        _HTTPHandler(),
        _HTTPSHandler(),
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def fetch(url, max_size=None, timeout=10):
    """``(content, name, content_type)`` of a public http(s) URL.

    ``timeout`` bounds the whole fetch, redirects included, not only each
    socket operation: a server sending a byte at a time cannot hold the worker.
    ``content_type`` is None when the response has none.
    """
    sockets, expired = [], threading.Event()
    lock, finished = threading.Lock(), []

    def expire():
        with lock:
            if finished:  # the fetch ended first: its result stands
                return
            expired.set()
        for sock in list(sockets):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    _fetching.sockets, _fetching.expired = sockets, expired
    timer.start()
    try:
        result = _fetch(url, max_size, timeout)
    except ToolError:
        with lock:
            finished.append(True)
        if not expired.is_set():
            raise
        result = None
    else:
        with lock:
            finished.append(True)
    finally:
        timer.cancel()
        _fetching.sockets = _fetching.expired = None
        for sock in sockets:
            sock.close()
    if expired.is_set():  # a shut down connection may also read as the end
        raise ToolError(f"Fetching {url} took longer than {timeout} seconds.")
    return result


def _fetch(url, max_size, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "django-xprez-mcp"})
    too_large = ToolError(f"The file at {url} is larger than {max_size} bytes.")
    try:
        with _opener().open(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length", "")
            if max_size is not None and length.isdigit() and int(length) > max_size:
                raise too_large
            chunks, size = [], 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if max_size is not None and size > max_size:
                    raise too_large
                chunks.append(chunk)
            content_type = None
            if response.headers.get("Content-Type"):
                content_type = response.headers.get_content_type()
            final_url = response.geturl()
    except urllib.error.HTTPError as error:
        raise ToolError(f"Fetching {url} failed: HTTP {error.code}.") from None
    except urllib.error.URLError as error:
        raise ToolError(f"Fetching {url} failed: {error.reason}.") from None
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise ToolError(f"Fetching {url} failed: {error}.") from None
    path = urllib.parse.unquote(urllib.parse.urlsplit(final_url).path)
    name = posixpath.basename(path.replace("\\", "/")).strip()
    if name in ("", ".", ".."):
        name = "download"
    if content_type and not posixpath.splitext(name)[1]:
        name += mimetypes.guess_extension(content_type) or ""
    return b"".join(chunks), name, content_type


def shorten_name(name, limit):
    """``name`` cut to ``limit`` characters, keeping its extension."""
    if limit is None or len(name) <= limit:
        return name
    root, extension = posixpath.splitext(name)
    if len(extension) >= limit:
        extension = ""
    return root[: limit - len(extension)] + extension
