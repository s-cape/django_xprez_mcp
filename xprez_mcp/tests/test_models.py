import datetime
import unittest

from django.contrib.auth.models import Permission, User
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from xprez_mcp import MCPServer, ModelProvider
from xprez_mcp.context import CallContext
from xprez_mcp.model_provider import field_annotation

from .test_protocol import MCPTestCase
from .testapp.models import Client, Industry, Tag

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
        server = MCPServer(name="clash", models="generic")
        server.register(Client, type("A", (ModelProvider,), {"fields": ["name"]}))
        server.register(
            type("B", (ModelProvider,), {"fields": ["name"], "model": Client})
        )
        with self.assertRaisesMessage(ImproperlyConfigured, "named 'clients'"):
            server.catalog

    def test_field_annotations(self):
        field = Client._meta.get_field
        self.assertIs(field_annotation(field("name")), str)
        self.assertEqual(field_annotation(field("industry")), int | None)
        self.assertEqual(field_annotation(field("tags")), list[int])
        self.assertEqual(field_annotation(field("founded")), datetime.date | None)


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
