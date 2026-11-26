import json

from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from io import StringIO
from xprez_mcp import MCPServer, mcp
from xprez_mcp.permissions import allow_authenticated


class MCPTestCase(TestCase):
    url = "/mcp/"

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("alice")
        cls.staff = User.objects.create_user("bob", is_staff=True)
        cls.staff.user_permissions.add(Permission.objects.get(codename="view_user"))

    def post(self, body, token="token-alice", url=None, **headers):
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        return self.client.post(
            url or self.url, data, content_type="application/json", **headers
        )

    def rpc(self, method, params=None, **kwargs):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        response = self.post(message, **kwargs)
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["id"], 1)
        return payload

    def result(self, method, params=None, **kwargs):
        payload = self.rpc(method, params, **kwargs)
        self.assertNotIn("error", payload)
        return payload["result"]

    def error(self, method, params=None, **kwargs):
        payload = self.rpc(method, params, **kwargs)
        self.assertNotIn("result", payload)
        return payload["error"]

    def call(self, name, arguments=None, **kwargs):
        params = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.result("tools/call", params, **kwargs)


class TransportTests(MCPTestCase):
    def test_requires_token(self):
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], "Bearer")

    def test_rejects_invalid_token(self):
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="x")
        self.assertEqual(response.status_code, 401)

    def test_rejects_inactive_user(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(response.status_code, 401)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_requires_json(self):
        response = self.client.post(
            self.url, "{}", content_type="text/plain", HTTP_AUTHORIZATION="Bearer x"
        )
        self.assertEqual(response.status_code, 415)

    def test_origin(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        response = self.post(message, HTTP_ORIGIN="https://evil.example")
        self.assertEqual(response.status_code, 403)
        response = self.post(message, HTTP_ORIGIN="http://testserver")
        self.assertEqual(response.status_code, 200)
        with override_settings(
            XPREZ_MCP={
                "TOKEN_RESOLVER": "xprez_mcp.tests.testapp.tokens.resolve",
                "PERMISSION": allow_authenticated,
                "ALLOWED_ORIGINS": ["https://app.example"],
            }
        ):
            response = self.post(message, HTTP_ORIGIN="https://app.example")
        self.assertEqual(response.status_code, 200)

    def test_protocol_version_header(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        response = self.post(message, HTTP_MCP_PROTOCOL_VERSION="1999-01-01")
        self.assertEqual(response.status_code, 400)
        response = self.post(message, HTTP_MCP_PROTOCOL_VERSION="2025-06-18")
        self.assertEqual(response.status_code, 200)
        # notifications are checked too
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        response = self.post(notification, HTTP_MCP_PROTOCOL_VERSION="1999-01-01")
        self.assertEqual(response.status_code, 400)

    def test_initialize_with_newer_version(self):
        """initialize negotiates in its body; the header is not rejected."""
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2099-01-01",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        response = self.post(message, HTTP_MCP_PROTOCOL_VERSION="2099-01-01")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["result"]["protocolVersion"], mcp.protocol_versions[0]
        )

    def test_unsupported_version_without_token(self):
        """Authentication comes first: no token means 401, not 400."""
        message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        response = self.post(
            message, token=None, HTTP_MCP_PROTOCOL_VERSION="1999-01-01"
        )
        self.assertEqual(response.status_code, 401)

    def test_notifications_and_responses(self):
        response = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(response.status_code, 202)
        response = self.post({"jsonrpc": "2.0", "id": 5, "result": {}})
        self.assertEqual(response.status_code, 202)

    def test_parse_error(self):
        response = self.post("{not json")
        self.assertEqual(response.json()["error"]["code"], -32700)
        response = self.post("[" * 100_000)
        self.assertEqual(response.json()["error"]["code"], -32700)

    def test_invalid_requests(self):
        for body in ([], [{"jsonrpc": "2.0", "id": 1, "method": "ping"}], {"id": 1}):
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEqual(response.json()["error"]["code"], -32600)
        response = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]}
        )
        self.assertEqual(response.json()["error"]["code"], -32600)

    def test_unknown_method(self):
        self.assertEqual(self.error("nope")["code"], -32601)

    def test_ping(self):
        self.assertEqual(self.result("ping"), {})


class InitializeTests(MCPTestCase):
    def test_initialize(self):
        result = self.result("initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["serverInfo"], {"name": "test", "version": "0.0.0"})
        self.assertEqual(
            set(result["capabilities"]),
            {"tools", "resources", "prompts", "completions"},
        )
        self.assertNotIn("instructions", result)

    def test_unknown_version_falls_back_to_newest(self):
        result = self.result("initialize", {"protocolVersion": "2030-01-01"})
        self.assertEqual(result["protocolVersion"], "2025-06-18")


class ToolTests(MCPTestCase):
    def tool_names(self, **kwargs):
        return [tool["name"] for tool in self.result("tools/list", **kwargs)["tools"]]

    def test_list(self):
        tools = {tool["name"]: tool for tool in self.result("tools/list")["tools"]}
        self.assertEqual(
            list(tools),
            [
                "notes_search",
                "notes_echo",
                "notes_fail",
                "notes_update",
                "notes_raw",
                "ping",
                "whoami",
            ],
        )
        self.assertEqual(
            tools["notes_search"],
            {
                "name": "notes_search",
                "description": "Search notes.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Text to find"},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": True},
            },
        )
        self.assertEqual(tools["ping"]["title"], "Ping")

    def test_permissions_filter_list(self):
        self.assertNotIn("staff_report", self.tool_names())
        staff_tools = self.tool_names(token="token-bob")
        self.assertIn("staff_report", staff_tools)
        self.assertIn("staff_users", staff_tools)

    def test_permission_on_item(self):
        User.objects.create_user("carol", is_staff=True)
        tools = self.tool_names(token="token-carol")
        self.assertIn("staff_report", tools)
        self.assertNotIn("staff_users", tools)

    def test_hidden_tool_is_unknown(self):
        error = self.error("tools/call", {"name": "staff_report"})
        self.assertEqual(error["code"], -32602)
        self.assertEqual(error["message"], "Unknown tool: staff_report")
        self.assertEqual(
            self.error("tools/call", {"name": "hidden_tool"})["code"], -32602
        )

    def test_call(self):
        result = self.call("notes_search", {"query": "first"})
        self.assertFalse(result["isError"])
        self.assertEqual(
            result["structuredContent"], {"notes": [{"id": 1, "text": "First note"}]}
        )
        self.assertEqual(
            json.loads(result["content"][0]["text"]), result["structuredContent"]
        )

    def test_call_text_result(self):
        result = self.call("ping")
        self.assertEqual(result["content"], [{"type": "text", "text": "pong"}])
        self.assertNotIn("structuredContent", result)

    def test_context_user(self):
        self.assertEqual(
            self.call("whoami")["structuredContent"], {"username": "alice"}
        )

    def test_converted_arguments(self):
        result = self.call("notes_echo", {"day": "2026-11-13", "color": "blue"})
        self.assertEqual(
            result["structuredContent"],
            {"day": "2026-11-13", "color": "blue", "tags": None},
        )

    def test_invalid_arguments(self):
        result = self.call("notes_search", {"limit": 1})
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["content"][0]["text"], "Missing required argument: query."
        )

    def test_raw_schema(self):
        result = self.call("notes_raw", {"raw": "x"})
        self.assertEqual(result["structuredContent"], {"received": {"raw": "x"}})
        self.assertTrue(self.call("notes_raw", {"raw": 1})["isError"])

    def test_errors(self):
        cases = {
            "tool": {"error": "Nope.", "hint": "try again"},
            "denied": "Permission denied.",
            "missing": "No such note.",
            "invalid": "Bad value.",
            "crash": "Internal error.",
        }
        for kind, expected in cases.items():
            with self.subTest(kind=kind):
                result = self.call("notes_fail", {"kind": kind})
                self.assertTrue(result["isError"])
                text = result["content"][0]["text"]
                if isinstance(expected, dict):
                    self.assertEqual(json.loads(text), expected)
                else:
                    self.assertEqual(text, expected)

    def test_error_handler(self):
        result = self.call("notes_update", {"id": 3})
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"],
            {"error": "Version conflict.", "current": {"id": 3, "version": 2}},
        )


class ResourceTests(MCPTestCase):
    def test_list(self):
        self.assertEqual(
            self.result("resources/list")["resources"],
            [
                {
                    "uri": "notes://all",
                    "name": "all_notes",
                    "description": "All notes.",
                    "mimeType": "application/json",
                }
            ],
        )
        self.assertEqual(
            self.result("resources/templates/list")["resourceTemplates"],
            [{"uriTemplate": "notes://{id}", "name": "note", "mimeType": "text/plain"}],
        )

    def test_read(self):
        contents = self.result("resources/read", {"uri": "notes://2"})["contents"]
        self.assertEqual(
            contents,
            [{"uri": "notes://2", "mimeType": "text/plain", "text": "Second note"}],
        )
        contents = self.result("resources/read", {"uri": "notes://all"})["contents"]
        self.assertEqual(
            json.loads(contents[0]["text"]), {"1": "First note", "2": "Second note"}
        )

    def test_not_found(self):
        for uri in ("notes://9", "notes://abc", "other://1"):
            with self.subTest(uri=uri):
                error = self.error("resources/read", {"uri": uri})
                self.assertEqual(error["code"], -32002)
                self.assertEqual(error["data"], {"uri": uri})

    def test_missing_uri(self):
        self.assertEqual(self.error("resources/read", {})["code"], -32602)


class PromptTests(MCPTestCase):
    def test_list(self):
        prompts = self.result("prompts/list")["prompts"]
        self.assertEqual(
            prompts,
            [
                {
                    "name": "notes_summarize",
                    "description": "Summarize a note.",
                    "arguments": [
                        {"name": "id", "required": True, "description": "Note id"},
                        {"name": "style", "required": False},
                    ],
                },
                {"name": "greeting", "arguments": []},
            ],
        )

    def test_get(self):
        result = self.result(
            "prompts/get", {"name": "notes_summarize", "arguments": {"id": "2"}}
        )
        self.assertEqual(result["description"], "Summarize a note.")
        self.assertEqual(
            result["messages"],
            [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": "Write a short summary of notes://2.",
                    },
                }
            ],
        )

    def test_get_messages(self):
        result = self.result("prompts/get", {"name": "greeting"})
        self.assertEqual(
            [message["role"] for message in result["messages"]], ["user", "assistant"]
        )

    def test_errors(self):
        cases = [
            {"name": "nope"},
            {"name": "notes_summarize"},
            {"name": "notes_summarize", "arguments": {"id": "1", "x": "y"}},
            {"name": "notes_summarize", "arguments": {"id": "one"}},
            {"name": "notes_summarize", "arguments": {"id": "1", "style": "huge"}},
        ]
        for params in cases:
            with self.subTest(params=params):
                self.assertEqual(self.error("prompts/get", params)["code"], -32602)


class CompletionTests(MCPTestCase):
    def complete(self, ref, name, value):
        params = {"ref": ref, "argument": {"name": name, "value": value}}
        return self.result("completion/complete", params)["completion"]

    def test_prompt_argument(self):
        completion = self.complete(
            {"type": "ref/prompt", "name": "notes_summarize"}, "style", "l"
        )
        self.assertEqual(completion, {"values": ["long"], "total": 1, "hasMore": False})

    def test_resource_template(self):
        completion = self.complete(
            {"type": "ref/resource", "uri": "notes://{id}"}, "id", ""
        )
        self.assertEqual(completion["values"], ["1", "2"])

    def test_without_completion(self):
        completion = self.complete(
            {"type": "ref/prompt", "name": "notes_summarize"}, "id", "1"
        )
        self.assertEqual(completion["values"], [])

    def test_unknown_reference(self):
        params = {
            "ref": {"type": "ref/prompt", "name": "nope"},
            "argument": {"name": "x"},
        }
        self.assertEqual(self.error("completion/complete", params)["code"], -32602)


class OtherServerTests(MCPTestCase):
    def test_session_server(self):
        response = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            token="token-bob",
            url="/staff/mcp/",
        )
        self.assertEqual(response.status_code, 401)

        self.client.force_login(self.staff)
        result = self.result("initialize", token=None, url="/staff/mcp/")
        self.assertEqual(result["serverInfo"]["name"], "staff")
        self.assertEqual(result["instructions"], "Staff tools.")
        tools = self.result("tools/list", token=None, url="/staff/mcp/")["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools], ["staff_report", "staff_users"]
        )

    def test_anonymous_server(self):
        result = self.call("hello", {"name": "you"}, token=None, url="/open/mcp/")
        self.assertEqual(result["content"][0]["text"], "Hello you")


class InspectCommandTests(MCPTestCase):
    def test_lists_items(self):
        out = StringIO()
        call_command("mcp_inspect", stdout=out)
        output = out.getvalue()
        self.assertIn("notes_search", output)
        self.assertIn("xprez_mcp.tests.testapp.mcp.NotesProvider", output)
        self.assertIn("notes://{id}", output)
        self.assertNotIn("(hidden)", output)

    def test_marks_hidden_for_user(self):
        out = StringIO()
        call_command("mcp_inspect", user="alice", stdout=out)
        line = next(
            line for line in out.getvalue().splitlines() if "staff_report" in line
        )
        self.assertTrue(line.endswith("(hidden)"))


class MalformedParamsTests(MCPTestCase):
    """Wrong shapes are invalid params, not internal errors."""

    def test_invalid_params(self):
        cases = [
            ("tools/call", {"name": ["notes_search"]}),
            ("tools/call", {"name": {"a": 1}}),
            ("prompts/get", {"name": ["x"]}),
            ("completion/complete", {"ref": "notes"}),
            ("completion/complete", {"ref": {"type": "ref/prompt"}, "argument": "x"}),
            (
                "completion/complete",
                {
                    "ref": {"type": "ref/prompt", "name": "notes_summarize"},
                    "argument": {"name": "style", "value": 1},
                },
            ),
            (
                "completion/complete",
                {
                    "ref": {"type": "ref/prompt", "name": "notes_summarize"},
                    "argument": {"name": "style", "value": "l"},
                    "context": "x",
                },
            ),
        ]
        for method, params in cases:
            with self.subTest(method=method, params=params):
                self.assertEqual(self.error(method, params)["code"], -32602)

    def test_meta_must_be_an_object(self):
        self.assertEqual(self.result("ping", {"_meta": "x"}), {})

    def test_nan_is_a_parse_error(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                body = (
                    '{"jsonrpc": "2.0", "id": 1, "method": "ping", '
                    f'"params": {{"x": {constant}}}}}'
                )
                self.assertEqual(self.post(body).json()["error"]["code"], -32700)


class ErrorResultTests(TestCase):
    def call(self, func, **options):
        server = MCPServer(name="errors", permission=True)
        server.tool(func, name="run", **options)
        return server.call_tool(server.get_context(None, None), {"name": "run"})

    def test_validation_error_keeps_field_names(self):
        def run(ctx):
            raise ValidationError({"name": ["Required."], "age": ["Too young."]})

        result = self.call(run)
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"],
            {
                "error": "Invalid data. name: Required.; age: Too young.",
                "fields": {"name": ["Required."], "age": ["Too young."]},
            },
        )

    def test_protected_delete(self):
        def run(ctx):
            raise ProtectedError("Cannot delete.", {object(), object()})

        result = self.call(run)
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["content"][0]["text"],
            "This cannot be deleted: 2 related objects depend on it.",
        )

    def test_integrity_error(self):
        def run(ctx):
            User.objects.create(username="twice")
            User.objects.create(username="twice")

        result = self.call(run, atomic=True)
        self.assertTrue(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("conflicts with existing data", text)
        self.assertNotIn("username", text)  # no table or column names
        self.assertFalse(User.objects.filter(username="twice").exists())

    def test_nan_results_are_valid_json(self):
        result = self.call(lambda ctx: {"ratio": float("nan"), "top": float("inf")})
        self.assertEqual(result["structuredContent"], {"ratio": None, "top": None})
        self.assertEqual(
            json.loads(result["content"][0]["text"], parse_constant=self.fail),
            {"ratio": None, "top": None},
        )
