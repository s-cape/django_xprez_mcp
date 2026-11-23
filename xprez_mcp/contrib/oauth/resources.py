"""URLs of a server's endpoint, issuer and OAuth views."""

from urllib.parse import urlsplit, urlunsplit

from django.urls import reverse

from . import conf


def base_url(request):
    base = conf.get("BASE_URL")
    if base:
        return base.rstrip("/")
    return f"{request.scheme}://{request.get_host()}"


def url(request, name, namespace=None):
    """An absolute URL in the server's namespace (by default, the current one)."""
    namespace = namespace or request.resolver_match.namespace
    return base_url(request) + reverse(f"{namespace}:{name}")


def resource(request, namespace=None):
    """The MCP endpoint: what tokens are issued for (RFC 8707)."""
    return url(request, "endpoint", namespace)


def issuer(request, namespace=None):
    """Every server is its own authorization server, named by its endpoint."""
    return resource(request, namespace).rstrip("/")


def canonical(value):
    """For comparing resource URLs: case-insensitive scheme and host, default
    port, no fragment, trailing slash ignored."""
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        return value
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    scheme = parts.scheme.lower()
    if port and port != {"https": 443, "http": 80}.get(scheme):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parts.path.rstrip("/"), parts.query, ""))
