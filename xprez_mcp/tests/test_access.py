from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from xprez_mcp import MCPServer
from xprez_mcp.checks import check_servers
from xprez_mcp.context import CallContext
from xprez_mcp.permissions import check_permission

from .test_protocol import MCPTestCase

PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
RESOLVER = "xprez_mcp.tests.testapp.tokens.resolve"


class ServerAccessTests(MCPTestCase):
    def test_closed_without_permission(self):
        with override_settings(XPREZ_MCP={"TOKEN_RESOLVER": RESOLVER}):
            response = self.post(PING)
        self.assertEqual(response.status_code, 403)

    def test_permission_string(self):
        settings = {"TOKEN_RESOLVER": RESOLVER, "PERMISSION": "auth.view_user"}
        with override_settings(XPREZ_MCP=settings):
            self.assertEqual(self.post(PING).status_code, 403)
            self.assertEqual(self.post(PING, token="token-bob").status_code, 200)

    def test_checked_before_initialize(self):
        settings = {"TOKEN_RESOLVER": RESOLVER, "PERMISSION": "staff"}
        message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        with override_settings(XPREZ_MCP=settings):
            self.assertEqual(self.post(message).status_code, 403)

    def test_server_argument_replaces_setting(self):
        server = MCPServer(name="x", permission="staff")
        self.assertEqual(server.permission, "staff")
        self.assertEqual(MCPServer(name="y").permission, "authenticated")

    def test_anonymous_needs_any(self):
        response = self.post(PING, token=None, url="/open/mcp/")
        self.assertEqual(response.status_code, 200)
        server = MCPServer(name="z", authentication=[], allow_anonymous=True)
        ctx = CallContext(server=server)
        self.assertFalse(server.has_access(ctx))

    def test_per_server_allowed_origins(self):
        server = MCPServer(name="o", allowed_origins=["https://app.example"])
        self.assertEqual(server.allowed_origins, ["https://app.example"])
        self.assertEqual(MCPServer(name="p").allowed_origins, [])


class PermissionTests(SimpleTestCase):
    def ctx(self, user=None):
        return CallContext(server=None, user=user)

    def test_shorthands(self):
        class User:
            is_authenticated = True
            is_staff = False

            def has_perms(self, perms):
                return True

        user = User()
        self.assertTrue(check_permission("any", self.ctx()))
        self.assertFalse(check_permission("authenticated", self.ctx()))
        self.assertTrue(check_permission("authenticated", self.ctx(user)))
        self.assertFalse(check_permission("staff", self.ctx(user)))
        self.assertFalse(check_permission(["authenticated", "staff"], self.ctx(user)))
        self.assertTrue(check_permission(["authenticated", "a.b"], self.ctx(user)))

    def test_unknown_shorthand(self):
        with self.assertRaises(ImproperlyConfigured):
            check_permission("stafff", self.ctx())


class CheckTests(SimpleTestCase):
    def ids(self):
        return [error.id for error in check_servers()]

    def test_configured(self):
        self.assertEqual(self.ids(), [])

    def test_missing_permission(self):
        with override_settings(XPREZ_MCP={}):
            names = {error.obj.name for error in check_servers()}
        # every mounted server relies on the setting except "open"
        self.assertLessEqual({"django", "staff", "models", "generic"}, names)
        self.assertNotIn("open", names)

    def test_unmounted_servers_ignored(self):
        server = MCPServer(name="unmounted")
        with override_settings(XPREZ_MCP={}):
            self.assertNotIn(server, [error.obj for error in check_servers()])

    def test_duplicate_names(self):
        server = MCPServer(name="staff")
        server.urls
        try:
            self.assertEqual(self.ids(), ["xprez_mcp.E002"])
        finally:
            server.mounted = False
