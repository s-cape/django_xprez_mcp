import json

from django.contrib.auth.models import Permission
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase, override_settings

from xprez_mcp import MCPServer
from xprez_mcp.auth import BearerToken
from xprez_mcp.checks import check_servers
from xprez_mcp.core.context import CallContext
from xprez_mcp.permissions import (
    allow_any,
    allow_staff,
    check_permission,
    check_permission_names,
)

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
        settings = {
            "TOKEN_RESOLVER": RESOLVER,
            "PERMISSION_CALLBACK": "xprez_mcp.permissions.allow_staff",
        }
        message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        with override_settings(XPREZ_MCP=settings):
            self.assertEqual(self.post(message).status_code, 403)

    def test_permission_and_callback_both_required(self):
        settings = {
            "TOKEN_RESOLVER": RESOLVER,
            "PERMISSION": "auth.change_user",
            "PERMISSION_CALLBACK": "xprez_mcp.permissions.allow_staff",
        }
        with override_settings(XPREZ_MCP=settings):
            # bob is staff but lacks auth.change_user
            self.assertEqual(self.post(PING, token="token-bob").status_code, 403)
            self.staff.user_permissions.add(
                Permission.objects.get(codename="change_user")
            )
            self.assertEqual(self.post(PING, token="token-bob").status_code, 200)

    def test_server_argument_replaces_both_settings(self):
        server = MCPServer(name="x", permission_callback=allow_staff)
        self.assertIsNone(server.permission)
        self.assertIs(server.permission_callback, allow_staff)
        server = MCPServer(name="x", permission="auth.view_user")
        self.assertEqual(server.permission, "auth.view_user")
        self.assertIsNone(server.permission_callback)
        default = MCPServer(name="y")
        self.assertIsNone(default.permission)
        self.assertEqual(default.permission_callback.__name__, "allow_authenticated")

    def test_server_permission_takes_names_only(self):
        server = MCPServer(name="x", permission=allow_staff)
        with self.assertRaisesMessage(ImproperlyConfigured, "permission_callback"):
            server.has_access(CallContext(server=server))

    def test_anonymous_needs_allow_any(self):
        response = self.post(PING, token=None, url="/open/mcp/")
        self.assertEqual(response.status_code, 200)
        server = MCPServer(name="z", authentication=[], allow_anonymous=True)
        ctx = CallContext(server=server)
        self.assertFalse(server.has_access(ctx))

    def test_rejected_token_not_anonymous(self):
        server = MCPServer(
            name="public",
            authentication=[BearerToken(resolver=lambda request, token: None)],
            allow_anonymous=True,
            permission_callback=allow_any,
        )
        view = server.get_view()
        body = json.dumps(PING)
        request = RequestFactory().post("/", body, content_type="application/json")
        self.assertEqual(view(request).status_code, 200)
        request = RequestFactory().post(
            "/", body, content_type="application/json", HTTP_AUTHORIZATION="Bearer x"
        )
        response = view(request)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], "Bearer")

    def test_per_server_allowed_origins(self):
        server = MCPServer(name="o", allowed_origins=["https://app.example"])
        self.assertEqual(server.allowed_origins, ["https://app.example"])
        self.assertEqual(MCPServer(name="p").allowed_origins, [])


class PermissionTests(SimpleTestCase):
    def ctx(self, user=None):
        return CallContext(server=None, user=user)

    def test_names_and_callables(self):
        class User:
            is_authenticated = True
            is_staff = False

            def has_perms(self, perms):
                return True

        user = User()
        self.assertTrue(check_permission(allow_any, self.ctx()))
        self.assertFalse(check_permission(allow_staff, self.ctx(user)))
        self.assertFalse(check_permission("a.b", self.ctx()))
        self.assertTrue(check_permission(["a.b", "c.d"], self.ctx(user)))

    def test_shorthands_removed(self):
        for permission in ("staff", ["a.b", "any"], [allow_staff]):
            with self.assertRaisesMessage(ImproperlyConfigured, "allow_staff"):
                check_permission(permission, self.ctx())

    def test_names_only(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "permission_callback"):
            check_permission_names(allow_staff, self.ctx())


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
