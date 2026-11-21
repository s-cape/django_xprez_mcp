import base64
import json

from django.core.serializers.json import DjangoJSONEncoder


def dumps(value):
    return json.dumps(value, cls=DjangoJSONEncoder, ensure_ascii=False)


def jsonable(value):
    return json.loads(dumps(value))


class ToolResult:
    """Full control over a tool result: content blocks, structured content, error flag."""

    def __init__(self, content=None, structured=None, is_error=False):
        self.content = content or []
        self.structured = structured
        self.is_error = is_error

    def to_dict(self):
        result = {"content": self.content, "isError": self.is_error}
        if self.structured is not None:
            result["structuredContent"] = jsonable(self.structured)
        return result


def tool_result(value, is_error=False):
    if isinstance(value, ToolResult):
        return value.to_dict()
    if isinstance(value, str):
        return ToolResult([text(value)], is_error=is_error).to_dict()
    structured = value if isinstance(value, dict) else None
    return ToolResult([text(dumps(value))], structured, is_error).to_dict()


def text(value):
    return {"type": "text", "text": value}


def resource_contents(uri, value, mime_type=None):
    if isinstance(value, bytes):
        return {
            "uri": uri,
            "mimeType": mime_type or "application/octet-stream",
            "blob": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, str):
        return {"uri": uri, "mimeType": mime_type or "text/plain", "text": value}
    return {
        "uri": uri,
        "mimeType": mime_type or "application/json",
        "text": dumps(value),
    }


def prompt_messages(value):
    if isinstance(value, str):
        return [user(value)]
    return list(value)


def user(content):
    """A prompt message from the user. ``content`` is text or a content block."""
    return {
        "role": "user",
        "content": text(content) if isinstance(content, str) else content,
    }


def assistant(content):
    return {
        "role": "assistant",
        "content": text(content) if isinstance(content, str) else content,
    }
