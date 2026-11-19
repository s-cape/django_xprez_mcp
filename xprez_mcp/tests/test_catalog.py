from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from xprez_mcp import InvalidName, MCPServer, ModelProvider, Provider, tool
from xprez_mcp.checks import check_names
from xprez_mcp.server import MCPServer as ServerClass

from .test_models import ModelTestCase
from .test_protocol import MCPTestCase
from .testapp.models import Client
from .testapp.model_servers import generic_mcp, model_mcp


class PaginationTests(MCPTestCase):
    def all_names(self):
        return [tool["name"] for tool in self.result("tools/list")["tools"]]

    def test_off_by_default(self):
        result = self.result("tools/list")
        self.assertNotIn("nextCursor", result)

    def test_keyset_pages(self):
        everything = self.all_names()
        with override_settings(
            XPREZ_MCP={
                "NAME": "test",
                "PERMISSION": "authenticated",
                "TOKEN_RESOLVER": "xprez_mcp.tests.testapp.tokens.resolve",
                "EXCLUDE": ["hidden_*"],
                "LIST_PAGE_SIZE": 2,
            }
        ):
            names, cursor = [], None
            while True:
                params = {"cursor": cursor} if cursor else {}
                result = self.result("tools/list", params)
                self.assertLessEqual(len(result["tools"]), 2)
                names += [tool["name"] for tool in result["tools"]]
                cursor = result.get("nextCursor")
                if cursor is None:
                    break
            error = self.error("tools/list", {"cursor": "%%%"})
        self.assertEqual(names, sorted(everything))
        self.assertEqual(error["code"], -32602)

    def test_server_argument(self):
        server = MCPServer(list_page_size=1, permission="any")

        @server.prompt
        def first(ctx):
            return "1"

        @server.prompt
        def second(ctx):
            return "2"

        ctx = server.get_context(None, None)
        page = server.list_prompts(ctx, {})
        self.assertEqual([p["name"] for p in page["prompts"]], ["first"])
        page = server.list_prompts(ctx, {"cursor": page["nextCursor"]})
        self.assertEqual([p["name"] for p in page["prompts"]], ["second"])
        self.assertNotIn("nextCursor", page)


class NameTests(SimpleTestCase):
    def server(self, name):
        server = MCPServer(permission="any")
        server.tool(lambda ctx: None, name=name)
        return server

    def test_invalid_names(self):
        for name in ("notes.search", "with space", "x" * 65, "čeština"):
            with self.subTest(name=name):
                with self.assertRaisesMessage(InvalidName, repr(name)):
                    self.server(name).catalog

    def test_invalid_name_is_improperly_configured(self):
        self.assertTrue(issubclass(InvalidName, ImproperlyConfigured))

    def test_check_error(self):
        errors = check_names(self.server("bad.name"))
        self.assertEqual([error.id for error in errors], ["xprez_mcp.E003"])

    def test_check_warns_on_long_names(self):
        errors = check_names(self.server("a" * 41))
        self.assertEqual([error.id for error in errors], ["xprez_mcp.W001"])
        self.assertEqual(errors[0].level, checks.WARNING)
        self.assertEqual(check_names(self.server("a" * 40)), [])

    def test_resources_follow_uri_rules(self):
        server = MCPServer(permission="any")
        server.resource("app://notes/v1.0")(lambda ctx: "x")
        self.assertIn("app://notes/v1.0", server.catalog["resource"])


class ThirdPartyWriteTests(ModelTestCase):
    """The test app stands in for an installed package."""

    def setUp(self):
        patcher = mock.patch.object(
            ServerClass,
            "is_third_party",
            lambda server, config: config.label == "testapp",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for server in (model_mcp, generic_mcp):
            server.clear_cache()
            self.addCleanup(server.clear_cache)

    def test_writes_held_back(self):
        names = set(self.tools(token="token-bob"))
        self.assertEqual(
            names,
            {"clients_list", "clients_get", "stats", "industries_list"}
            | {"industries_get", "tags_list"},
        )
        held = {item.name for item in model_mcp.held_back}
        self.assertEqual(held, {"clients_create", "clients_update", "clients_delete"})

    def test_generic_writes_held_back(self):
        names = set(self.tools(token="token-bob", url="/generic/mcp/"))
        self.assertNotIn("record_update", self.tools(url="/generic/mcp/"))
        self.assertTrue({"models_list", "records_search", "record_get"} <= names)
        self.assertFalse({"record_create", "record_update"} & names)

    def test_allow_writes(self):
        with mock.patch.object(model_mcp, "allow_writes", ["test*"]):
            model_mcp.clear_cache()
            self.assertIn("clients_update", self.tools())
            self.assertEqual(model_mcp.held_back, [])

    def test_inspect_lists_held_back(self):
        out = StringIO()
        call_command(
            "mcp_inspect",
            server="xprez_mcp.tests.testapp.model_servers.model_mcp",
            stdout=out,
        )
        self.assertIn("clients_delete", out.getvalue())
        self.assertIn("(held back: ALLOW_WRITES)", out.getvalue())

    def test_project_apps_unaffected(self):
        server = MCPServer(permission="any")

        @server.tool
        def write(ctx):  # this test module is not inside an installed app
            return "ok"

        self.assertIn("write", server.catalog["tool"])


class ExposureTests(SimpleTestCase):
    def test_per_provider(self):
        server = MCPServer(models="generic")

        class Own(ModelProvider):
            fields = ["slug"]
            exposure = "tools"

        server.register(Client, Own)
        tools = set(server.catalog["tool"])
        self.assertIn("clients_list", tools)
        self.assertNotIn("models_list", tools)  # no generic provider left

    def test_invalid(self):
        class Bad(ModelProvider):
            fields = ["slug"]
            exposure = "both"

        with self.assertRaisesMessage(ImproperlyConfigured, "exposure"):
            Bad(MCPServer(), model=Client)

    def test_decorated_tools_follow_writes_rule(self):
        class Writer(Provider):
            @tool
            def change(self, ctx):
                return "ok"

        server = MCPServer(permission="any")
        server.register(Writer)
        self.assertIn("change", server.catalog["tool"])
