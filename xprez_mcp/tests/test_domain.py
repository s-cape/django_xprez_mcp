"""Hand-written domain tools: lookups, atomic writes, constraints, test helpers."""

import decimal
import importlib.util
import math
import unittest
from typing import Annotated, Optional, Union

from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from xprez_mcp.checks import check_atomic
from xprez_mcp import Length, Lookup, MCPServer, Provider, Range, tool
from xprez_mcp.contrib.oauth.models import AccessToken, Grant
from xprez_mcp.contrib.oauth.testing import access_token
from xprez_mcp.contrib.tokens.models import Token
from xprez_mcp.core.schema import annotation_schema, coerce, from_string
from xprez_mcp.errors import InvalidArguments
from xprez_mcp.testing import McpTestCase

from .testapp.models import Industry, Tag


def get_industry(ctx, value):
    return Industry.objects.get(name=value)


class DomainTestCase(McpTestCase):
    mcp_url = "/domain/mcp/"

    @classmethod
    def setUpTestData(cls):
        cls.mcp_user = User.objects.create_user("alice")
        cls.energy = Industry.objects.create(name="energy")
        cls.locked = Industry.objects.create(name="locked mining")
        cls.red = Tag.objects.create(name="red")
        cls.blue = Tag.objects.create(name="blue")

    def tool_schema(self, name):
        response = self.rpc("tools/list")
        tools = {entry["name"]: entry for entry in response.json()["result"]["tools"]}
        return tools[name]["inputSchema"]


class LookupTests(DomainTestCase):
    def test_schema_shows_input_type(self):
        schema = self.tool_schema("industry_show")
        self.assertEqual(
            schema["properties"]["industry"], {"type": "string", "description": "Name"}
        )
        schema = self.tool_schema("industry_tags")
        self.assertEqual(
            schema["properties"]["tags"],
            {"type": "array", "items": {"type": "integer"}},
        )
        self.assertEqual(
            schema["properties"]["industry"],
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
        )

    def test_resolved(self):
        structured, _ = self.call("industry_show", industry="energy")
        self.assertEqual(structured, {"name": "energy"})

    def test_not_found(self):
        text = self.assertToolError("industry_show", industry="nope")
        self.assertEqual(text, "Industry matching query does not exist.")

    def test_options_passed(self):
        structured, _ = self.call("industry_show", industry="locked mining")
        self.assertEqual(structured, {"name": "locked mining"})
        text = self.assertToolError(
            "industry_rename", industry="locked mining", name="x"
        )
        self.assertEqual(text, "locked mining is read-only.")
        self.call("industry_rename", industry="energy", name="power")
        self.energy.refresh_from_db()
        self.assertEqual(self.energy.name, "power")

    def test_list_and_provider_method(self):
        structured, _ = self.call(
            "industry_tags", tags=[self.blue.pk, self.red.pk], industry="energy"
        )
        self.assertEqual(structured, {"tags": ["blue", "red"], "industry": "energy"})
        structured, _ = self.call("industry_tags", tags=[], industry=None)
        self.assertEqual(structured, {"tags": [], "industry": None})
        text = self.assertToolError("industry_tags", tags=[self.red.pk, 0])
        self.assertEqual(text, "No tag 0.")

    def test_input_type_checked(self):
        text = self.assertToolError("industry_tags", tags=["red"])
        self.assertEqual(text, "tags[0] must be an integer.")

    def test_provider_method_needs_provider(self):
        server = MCPServer(name="lookup-function", permission=True)

        @server.tool
        def tagged(ctx, tag: Annotated[Tag, Lookup("get_tag")]):
            pass

        with self.assertRaisesMessage(ImproperlyConfigured, "Lookup('get_tag')"):
            server.catalog

    def test_missing_provider_method(self):
        class Tags(Provider):
            @tool
            def tagged(self, ctx, tag: Annotated[Tag, Lookup("get_tag")]):
                pass

        server = MCPServer(name="lookup-method", permission=True)
        server.register(Tags)
        with self.assertRaisesMessage(ImproperlyConfigured, "get_tag() method"):
            server.catalog

    def test_tools_only(self):
        server = MCPServer(name="lookup-resource", permission=True)

        @server.resource("industry://{industry}")
        def industry(ctx, industry: Annotated[Industry, Lookup(get_industry)]):
            pass

        with self.assertRaisesMessage(ImproperlyConfigured, "tools only"):
            server.catalog

    def test_union_rejected(self):
        server = MCPServer(name="lookup-union", permission=True)

        @server.tool
        def either(ctx, value: Union[Annotated[Industry, Lookup(get_industry)], int]):
            pass

        with self.assertRaisesMessage(ImproperlyConfigured, "only be optional"):
            server.catalog


class AtomicWriteTests(DomainTestCase):
    def test_error_rolls_back(self):
        text = self.assertToolError("industry_create_and_fail", name="oil")
        self.assertEqual(text, "Failed after writing.")
        self.assertFalse(Industry.objects.filter(name__startswith="oil").exists())

    def test_error_result_rolls_back(self):
        self.assertToolError("industry_create_and_return_error", name="oil")
        self.assertFalse(Industry.objects.filter(name="oil").exists())

    def test_read_only_not_wrapped(self):
        self.assertToolError("industry_read_only_create", name="oil")
        self.assertTrue(Industry.objects.filter(name="oil").exists())

    def test_tool_opts_out(self):
        self.assertToolError("industry_unwrapped_create", name="oil")
        self.assertTrue(Industry.objects.filter(name="oil").exists())

    def test_success_commits(self):
        self.call("industry_rename", industry="energy", name="power")
        self.assertTrue(Industry.objects.filter(name="power").exists())


class AtomicSettingTests(McpTestCase):
    mcp_url = "/plain/mcp/"

    @classmethod
    def setUpTestData(cls):
        cls.mcp_user = User.objects.create_user("alice")

    def test_off_by_default(self):
        self.assertToolError("plain_create_and_fail", name="oil")
        self.assertTrue(Industry.objects.filter(name="oil").exists())

    def test_tool_opts_in(self):
        self.assertToolError("plain_atomic_create_and_fail", name="oil")
        self.assertFalse(Industry.objects.filter(name="oil").exists())

    def test_setting(self):
        with override_settings(
            XPREZ_MCP={"NAME": "test", "ATOMIC_WRITES": True},
        ):
            self.assertToolError("plain_create_and_fail", name="oil")
        self.assertFalse(Industry.objects.filter(name="oil").exists())

    def test_database_alias(self):
        server = MCPServer(name="alias", permission=True, atomic_writes="default")

        @server.tool
        def write(ctx):
            pass

        @server.tool(read_only=True)
        def read(ctx):
            pass

        @server.tool(read_only=True, atomic=True)
        def forced(ctx):
            pass

        @server.tool(atomic="other")
        def unknown(ctx):
            pass

        catalog = server.catalog["tool"]
        self.assertEqual(server.get_tool_database(catalog["write"]), "default")
        self.assertIsNone(server.get_tool_database(catalog["read"]))
        self.assertEqual(server.get_tool_database(catalog["forced"]), "default")
        with self.assertRaisesMessage(ImproperlyConfigured, "not 'other'"):
            server.get_tool_database(catalog["unknown"])
        errors = check_atomic(server)
        self.assertEqual([error.id for error in errors], ["xprez_mcp.E005"])
        self.assertIn("unknown", errors[0].msg)

    def test_invalid_option(self):
        with self.assertRaises(ImproperlyConfigured):

            @tool(atomic=1)
            def write(ctx):
                pass

        with self.assertRaises(ValueError):
            MCPServer(name="bad", atomic_writes=1)


class ConstraintToolTests(DomainTestCase):
    def test_schema(self):
        schema = self.tool_schema("industry_plan")
        self.assertEqual(
            schema["properties"]["weeks"],
            {"type": "integer", "minimum": 1, "maximum": 52},
        )
        self.assertEqual(
            schema["properties"]["names"],
            {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
            },
        )
        rename = self.tool_schema("industry_rename")
        self.assertEqual(
            rename["properties"]["name"],
            {"type": "string", "minLength": 1, "maxLength": 50},
        )

    def test_validated(self):
        text = self.assertToolError("industry_plan", weeks=53)
        self.assertEqual(text, "weeks must be between 1 and 52.")
        text = self.assertToolError("industry_plan", weeks=1, names=[])
        self.assertEqual(text, "names must have between 1 and 3 items.")
        structured, _ = self.call("industry_plan", weeks=52, names=["x", "y"])
        self.assertEqual(structured, {"weeks": 52, "names": ["x", "y"]})


class ConstraintTests(SimpleTestCase):
    def test_schema(self):
        self.assertEqual(
            annotation_schema(Annotated[float, Range(min=0)]),
            {"type": "number", "minimum": 0},
        )
        self.assertEqual(
            annotation_schema(Annotated[str, Length(max=10), "Code"]),
            {"type": "string", "maxLength": 10, "description": "Code"},
        )
        self.assertEqual(
            annotation_schema(Optional[Annotated[int, Range(1, 5)]]),
            {
                "anyOf": [
                    {"type": "integer", "minimum": 1, "maximum": 5},
                    {"type": "null"},
                ]
            },
        )

    def test_misplaced(self):
        with self.assertRaisesMessage(TypeError, "Number limits apply to"):
            annotation_schema(Annotated[str, Range(1, 5)])
        with self.assertRaisesMessage(TypeError, "Length limits apply to"):
            annotation_schema(Annotated[int, Length(1, 5)])

    def test_decimal(self):
        annotation = Annotated[decimal.Decimal, Range(decimal.Decimal("0.1"), 10)]
        self.assertEqual(
            annotation_schema(Annotated[annotation, "Price"]),
            {
                "type": "string",
                "format": "decimal",
                "description": "Price. Must be between 0.1 and 10.",
            },
        )
        with self.assertRaisesMessage(InvalidArguments, "between 0.1 and 10"):
            coerce(annotation, "NaN", "n")
        self.assertEqual(coerce(annotation, "0.1", "n"), decimal.Decimal("0.1"))

    def test_nan(self):
        with self.assertRaisesMessage(InvalidArguments, "n must be at most 5."):
            coerce(Annotated[float, Range(max=5)], float("nan"), "n")
        self.assertTrue(math.isnan(coerce(float, float("nan"), "n")))

    def test_union_tries_every_arm(self):
        annotation = Union[Annotated[int, Range(1, 5)], float]
        self.assertEqual(coerce(annotation, 9, "n"), 9.0)
        annotation = Union[Annotated[int, Range(1, 5)], str]
        with self.assertRaisesMessage(InvalidArguments, "n must be between 1 and 5."):
            coerce(annotation, 9, "n")

    def test_messages(self):
        cases = [
            (Annotated[int, Range(min=1)], 0, "n must be at least 1."),
            (Annotated[int, Range(max=5)], 6, "n must be at most 5."),
            (Annotated[str, Length(2, 2)], "a", "n must be exactly 2 characters long."),
            (Annotated[str, Length(1)], "", "n must be at least 1 character long."),
            (
                Annotated[list[int], Length(max=1)],
                [1, 2],
                "n must have at most 1 item.",
            ),
            (Optional[Annotated[int, Range(1, 5)]], 9, "n must be between 1 and 5."),
        ]
        for annotation, value, message in cases:
            with self.subTest(annotation=annotation):
                with self.assertRaisesMessage(InvalidArguments, message):
                    coerce(annotation, value, "n")
        self.assertEqual(coerce(Optional[Annotated[int, Range(1, 5)]], None, "n"), None)
        self.assertEqual(coerce(Annotated[int, Range(1, 5)], 5, "n"), 5)

    def test_from_string(self):
        self.assertEqual(from_string(Annotated[int, Range(1, 5)], "3", "n"), 3)
        with self.assertRaisesMessage(InvalidArguments, "n must be between 1 and 5."):
            from_string(Annotated[int, Range(1, 5)], "7", "n")

    @unittest.skipUnless(
        importlib.util.find_spec("annotated_types"), "annotated_types not installed"
    )
    def test_annotated_types(self):
        import annotated_types as at

        self.assertEqual(
            annotation_schema(Annotated[int, at.Gt(0), at.Le(10)]),
            {"type": "integer", "exclusiveMinimum": 0, "maximum": 10},
        )
        self.assertEqual(
            annotation_schema(Annotated[int, at.Interval(ge=1, lt=5)]),
            {"type": "integer", "minimum": 1, "exclusiveMaximum": 5},
        )
        self.assertEqual(
            annotation_schema(Annotated[str, at.MinLen(1), at.MaxLen(9)]),
            {"type": "string", "minLength": 1, "maxLength": 9},
        )
        with self.assertRaisesMessage(
            InvalidArguments, "n must be greater than 0 and at most 10."
        ):
            coerce(Annotated[int, at.Gt(0), at.Le(10)], 0, "n")
        self.assertEqual(
            annotation_schema(Annotated[int, at.MultipleOf(5)]),
            {"type": "integer", "multipleOf": 5},
        )
        with self.assertRaisesMessage(InvalidArguments, "n must be a multiple of 5."):
            coerce(Annotated[int, at.MultipleOf(5)], 7, "n")
        # misplaced or unknown: left to whatever library they are meant for
        self.assertEqual(
            annotation_schema(Annotated[str, at.Gt(0), at.Predicate(str.isdigit)]),
            {"type": "string"},
        )
        self.assertEqual(coerce(Annotated[str, at.Gt(0)], "x", "n"), "x")


class TestingHelperTests(McpTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice")
        cls.staff = User.objects.create_user("bob", is_staff=True)

    def test_default_url(self):
        self.assertEqual(self.get_mcp_url(), "/mcp/")
        with self.assertRaisesMessage(ImproperlyConfigured, "cannot sign"):
            self.rpc("ping", user=self.user)
        self.assertEqual(self.rpc("ping", token="token-alice").json()["result"], {})

    def test_oauth_token(self):
        self.mcp_url = "/oauth/mcp/"
        structured, result = self.call("whoami", user=self.user)
        self.assertIsNone(structured)
        self.assertEqual(result["content"][0]["text"], "alice")
        self.call("whoami", user=self.user)
        self.assertEqual(AccessToken.objects.count(), 1)
        grant = Grant.objects.get()
        self.assertEqual(grant.resource, "http://testserver/oauth/mcp/")
        self.assertEqual(grant.client.name, "Test client")

    def test_access_token_reuses_client(self):
        access_token(self.user, "/oauth/mcp/")
        access_token(self.staff, "/oauth/mcp/")
        self.assertEqual(Grant.objects.values("client").distinct().count(), 1)

    def test_stored_token(self):
        self.mcp_url = "/plain/mcp/"
        self.assertEqual(self.rpc("ping", user=self.user).status_code, 200)
        self.assertEqual(Token.objects.get().user, self.user)

    def test_session(self):
        self.mcp_url = "/staff/mcp/"
        self.call("staff_report", user=self.staff)
        self.assertEqual(self.rpc("ping").status_code, 200)  # still logged in

    def test_anonymous(self):
        self.mcp_url = "/oauth/mcp/"
        self.assertEqual(self.rpc("ping").status_code, 401)
        self.mcp_user = self.user
        self.assertEqual(self.rpc("ping").status_code, 200)
        self.assertEqual(self.rpc("ping", user=None).status_code, 401)
        self.mcp_url = "/staff/mcp/"
        self.assertEqual(self.rpc("ping", user=self.staff).status_code, 200)
        self.assertEqual(self.rpc("ping", user=None).status_code, 401)

    def test_token_per_endpoint(self):
        self.mcp_url = "/oauth/mcp/"
        self.call("whoami", user=self.user)
        self.mcp_url = "/domain/mcp/"
        self.call("industry_plan", user=self.user, weeks=1)
        self.assertEqual(Grant.objects.count(), 2)

    def test_failures_reported(self):
        self.mcp_url = "/open/mcp/"
        with self.assertRaisesMessage(AssertionError, "did not fail: Hello world"):
            self.assertToolError("hello")
        with self.assertRaisesMessage(AssertionError, "Unknown tool: nope"):
            self.call("nope")
