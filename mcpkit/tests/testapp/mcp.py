"""Registered on the default server through autodiscovery."""

import datetime
import enum
from typing import Annotated, Literal

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404

from mcpkit import Provider, ToolError, mcp, prompt, resource, tool
from mcpkit.results import assistant, user

NOTES = {1: "First note", 2: "Second note"}


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Conflict(Exception):
    def __init__(self, current):
        self.current = current


@mcp.register
class NotesProvider(Provider):
    namespace = "notes"

    @tool(read_only=True)
    def search(self, ctx, query: Annotated[str, "Text to find"], limit: int = 10):
        """Search notes."""
        found = [
            {"id": pk, "text": text}
            for pk, text in NOTES.items()
            if query.lower() in text.lower()
        ]
        return {"notes": found[:limit]}

    @tool
    def echo(
        self,
        ctx,
        day: datetime.date,
        color: Color = Color.RED,
        tags: list[str] | None = None,
    ):
        return {"day": day, "color": color.value, "tags": tags}

    @tool
    def fail(self, ctx, kind: Literal["tool", "denied", "missing", "invalid", "crash"]):
        if kind == "tool":
            raise ToolError("Nope.", data={"hint": "try again"})
        if kind == "denied":
            raise PermissionDenied
        if kind == "missing":
            raise Http404("No such note.")
        if kind == "invalid":
            raise ValidationError("Bad value.")
        raise RuntimeError("boom")

    @tool
    def update(self, ctx, id: int):
        raise Conflict(current={"id": id, "version": 2})

    @tool(input_schema={"type": "object", "properties": {"raw": {"type": "string"}}})
    def raw(self, ctx, **arguments):
        return {"received": arguments}

    @resource("notes://all", mime_type="application/json")
    def all_notes(self, ctx):
        """All notes."""
        return NOTES

    @resource("notes://{id}", mime_type="text/plain")
    def note(self, ctx, id: int):
        if id not in NOTES:
            raise Http404
        return NOTES[id]

    @note.complete("id")
    def _note_ids(self, ctx, value):
        return [pk for pk in NOTES if str(pk).startswith(value)]

    @prompt
    def summarize(
        self,
        ctx,
        id: Annotated[int, "Note id"],
        style: Literal["short", "long"] = "short",
    ):
        """Summarize a note."""
        return f"Write a {style} summary of notes://{id}."

    @summarize.complete("style")
    def _styles(self, ctx, value, arguments):
        return [style for style in ("short", "long") if style.startswith(value)]


@mcp.error_handler(Conflict)
def conflict(ctx, error):
    return {"error": "Version conflict.", "current": error.current}


class StaffProvider(Provider):
    namespace = "staff"

    def has_permission(self, ctx):
        return ctx.user.is_staff

    @tool
    def report(self, ctx):
        return "report"

    @tool(permission="auth.view_user")
    def users(self, ctx):
        return {"count": 0}


mcp.register(StaffProvider)


@mcp.tool(read_only=True, title="Ping")
def ping(ctx) -> str:
    """Check the server."""
    return "pong"


@mcp.tool
def whoami(ctx):
    return {"username": ctx.user.get_username()}


@mcp.tool
def hidden_tool(ctx):
    """Excluded by MCPKIT["EXCLUDE"]."""
    return "hidden"


@mcp.prompt(name="greeting")
def greeting_prompt(ctx):
    return [user("Hello"), assistant("Hi, how can I help?")]
