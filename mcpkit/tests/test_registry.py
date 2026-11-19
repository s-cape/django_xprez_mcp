from django.contrib.auth.models import Group
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from mcpkit import (
    AlreadyRegistered,
    MCPServer,
    NotRegistered,
    Provider,
    mcp,
    prompt,
    resource,
    tool,
)
from mcpkit.context import CallContext


class Base(Provider):
    namespace = "base"

    @tool
    def one(self, ctx):
        return 1

    @tool
    def two(self, ctx):
        return 2

    @resource("base://info")
    def info(self, ctx):
        return "info"

    @prompt
    def ask(self, ctx):
        return "ask"


class Child(Base):
    namespace = "child"
    two = None

    @tool
    def one(self, ctx):
        return super().one(ctx) + 10

    @tool
    def three(self, ctx):
        return 3


class Undecorated(Base):
    def one(self, ctx):
        return "plain method"


def names(server, kind="tool"):
    return list(server.catalog[kind])


def context(server, user=None):
    return CallContext(server=server, user=user)


class RegistrationTests(SimpleTestCase):
    def test_register_provider_class(self):
        server = MCPServer("t")
        self.assertIs(server.register(Base), Base)
        self.assertTrue(server.is_registered(Base))
        self.assertEqual(names(server), ["base_one", "base_two"])
        self.assertEqual(names(server, "resource"), ["base://info"])
        self.assertEqual(names(server, "prompt"), ["base_ask"])

    def test_register_as_decorator(self):
        server = MCPServer("t")

        @server.register
        class Decorated(Provider):
            @tool
            def hello(self, ctx):
                pass

        self.assertTrue(server.is_registered(Decorated))
        self.assertEqual(names(server), ["hello"])

    def test_register_model_key(self):
        server = MCPServer("t")
        server.register(Group, Base)
        self.assertIs(server.get_provider(Group).model, Group)

        server2 = MCPServer("t2")

        @server2.register(Group)
        class GroupProvider(Provider):
            pass

        self.assertIsInstance(server2.get_provider(Group), GroupProvider)

    def test_register_twice(self):
        server = MCPServer("t")
        server.register(Base)
        with self.assertRaises(AlreadyRegistered):
            server.register(Base)

    def test_register_rejects_non_providers(self):
        server = MCPServer("t")
        with self.assertRaises(TypeError):
            server.register(Group, object)

    def test_unregister(self):
        server = MCPServer("t")
        server.register(Base)
        self.assertEqual(len(names(server)), 2)
        server.unregister(Base)
        self.assertEqual(names(server), [])
        with self.assertRaises(NotRegistered):
            server.unregister(Base)

    def test_unregister_single_items(self):
        server = MCPServer("t")
        server.register(Base)
        server.unregister_tool("base_two")
        server.unregister_resource("base://info")
        server.unregister_prompt("base_ask")
        self.assertEqual(names(server), ["base_one"])
        self.assertEqual(names(server, "resource"), [])
        self.assertEqual(names(server, "prompt"), [])

    def test_functions(self):
        server = MCPServer("t")

        @server.tool(name="renamed")
        def original(ctx):
            return "x"

        @server.resource("x://{id}")
        def thing(ctx, id: int):
            pass

        @server.prompt
        def greet(ctx):
            pass

        self.assertEqual(names(server), ["renamed"])
        self.assertEqual(names(server, "resource"), ["x://{id}"])
        self.assertEqual(names(server, "prompt"), ["greet"])
        self.assertEqual(original(None), "x")

    def test_duplicate_names(self):
        server = MCPServer("t")

        @server.tool
        def base_one(ctx):
            pass

        server.register(Base)
        with self.assertRaisesMessage(ImproperlyConfigured, "Duplicate MCP tool"):
            server.catalog

    def test_servers_are_independent(self):
        first, second = MCPServer("a"), MCPServer("b")
        first.register(Base)
        self.assertEqual(names(second), [])


class InheritanceTests(SimpleTestCase):
    def test_override_remove_and_add(self):
        server = MCPServer("t")
        server.register(Child)
        self.assertEqual(names(server), ["child_one", "child_three"])
        ctx = context(server)
        self.assertEqual(server.catalog["tool"]["child_one"].call(ctx, {}), 11)

    def test_override_without_decorator_removes(self):
        server = MCPServer("t")
        server.register(Undecorated)
        self.assertEqual(names(server), ["base_two"])

    def test_parent_unchanged(self):
        self.assertEqual(
            [spec.name for spec in Base.get_specs()], ["one", "two", "info", "ask"]
        )


class ExcludeTests(SimpleTestCase):
    def test_server_exclude(self):
        server = MCPServer("t", exclude=["base_t*", "base://*"])
        server.register(Base)
        self.assertEqual(names(server), ["base_one"])
        self.assertEqual(names(server, "resource"), [])

    def test_settings_exclude(self):
        server = MCPServer("t")
        server.register(Base)
        self.assertEqual(len(names(server)), 2)
        with override_settings(MCPKIT={"EXCLUDE": ["base_one"]}):
            self.assertEqual(names(server), ["base_two"])
        self.assertEqual(len(names(server)), 2)

    def test_exclude_apps(self):
        self.assertIn("notes_search", mcp.catalog["tool"])
        with override_settings(MCPKIT={"EXCLUDE_APPS": ["testapp"]}):
            self.assertEqual(names(mcp), [])

    def test_default_settings_exclude(self):
        self.assertNotIn("hidden_tool", mcp.catalog["tool"])


class AutodiscoverTests(SimpleTestCase):
    def test_testapp_registered(self):
        self.assertIn("notes_search", mcp.catalog["tool"])
        self.assertIn("ping", mcp.catalog["tool"])
        item = mcp.catalog["tool"]["notes_search"]
        self.assertEqual(item.app_label, "testapp")
        self.assertEqual(item.source, "mcpkit.tests.testapp.mcp.NotesProvider")

    def test_async_rejected(self):
        with self.assertRaises(ImproperlyConfigured):

            @tool
            async def later(ctx):
                pass

    def test_resource_parameters_checked(self):
        with self.assertRaises(ImproperlyConfigured):

            @resource("x://{id}")
            def missing(ctx):
                pass
