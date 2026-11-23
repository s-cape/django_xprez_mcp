"""Finding clients: registered ones, and client ID metadata documents."""

import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext as _

from . import cimd, conf
from .throttling import client_ip, throttled
from .errors import ClientError
from .models import Client, hash_key

AUTH_METHODS = ["none", "client_secret_basic", "client_secret_post"]
GRANT_TYPES = {"authorization_code", "refresh_token"}
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
# registration fields kept in Client.metadata
REGISTERED_FIELDS = {
    "client_name",
    "client_uri",
    "logo_uri",
    "grant_types",
    "response_types",
    "token_endpoint_auth_method",
    "software_id",
    "software_version",
}


def is_loopback(uri):
    parts = urlsplit(uri)
    return parts.scheme == "http" and parts.hostname in LOOPBACK


def validate_redirect_uri(uri):
    """https, or http on a loopback host; no fragment."""
    invalid = ClientError(
        _("Redirect URIs must use https, or http on localhost."),
        "invalid_redirect_uri",
    )
    if not isinstance(uri, str) or not uri or len(uri) > 2000 or "#" in uri:
        raise invalid
    parts = urlsplit(uri)
    if not parts.hostname:
        raise invalid
    if parts.scheme != "https" and not is_loopback(uri):
        raise invalid
    return uri


def match_redirect_uri(client, uri):
    """The registered URI ``uri`` matches exactly; on loopback any port
    matches (RFC 8252), as native apps listen on a free port."""
    registered = client.redirect_uris
    if not uri:
        if len(registered) == 1:
            return registered[0]
        raise ClientError(_("The redirect URI is missing."), "invalid_request")
    for candidate in registered:
        if uri == candidate or (
            is_loopback(candidate)
            and is_loopback(uri)
            and same_but_port(uri, candidate)
        ):
            return uri
    raise ClientError(
        _("The redirect URI is not registered for this client."), "invalid_request"
    )


def same_but_port(first, second):
    a, b = urlsplit(first), urlsplit(second)
    return (a.scheme, a.hostname, a.path, a.query) == (
        b.scheme,
        b.hostname,
        b.path,
        b.query,
    )


def get_client(client_id, request=None):
    """The client for ``client_id``; a pre-registered client wins over a
    metadata document at the same URL. With ``request``, fetching documents
    is rate limited per client IP."""
    if not isinstance(client_id, str) or not client_id or len(client_id) > 500:
        raise ClientError(_("Unknown client."))
    client = Client.objects.filter(client_id=client_id).first()
    if client is not None and client.kind != Client.METADATA_DOCUMENT:
        return client
    if conf.get("CLIENT_ID_METADATA_DOCUMENTS") and cimd.is_metadata_url(client_id):
        if client and client.refresh_after and client.refresh_after > timezone.now():
            return client
        return fetch_client(client_id, request, stale=client)
    raise ClientError(_("Unknown client."))


def fetch_client(url, request=None, stale=None):
    """Fetches at most ``METADATA_FETCH_RATE`` documents per client IP and per
    host, so the endpoint cannot be used to send requests elsewhere. A failed
    URL waits a minute. While throttled or failing, a stale copy is used, so
    known clients keep working when their host is briefly down."""
    # a stale copy outlives its cache period by at most the longest one
    longest = timedelta(seconds=conf.get("METADATA_CACHE")[2])
    if stale is not None and (
        stale.refresh_after is None or stale.refresh_after + longest <= timezone.now()
    ):
        stale = None
    failed = f"xprez_mcp_oauth:metadata_failed:{hash_key(url)}"
    if cache.get(failed):
        if stale is not None:
            return stale
        raise ClientError(_("The client's metadata document could not be fetched."))
    rate = conf.get("METADATA_FETCH_RATE")
    if (request is not None and throttled("metadata_ip", client_ip(request), rate)) or (
        throttled("metadata_host", urlsplit(url).hostname, rate)
    ):
        if stale is not None:
            return stale
        raise ClientError(_("Too many requests; try again in a minute."))
    try:
        return fetch_document_client(url)
    except ClientError:
        cache.set(failed, True, 60)
        if stale is not None:
            return stale
        raise


def fetch_document_client(url):
    document, max_age = cimd.fetch(url)
    fields = parse_metadata_document(url, document)
    shortest, default, longest = conf.get("METADATA_CACHE")
    seconds = default if max_age is None else min(max(max_age, shortest), longest)
    client, _created = Client.objects.update_or_create(
        client_id=url,
        defaults={
            **fields,
            "kind": Client.METADATA_DOCUMENT,
            "metadata": document,
            "refresh_after": timezone.now() + timedelta(seconds=seconds),
        },
    )
    return client


def parse_metadata_document(url, document):
    if not isinstance(document, dict):
        raise ClientError(_("The client's metadata document is not a JSON object."))
    if document.get("client_id") != url:
        raise ClientError(
            _("The client_id in the metadata document does not match its URL.")
        )
    redirect_uris = document.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise ClientError(_("The client's metadata document has no redirect URIs."))
    for uri in redirect_uris:
        validate_redirect_uri(uri)
    if document.get("token_endpoint_auth_method", "none") != "none":
        raise ClientError(
            _("Only public clients can use a metadata document here."),
        )
    name = document.get("client_name")
    if not isinstance(name, str) or not name.strip():
        name = urlsplit(url).hostname
    return {
        "name": name.strip()[:200],
        "redirect_uris": redirect_uris,
        "auth_method": "none",
        "secret_hash": "",
    }


def register_client(data):
    """``(client, secret)`` for a registration request (RFC 7591); ``secret``
    is None for public clients."""
    if not isinstance(data, dict):
        raise ClientError(_("Invalid client metadata."), "invalid_client_metadata")
    redirect_uris = data.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise ClientError(_("redirect_uris is required."), "invalid_redirect_uri")
    for uri in redirect_uris:
        validate_redirect_uri(uri)
    auth_method = data.get("token_endpoint_auth_method", "client_secret_basic")
    if auth_method not in AUTH_METHODS:
        raise ClientError(
            _("Unsupported token_endpoint_auth_method."), "invalid_client_metadata"
        )
    grant_types = data.get("grant_types", ["authorization_code"])
    response_types = data.get("response_types", ["code"])
    if (
        not isinstance(grant_types, list)
        or not set(grant_types) <= GRANT_TYPES
        or "authorization_code" not in grant_types
        or response_types != ["code"]
    ):
        raise ClientError(
            _("Only the authorization code flow is supported."),
            "invalid_client_metadata",
        )
    name = data.get("client_name")
    name = name.strip()[:200] if isinstance(name, str) else ""
    client = Client(
        client_id=f"mcpc_{secrets.token_urlsafe(16)}",
        kind=Client.REGISTERED,
        name=name,
        redirect_uris=redirect_uris,
        auth_method=auth_method,
        metadata={
            key: value
            for key, value in data.items()
            if key in REGISTERED_FIELDS and isinstance(value, (str, list))
        },
    )
    secret = None if auth_method == "none" else client.set_secret()
    client.save()
    return client, secret
