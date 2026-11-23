"""Fetching client ID metadata documents without reaching internal hosts.

The host is resolved once, every address must be public, and the connection
goes to the checked addresses (no second lookup to rebind). No redirects, and
one deadline for the whole fetch.
"""

import http.client
import io
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

from django.utils.translation import gettext as _

from . import conf
from .errors import ClientError

MAX_AGE = re.compile(r"max-age=(\d+)")


def is_metadata_url(value):
    parts = urlsplit(value)
    return (
        parts.scheme == "https" and bool(parts.netloc) and parts.path not in ("", "/")
    )


def public_addresses(url):
    """``(host, port, addresses)`` of an https URL whose host is public, in
    resolver order."""
    parts = urlsplit(url)
    try:
        port = parts.port or 443
    except ValueError:
        port = None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or port is None
        or parts.username is not None
        or parts.fragment
    ):
        raise ClientError(_("The client ID is not a valid https URL."))
    try:
        infos = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise ClientError(_("The client's metadata document could not be fetched."))
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise ClientError(
                _("The client's metadata document is not on a public host.")
            )
    if not addresses:
        raise ClientError(_("The client's metadata document could not be fetched."))
    return parts.hostname, port, addresses


class DeadlineSocket:
    """Bounds the whole exchange, not each read: a server sending a byte at
    a time cannot hold the worker past the deadline."""

    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("Metadata document fetch took too long.")
        self.sock.settimeout(remaining)

    def sendall(self, data):
        self.remaining()
        self.sock.sendall(data)

    def recv_into(self, buffer):
        self.remaining()
        return self.sock.recv_into(buffer)

    def makefile(self, mode):
        return io.BufferedReader(DeadlineReader(self))

    def close(self):
        self.sock.close()


class DeadlineReader(io.RawIOBase):
    def __init__(self, sock):
        self.sock = sock

    def readable(self):
        return True

    def readinto(self, buffer):
        return self.sock.recv_into(buffer)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to the checked addresses (each in turn), verifying the
    certificate for ``host``, within ``deadline``."""

    def __init__(self, host, port, addresses, deadline):
        self.ssl_context = ssl.create_default_context()
        super().__init__(host, port, context=self.ssl_context)
        self.addresses = addresses
        self.deadline = deadline

    def connect(self):
        error = OSError("No address to connect to.")
        for address in self.addresses:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                sock = socket.create_connection((address, self.port), remaining)
            except OSError as exc:
                error = exc
                continue
            sock.settimeout(max(self.deadline - time.monotonic(), 0.001))
            try:
                sock = self.ssl_context.wrap_socket(sock, server_hostname=self.host)
            except OSError:
                sock.close()
                raise
            self.sock = DeadlineSocket(sock, self.deadline)
            return
        raise error


def fetch(url):
    """``(document, max_age)``; raises ``ClientError``."""
    host, port, addresses = public_addresses(url)
    parts = urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    limit = conf.get("METADATA_MAX_BYTES")
    deadline = time.monotonic() + conf.get("METADATA_TIMEOUT")
    connection = PinnedHTTPSConnection(host, port, addresses, deadline)
    try:
        connection.request(
            "GET",
            target,
            headers={"Accept": "application/json", "User-Agent": "django-xprez-mcp"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ClientError(_("The client's metadata document could not be fetched."))
        body = response.read(limit + 1)
        cache_control = response.getheader("Cache-Control") or ""
    except (OSError, http.client.HTTPException):
        raise ClientError(_("The client's metadata document could not be fetched."))
    finally:
        connection.close()
    if len(body) > limit:
        raise ClientError(_("The client's metadata document is too large."))
    try:
        document = json.loads(body)
    except ValueError:
        raise ClientError(_("The client's metadata document is not valid JSON."))
    return document, max_age(cache_control)


def max_age(cache_control):
    if "no-store" in cache_control or "no-cache" in cache_control:
        return 0
    match = MAX_AGE.search(cache_control)
    return int(match.group(1)) if match else None
