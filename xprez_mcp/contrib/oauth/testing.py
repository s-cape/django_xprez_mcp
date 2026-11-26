"""Test helpers for projects that sign MCP clients in with OAuth."""

from django.urls import reverse

from xprez_mcp import conf as xprez_conf
from xprez_mcp.server import mcp

from . import conf
from .models import ACCESS_PREFIX, AccessToken, Client, Grant, expiry, new_key

TEST_REDIRECT_URI = "http://127.0.0.1/callback"


def access_token(user, url=None, client_name="Test client"):
    """An access token for ``user`` at the MCP endpoint ``url`` (a path or an
    absolute URL; by default the default server's), made directly in the
    database instead of through the sign-in flow."""
    if url is None:
        url = reverse(f"{mcp.name}:endpoint")
    if url.startswith("/"):
        url = (xprez_conf.get("BASE_URL") or "http://testserver").rstrip("/") + url
    client = Client.objects.filter(kind=Client.MANUAL, name=client_name).first()
    if client is None:
        client, _ = Client.objects.create_client(client_name, [TEST_REDIRECT_URI])
    grant, _ = Grant.objects.get_or_create(user=user, client=client, resource=url)
    key, key_hash = new_key(ACCESS_PREFIX)
    AccessToken.objects.create(
        grant=grant,
        key_hash=key_hash,
        expires=expiry(conf.get("ACCESS_TOKEN_LIFETIME")),
    )
    return key
