import datetime
import unittest
from typing import Optional

from django.contrib.auth.models import Permission, User
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from xprez_mcp import MCPServer, ModelProvider
from xprez_mcp.core.context import CallContext
from xprez_mcp.permissions import allow_staff
from xprez_mcp.providers.fields import field_annotation

from .test_protocol import MCPTestCase
from .testapp.models import Client, ColoredTag, Industry, Note, Tag

try:
    import rest_framework  # noqa: F401
except ImportError:
    rest_framework = None


class ModelTestCase(MCPTestCase):
    url = "/models/mcp/"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.creator = User.objects.create_user("carol")
        cls.creator.user_permissions.add(Permission.objects.get(codename="add_client"))
        cls.tech = Industry.objects.create(name="Tech")
        cls.food = Industry.objects.create(name="Food")
        cls.vip = Tag.objects.create(name="vip")
        cls.acme = Client.objects.create(
            slug="acme",
            name="Acme",
            group="Acme Group",
            industry=cls.tech,
            size="l",
            founded=datetime.date(2001, 1, 1),
        )
        cls.acme.tags.add(cls.vip)
        cls.bistro = Client.objects.create(
            slug="bistro", name="Bistro", industry=cls.food, owner=cls.staff
        )
        cls.hidden = Client.objects.create(slug="gone", name="Gone", active=False)
        cls.mine = Client.objects.create(
            slug="mine", name="Mine", active=False, owner=cls.user
        )

    def tools(self, **kwargs):
        return {
            tool["name"]: tool for tool in self.result("tools/list", **kwargs)["tools"]
        }

    def structured(self, name, arguments=None, **kwargs):
        result = self.call(name, arguments, **kwargs)
        self.assertFalse(result["isError"], result)
        return result.get("structuredContent")

    def failed(self, name, arguments=None, **kwargs):
        result = self.call(name, arguments, **kwargs)
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]


class ToolsTests(ModelTestCase):
    def test_tool_names(self):
        names = set(self.tools())
        self.assertEqual(
            names,
            {
                "clients_list",
                "clients_get",
                "clients_update",
                "stats",
                "industries_list",
                "industries_get",
            },
        )
        self.assertIn("clients_create", self.tools(token="token-carol"))
        staff = self.tools(token="token-bob")
        self.assertIn("clients_delete", staff)
        self.assertIn("tags_list", staff)

    def test_list_schema(self):
        tool = self.tools()["clients_list"]
        properties = tool["inputSchema"]["properties"]
        self.assertEqual(
            list(properties),
            [
                "query",
                "industry",
                "size",
                "active",
                "founded_after",
                "tag",
                "ordering",
                "limit",
                "cursor",
            ],
        )
        self.assertEqual(properties["size"]["anyOf"][0], {"enum": ["s", "m", "l"]})
        self.assertEqual(properties["founded_after"]["anyOf"][0]["format"], "date")
        self.assertEqual(properties["limit"]["default"], 20)
        self.assertNotIn("required", tool["inputSchema"])
        self.assertEqual(tool["annotations"], {"readOnlyHint": True})
        self.assertIn("query searches name, group", tool["description"])

    def test_write_schemas(self):
        tools = self.tools(token="token-carol")
        create = tools["clients_create"]["inputSchema"]
        self.assertEqual(create["required"], ["slug", "name"])
        self.assertEqual(create["properties"]["tags"]["type"], "array")
        update = tools["clients_update"]["inputSchema"]
        self.assertEqual(update["required"], ["slug"])
        self.assertEqual(
            tools["clients_update"]["annotations"], {"idempotentHint": True}
        )

    def test_list(self):
        data = self.structured("clients_list")
        self.assertEqual(data["count"], 3)
        self.assertIsNone(data["next_cursor"])
        self.assertEqual(
            data["results"][0],
            {
                "slug": "acme",
                "name": "Acme",
                "industry": self.tech.pk,
                "size": "l",
                "active": True,
            },
        )
        # get_queryset: inactive clients only for their owner
        self.assertEqual(
            [row["slug"] for row in data["results"]], ["acme", "bistro", "mine"]
        )

    def test_search(self):
        found = self.structured("clients_list", {"query": "acm"})["results"]
        self.assertEqual([row["slug"] for row in found], ["acme"])
        found = self.structured("clients_list", {"query": "acme group"})["results"]
        self.assertEqual(found, [])  # "=group" is exact per word
        self.assertEqual(
            self.structured("clients_list", {"query": "bistro"})["count"], 1
        )

    def test_filters(self):
        def slugs(arguments):
            data = self.structured("clients_list", arguments)
            return [row["slug"] for row in data["results"]]

        self.assertEqual(slugs({"industry": "tech"}), ["acme"])
        self.assertEqual(slugs({"size": "l"}), ["acme"])
        self.assertEqual(slugs({"active": False}), ["mine"])
        self.assertEqual(slugs({"founded_after": "2000-06-01"}), ["acme"])
        self.assertEqual(slugs({"tag": "vip"}), ["acme"])
        self.assertIn("invalid", self.failed("clients_list", {"size": "xl"}))
        self.assertIn("Unknown argument", self.failed("clients_list", {"nope": 1}))

    def test_ordering(self):
        data = self.structured("clients_list", {"ordering": "-name"})
        self.assertEqual(
            [row["slug"] for row in data["results"]], ["mine", "bistro", "acme"]
        )

    def test_ordering_ends_with_pk(self):
        """Cursors are offsets: rows tied on the ordering would repeat across pages."""
        for arguments in ({}, {"ordering": "-name"}):
            with self.subTest(arguments=arguments):
                with CaptureQueriesContext(connection) as queries:
                    self.structured("clients_list", arguments)
                selects = [q["sql"] for q in queries if " LIMIT " in q["sql"]]
                self.assertRegex(selects[-1], r'"id" ASC LIMIT \d+$')

    def test_pagination(self):
        first = self.structured("clients_list", {"limit": 2})
        self.assertEqual(len(first["results"]), 2)
        self.assertEqual(first["count"], 3)
        second = self.structured(
            "clients_list", {"limit": 2, "cursor": first["next_cursor"]}
        )
        self.assertEqual([row["slug"] for row in second["results"]], ["mine"])
        self.assertIsNone(second["next_cursor"])
        self.assertIn("limit", self.failed("clients_list", {"limit": 500}))
        self.assertIn("cursor", self.failed("clients_list", {"cursor": "!!"}))

    def test_get(self):
        data = self.structured("clients_get", {"slug": "acme"})
        self.assertEqual(data["tags"], [self.vip.pk])
        self.assertEqual(data["label"], "Acme (acme)")
        self.assertEqual(data["group"], "Acme Group")
        self.assertIn(
            "No client with slug 'gone'", self.failed("clients_get", {"slug": "gone"})
        )
        self.assertIn("Missing required", self.failed("clients_get", {}))

    def test_pk_lookup(self):
        tool = self.tools()["industries_get"]
        self.assertEqual(tool["inputSchema"]["properties"]["id"], {"type": "integer"})
        data = self.structured("industries_get", {"id": self.tech.pk})
        self.assertEqual(data, {"id": self.tech.pk, "name": "Tech"})

    def test_create(self):
        arguments = {
            "slug": "new",
            "name": "New",
            "industry": self.food.pk,
            "tags": [self.vip.pk],
            "founded": "2020-02-02",
        }
        self.assertIn(
            "Unknown tool",
            self.error(
                "tools/call", {"name": "clients_create", "arguments": arguments}
            )["message"],
        )
        data = self.structured("clients_create", arguments, token="token-carol")
        self.assertEqual(data["tags"], [self.vip.pk])
        client = Client.objects.get(slug="new")
        self.assertEqual(client.founded, datetime.date(2020, 2, 2))
        self.assertEqual(client.industry, self.food)

    def test_create_validation(self):
        text = self.failed(
            "clients_create", {"slug": "acme", "name": "Dup"}, token="token-carol"
        )
        self.assertIn("slug:", text)
        text = self.failed(
            "clients_create",
            {"slug": "x", "name": "X", "industry": 999},
            token="token-carol",
        )
        self.assertIn("industry:", text)
        self.assertIn(
            "Missing required argument: name",
            self.failed("clients_create", {"slug": "y"}, token="token-carol"),
        )

    def test_update(self):
        data = self.structured("clients_update", {"slug": "acme", "name": "Acme Ltd"})
        self.assertEqual(data["name"], "Acme Ltd")
        self.acme.refresh_from_db()
        self.assertEqual(self.acme.name, "Acme Ltd")
        self.assertEqual(self.acme.size, "l")  # untouched fields stay

    def test_update_object_permission(self):
        text = self.failed("clients_update", {"slug": "bistro", "name": "Mine now"})
        self.assertEqual(text, "You cannot update this client.")
        self.structured(
            "clients_update", {"slug": "bistro", "group": "G"}, token="token-bob"
        )

    def test_delete(self):
        self.assertEqual(
            self.structured("clients_delete", {"slug": "acme"}, token="token-bob"),
            {"deleted": True, "slug": "acme"},
        )
        self.assertFalse(Client.objects.filter(slug="acme").exists())

    def test_provider_permission(self):
        self.assertIn(
            "Unknown tool", self.error("tools/call", {"name": "tags_list"})["message"]
        )

    def test_custom_tool(self):
        self.assertEqual(self.structured("stats"), {"count": 3})


class ResourceTests(ModelTestCase):
    def test_listed(self):
        resources = self.result("resources/list")["resources"]
        self.assertIn("crm://clients", [entry["uri"] for entry in resources])
        templates = self.result("resources/templates/list")["resourceTemplates"]
        self.assertIn(
            "crm://clients/{slug}", [entry["uriTemplate"] for entry in templates]
        )

    def test_read(self):
        result = self.result("resources/read", {"uri": "crm://clients/acme"})
        self.assertIn('"label": "Acme (acme)"', result["contents"][0]["text"])
        result = self.result("resources/read", {"uri": "crm://clients"})
        self.assertIn('"count": 3', result["contents"][0]["text"])
        error = self.error("resources/read", {"uri": "crm://clients/gone"})
        self.assertEqual(error["code"], -32002)

    def test_complete(self):
        result = self.result(
            "completion/complete",
            {
                "ref": {"type": "ref/resource", "uri": "crm://clients/{slug}"},
                "argument": {"name": "slug", "value": "b"},
            },
        )
        self.assertEqual(result["completion"]["values"], ["bistro"])


class GenericTests(ModelTestCase):
    url = "/generic/mcp/"

    def test_fixed_tools(self):
        tools = self.tools()
        self.assertEqual(
            set(tools),
            {"models_list", "records_search", "record_get", "record_update", "stats"},
        )
        model = tools["records_search"]["inputSchema"]["properties"]["model"]
        self.assertEqual(model["enum"], ["clients", "industries"])
        staff = self.tools(token="token-bob")
        self.assertEqual(
            staff["records_search"]["inputSchema"]["properties"]["model"]["enum"],
            ["clients", "industries", "tags"],
        )
        self.assertIn("record_delete", staff)
        self.assertIn("record_create", self.tools(token="token-carol"))

    def test_models_list(self):
        models = {
            entry["model"]: entry for entry in self.structured("models_list")["models"]
        }
        self.assertEqual(set(models), {"clients", "industries"})
        clients = models["clients"]
        self.assertEqual(clients["lookup"], "slug")
        self.assertEqual(clients["actions"], ["list", "get", "update"])
        self.assertTrue(clients["search"])
        self.assertEqual(clients["filters"]["size"], {"enum": ["s", "m", "l"]})
        self.assertIn("founded", clients["writable"])

    def test_search(self):
        data = self.structured(
            "records_search",
            {"model": "clients", "query": "a", "filters": {"size": "l"}},
        )
        self.assertEqual([row["slug"] for row in data["results"]], ["acme"])
        text = self.failed(
            "records_search", {"model": "clients", "filters": {"colour": "red"}}
        )
        self.assertIn("Unknown argument: colour.", text)
        self.assertIn("'industry'", text)
        text = self.failed("records_search", {"model": "tags"})
        self.assertIn("Unknown model 'tags'", text)

    def test_get_and_update(self):
        self.assertEqual(
            self.structured(
                "record_get", {"model": "industries", "id": str(self.tech.pk)}
            )["name"],
            "Tech",
        )
        data = self.structured(
            "record_update", {"model": "clients", "id": "acme", "data": {"name": "A2"}}
        )
        self.assertEqual(data["name"], "A2")
        text = self.failed(
            "record_update", {"model": "clients", "id": "acme", "data": {"nope": 1}}
        )
        self.assertIn("Unknown argument: nope.", text)
        self.assertIn("'founded'", text)

    def test_create_and_delete(self):
        self.structured(
            "record_create",
            {"model": "clients", "data": {"slug": "g", "name": "G"}},
            token="token-carol",
        )
        self.assertTrue(Client.objects.filter(slug="g").exists())
        self.assertEqual(
            self.structured(
                "record_delete", {"model": "clients", "id": "g"}, token="token-bob"
            ),
            {"deleted": True, "slug": "g"},
        )


class OneToOneValueTests(TestCase):
    def provider(self, model, fields):
        cls = type("P", (ModelProvider,), {"fields": fields})
        return cls(MCPServer(name="one-to-one"), model=model)

    def test_parent_link_and_reverse(self):
        colored = ColoredTag.objects.create(name="red", color="#f00")
        child = self.provider(ColoredTag, ["tag_ptr", "color"])
        self.assertEqual(child.field_value(colored, "tag_ptr"), colored.pk)
        parent = self.provider(Tag, ["name", "coloredtag"])
        tag = Tag.objects.get(pk=colored.pk)
        self.assertEqual(parent.field_value(tag, "coloredtag"), colored.pk)
        plain = Tag.objects.create(name="plain")
        self.assertIsNone(parent.field_value(plain, "coloredtag"))

    def test_generic_foreign_key(self):
        provider = self.provider(Note, ["text", "target"])
        tag = Tag.objects.create(name="red")
        note = Note.objects.create(text="hi", target=tag)
        self.assertEqual(provider.field_value(note, "target"), str(tag))
        self.assertIsNone(provider.field_value(Note(text="loose"), "target"))


class ExposureGuardTests(TestCase):
    def test_complete_lookup_needs_list(self):
        Client.objects.create(slug="acme", name="Acme")
        attributes = {
            "fields": ["slug"],
            "lookup_field": "slug",
            "permissions": {"list": allow_staff},
        }
        provider = type("P", (ModelProvider,), attributes)(
            MCPServer(name="complete"), model=Client
        )
        alice = User.objects.create_user("alice")
        bob = User.objects.create_user("bob", is_staff=True)
        for user, expected in [(alice, []), (bob, ["acme"])]:
            ctx = CallContext(server=provider.server, user=user)
            self.assertEqual(list(provider.complete_lookup(ctx, "a")), expected)

    def test_unexposed_model_refused(self):
        from xprez_mcp.contrib.tokens.models import Token

        with self.assertRaisesMessage(ImproperlyConfigured, "mcp_exposed"):
            type("P", (ModelProvider,), {"fields": ["name"]})(
                MCPServer(name="unexposed"), model=Token
            )


class RelatedLabelsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tech = Industry.objects.create(name="Tech")
        cls.vip = Tag.objects.create(name="vip")
        cls.acme = Client.objects.create(slug="acme", name="Acme", industry=cls.tech)
        cls.acme.tags.add(cls.vip)
        cls.bare = Client.objects.create(slug="bare", name="Bare")

    def provider(self, server=None, **attributes):
        attributes.setdefault("fields", ["name", "industry", "tags"])
        cls = type("P", (ModelProvider,), attributes)
        return cls(server or MCPServer(name="labels"), model=Client)

    def test_off_by_default(self):
        provider = self.provider()
        self.assertEqual(provider.field_value(self.acme, "industry"), self.tech.pk)
        self.assertEqual(provider.field_value(self.acme, "tags"), [self.vip.pk])

    def test_provider_option(self):
        provider = self.provider(related_labels=True)
        tech = {"id": self.tech.pk, "display": str(self.tech)}
        vip = {"id": self.vip.pk, "display": str(self.vip)}
        self.assertEqual(provider.field_value(self.acme, "industry"), tech)
        self.assertEqual(provider.field_value(self.acme, "tags"), [vip])
        self.assertIsNone(provider.field_value(self.bare, "industry"))
        self.assertEqual(provider.field_value(self.bare, "tags"), [])

    def test_parent_link_replaced_by_property(self):
        # as django-polymorphic does
        colored = ColoredTag.objects.create(name="red", color="#f00")
        cls = type("P", (ModelProvider,), {"fields": ["tag_ptr"]})
        provider = cls(MCPServer(name="labels", related_labels=True), model=ColoredTag)
        original = ColoredTag.tag_ptr
        ColoredTag.tag_ptr = property(lambda obj: Tag.objects.get(pk=obj.pk))
        try:
            rows = provider.serialize(None, ColoredTag.objects.all(), many=True)
        finally:
            ColoredTag.tag_ptr = original
        tag = Tag.objects.get(pk=colored.pk)
        self.assertEqual(rows, [{"tag_ptr": {"id": tag.pk, "display": str(tag)}}])

    def test_reverse_one_to_one(self):
        colored = ColoredTag.objects.create(name="red", color="#f00")
        cls = type("P", (ModelProvider,), {"fields": ["coloredtag"]})
        provider = cls(MCPServer(name="labels", related_labels=True), model=Tag)
        tag = Tag.objects.get(pk=colored.pk)
        self.assertEqual(
            provider.field_value(tag, "coloredtag"),
            {"id": colored.pk, "display": str(colored)},
        )

    def test_server_and_setting(self):
        server = MCPServer(name="labels", related_labels=True)
        self.assertTrue(self.provider(server).get_related_labels())
        self.assertFalse(
            self.provider(server, related_labels=False).get_related_labels()
        )
        with override_settings(XPREZ_MCP={"RELATED_LABELS": True}):
            self.assertTrue(self.provider().get_related_labels())
            server = MCPServer(name="labels", related_labels=False)
            self.assertFalse(self.provider(server).get_related_labels())

    def test_list_prefetches(self):
        provider = self.provider(related_labels=True)
        queryset = Client.objects.order_by("name")
        with self.assertNumQueries(3):  # clients, industries, tags
            rows = provider.serialize(None, queryset, many=True)
        self.assertEqual(rows[0]["industry"]["display"], str(self.tech))
        self.assertIsNone(rows[1]["industry"])


class ConfigurationTests(SimpleTestCase):
    def provider(self, **attributes):
        attributes.setdefault("fields", ["name"])
        cls = type("P", (ModelProvider,), attributes)
        return cls(MCPServer(name="config"), model=Client)

    def test_needs_fields_or_serializer(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "fields or a serializer"):
            self.provider(fields=None)
        with self.assertRaisesMessage(ImproperlyConfigured, "not both"):
            self.provider(serializer=object)

    def test_needs_model(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "needs a model"):
            type("P", (ModelProvider,), {"fields": ["x"]})(MCPServer(name="m"))

    def test_invalid_options(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "unknown actions"):
            self.provider(actions=["list", "purge"])
        with self.assertRaisesMessage(ImproperlyConfigured, "no field 'code'"):
            self.provider(lookup_field="code")
        with self.assertRaisesMessage(ImproperlyConfigured, "reserved"):
            self.provider(filters=["limit"])
        with self.assertRaisesMessage(ImproperlyConfigured, "not editable"):
            self.provider(write_fields=["name", "label"])

    def test_django_model_permissions(self):
        provider = self.provider(permissions=ModelProvider.django_model_permissions)
        self.assertEqual(provider.get_permissions()["update"], "testapp.change_client")

        class Perms:
            is_authenticated = True

            def has_perms(self, perms):
                return perms == ["testapp.view_client"]

        ctx = CallContext(server=None, user=Perms())
        self.assertTrue(provider.has_action_permission(ctx, "list"))
        self.assertFalse(provider.has_action_permission(ctx, "update"))

    def test_generic_name_clash(self):
        server = MCPServer(name="clash", exposure="generic")
        server.register(Client, type("A", (ModelProvider,), {"fields": ["name"]}))
        server.register(
            type("B", (ModelProvider,), {"fields": ["name"], "model": Client})
        )
        with self.assertRaisesMessage(ImproperlyConfigured, "named 'clients'"):
            server.catalog

    def test_field_annotations(self):
        field = Client._meta.get_field
        self.assertIs(field_annotation(field("name")), str)
        self.assertEqual(field_annotation(field("industry")), Optional[int])
        self.assertEqual(field_annotation(field("tags")), list[int])
        self.assertEqual(field_annotation(field("founded")), Optional[datetime.date])


class FilterNamedActionTests(TestCase):
    """A filter or field may be named ``action``, which is also ``run``'s argument."""

    def test_search_and_list(self):
        class TagProvider(ModelProvider):
            fields = ["id", "name"]
            filters = {"action": "name"}

        generic = MCPServer(name="action-generic", exposure="generic")
        tools = MCPServer(name="action-tools")
        generic.register(Tag, TagProvider)
        tools.register(Tag, TagProvider)
        Tag.objects.create(name="keep")
        Tag.objects.create(name="drop")
        user = User.objects.create_user("dana")
        for server, name, arguments in (
            (
                generic,
                "records_search",
                {"model": "tags", "filters": {"action": "keep"}},
            ),
            (tools, "tags_list", {"action": "keep"}),
        ):
            with self.subTest(name=name):
                result = server.call_tool(
                    CallContext(server=server, user=user),
                    {"name": name, "arguments": arguments},
                )
                self.assertFalse(result["isError"], result)
                found = [row["name"] for row in result["structuredContent"]["results"]]
                self.assertEqual(found, ["keep"])


@unittest.skipIf(rest_framework is None, "Django REST framework is not installed")
class DRFTests(ModelTestCase):
    url = "/drf/mcp/"

    def test_serializer_read_and_write(self):
        data = self.structured("clients_get", {"slug": "acme"})
        self.assertEqual(data["industry_name"], "Tech")
        tool = self.tools()["clients_update"]
        self.assertIn("name", tool["inputSchema"]["properties"])
        self.assertNotIn("industry_name", tool["inputSchema"]["properties"])
        text = self.failed("clients_update", {"slug": "acme", "name": ""})
        self.assertIn("name:", text)
        data = self.structured("clients_update", {"slug": "acme", "name": "Acme 2"})
        self.assertEqual(data["name"], "Acme 2")
