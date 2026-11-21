import json
import os
import subprocess
import sys

from django.contrib.auth.models import Permission
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase, override_settings

from xprez_mcp import MCPServer
from xprez_mcp.auth import BearerToken
from xprez_mcp.checks import check_servers
from xprez_mcp.core.context import CallContext
from xprez_mcp.permissions import allow_authenticated, allow_staff, check_permission

from .test_protocol import MCPTestCase

PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
RESOLVER = "xprez_mcp.tests.testapp.tokens.resolve"


class ServerAccessTests(MCPTestCase):
    def test_closed_without_permission(self):
        with override_settings(XPREZ_MCP={"TOKEN_RESOLVER": RESOLVER}):
            response = self.post(PING)
        self.assertEqual(response.status_code, 403)

    def test_function_checking_a_django_permission(self):
        settings = {
            "TOKEN_RESOLVER": RESOLVER,
            "PERMISSION": lambda ctx: ctx.user.has_perm("auth.view_user"),
        }
        with override_settings(XPREZ_MCP=settings):
            self.assertEqual(self.post(PING).status_code, 403)
            # bob has auth.view_user
            self.assertEqual(self.post(PING, token="token-bob").status_code, 200)

    def test_checked_before_initialize(self):
        settings = {
            "TOKEN_RESOLVER": RESOLVER,
            "PERMISSION": allow_staff,
        }
        message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        with override_settings(XPREZ_MCP=settings):
            self.assertEqual(self.post(message).status_code, 403)

    def test_server_argument_replaces_the_setting(self):
        self.assertIs(
            MCPServer(name="x", permission=allow_staff).permission, allow_staff
        )
        default = MCPServer(name="y")
        self.assertEqual(default.permission.__name__, "allow_authenticated")

    def test_true_and_false(self):
        anonymous = CallContext(server=None)
        self.assertTrue(MCPServer(name="x", permission=True).permission(anonymous))
        closed = MCPServer(name="x", permission=False)
        self.assertFalse(closed.has_access(CallContext(server=closed, user=self.staff)))
        with override_settings(
            XPREZ_MCP={"TOKEN_RESOLVER": RESOLVER, "PERMISSION": True}
        ):
            self.assertEqual(self.post(PING).status_code, 200)
        with override_settings(
            XPREZ_MCP={"TOKEN_RESOLVER": RESOLVER, "PERMISSION": False}
        ):
            self.assertEqual(self.post(PING, token="token-bob").status_code, 403)
            ids = [e.id for e in check_servers() if e.obj.name == "django"]
        self.assertEqual(ids, [])  # closed on purpose: no E001

    def test_no_strings(self):
        for permission in (
            "auth.view_user",
            "xprez_mcp.permissions.allow_staff",
            ["auth.view_user"],
        ):
            with self.subTest(permission=permission):
                server = MCPServer(name="x", permission=permission)
                with self.assertRaisesMessage(ImproperlyConfigured, "has_perm(") as e:
                    server.permission
                self.assertIn("Invalid server permission", str(e.exception))

    def test_anonymous_needs_true(self):
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
            permission=True,
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
        self.assertTrue(check_permission(lambda ctx: True, self.ctx()))
        self.assertFalse(check_permission(allow_authenticated, self.ctx()))
        self.assertFalse(check_permission(allow_staff, self.ctx(user)))
        self.assertFalse(check_permission("a.b", self.ctx()))
        self.assertTrue(check_permission(["a.b", "c.d"], self.ctx(user)))

    def test_shorthands_removed(self):
        for permission in ("staff", ["a.b", "any"], [allow_staff]):
            with self.assertRaisesMessage(ImproperlyConfigured, "allow_staff"):
                check_permission(permission, self.ctx())

    def test_importable_from_settings(self):
        # settings.py imports allow_staff before Django is configured
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        code = "from xprez_mcp.permissions import allow_staff"
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_none_allows(self):
        self.assertTrue(check_permission(None, self.ctx()))


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

    def test_invalid_permission(self):
        with override_settings(XPREZ_MCP={"PERMISSION": "auth.view_user"}):
            errors = [error for error in check_servers() if error.obj.name == "django"]
        self.assertEqual([error.id for error in errors], ["xprez_mcp.E004"])
        self.assertIn("has_perm", errors[0].msg)

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
