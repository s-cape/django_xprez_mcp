"""Test helpers: ``McpTestCase`` calls a server as an MCP client would."""

import json

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase
from django.urls import resolve, reverse

from . import auth
from .server import mcp

OAUTH_APP = "xprez_mcp.contrib.oauth"
TOKENS_APP = "xprez_mcp.contrib.tokens"
# ``user`` left out: ``mcp_user``. ``user=None`` is an anonymous request.
DEFAULT = object()


class McpTestCase(TestCase):
    """A ``TestCase`` with JSON-RPC helpers for the server at ``mcp_url``
    (by default the default server's endpoint).

    ``user=`` (by default ``mcp_user``) signs the request in through the
    server's own authentication: an OAuth access token, a stored token, or
    the session, whichever of its backends comes first. ``user=None`` sends
    an anonymous request. Override ``mcp_credentials`` for other backends.
    """

    mcp_url = None
    mcp_user = None
    mcp_client_name = "Test client"

    def get_mcp_url(self):
        return self.mcp_url or reverse(f"{mcp.name}:endpoint")

    def get_mcp_server(self):
        return resolve(self.get_mcp_url()).func.view_initkwargs["server"]

    def mcp_credentials(self, user):
        """Request headers that authenticate ``user``."""
        # tokens are made per endpoint: an OAuth token is valid at one only
        tokens = self.__dict__.setdefault("_mcp_tokens", {})
        url = self.get_mcp_url()
        for backend in self.get_mcp_server().get_authentication_backends():
            if backend is auth.session:
                self.client.force_login(user)
                return {}
            cache_key = (url, id(backend), user.pk)
            key = tokens.get(cache_key)
            if key is None:
                key = self._token_for(backend, user)
                if key is None:
                    continue
                tokens[cache_key] = key
            return {"HTTP_AUTHORIZATION": f"Bearer {key}"}
        raise ImproperlyConfigured(
            f"McpTestCase cannot sign {user!r} in to {self.get_mcp_url()}: pass "
            "token= or override mcp_credentials()."
        )

    def _token_for(self, backend, user):
        if apps.is_installed(OAUTH_APP):
            from .contrib.oauth.auth import AccessTokenBackend

            if isinstance(backend, AccessTokenBackend):
                from .contrib.oauth.testing import access_token

                return access_token(user, self.get_mcp_url(), self.mcp_client_name)
        if isinstance(backend, auth.BearerToken) and apps.is_installed(TOKENS_APP):
            from .contrib.tokens.auth import TokenResolver
            from .contrib.tokens.models import Token

            try:
                resolver = backend.get_resolver()
            except ImproperlyConfigured:
                return None
            if isinstance(resolver, TokenResolver):
                return Token.objects.create_token(user, self.mcp_client_name)[1]
        return None

    def rpc(self, method, params=None, user=DEFAULT, token=None):
        """POST one JSON-RPC request; returns the response."""
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        headers = {}
        if user is DEFAULT:
            user = self.mcp_user
        elif user is None:
            self.client.logout()  # also anonymous on session servers
        if token is not None:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        elif user is not None:
            headers = self.mcp_credentials(user)
        return self.client.post(
            self.get_mcp_url(),
            json.dumps(message),
            content_type="application/json",
            **headers,
        )

    def call(self, name, /, user=DEFAULT, **arguments):
        """Call a tool that must succeed: ``(structuredContent, result)``."""
        result = self._tool_result(name, user, arguments)
        if result.get("isError"):
            self.fail(f"Tool {name} failed: {_text(result)}")
        return result.get("structuredContent"), result

    def assertToolError(self, name, /, user=DEFAULT, **arguments):
        """Call a tool that must fail; returns the error text."""
        result = self._tool_result(name, user, arguments)
        if not result.get("isError"):
            self.fail(f"Tool {name} did not fail: {_text(result)}")
        return _text(result)

    def _tool_result(self, name, user, arguments):
        response = self.rpc(
            "tools/call", {"name": name, "arguments": arguments}, user=user
        )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        if "error" in payload:
            self.fail(f"tools/call {name}: {payload['error']}")
        return payload["result"]


def _text(result):
    return "\n".join(
        block["text"] for block in result.get("content", []) if "text" in block
    )
