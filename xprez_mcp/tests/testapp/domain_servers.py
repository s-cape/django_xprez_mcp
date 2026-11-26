"""Hand-written domain tools: lookups, atomic writes and constraints."""

from typing import Annotated, Optional

from django.core.exceptions import PermissionDenied

from xprez_mcp import (
    Length,
    Lookup,
    MCPServer,
    Provider,
    Range,
    ToolError,
    ToolResult,
    auth,
    tool,
)
from xprez_mcp.contrib.oauth.auth import access_token
from xprez_mcp.contrib.tokens.auth import resolve as stored_token
from xprez_mcp.permissions import allow_authenticated

from .models import Industry, Tag


def get_industry(ctx, value, *, write=False):
    industry = Industry.objects.get(name=value)
    if write and industry.name.startswith("locked"):
        raise PermissionDenied(f"{industry.name} is read-only.")
    return industry


class IndustryProvider(Provider):
    namespace = "industry"

    def get_tag(self, ctx, value):
        tag = Tag.objects.filter(pk=value).first()
        if tag is None:
            raise ToolError(f"No tag {value}.")
        return tag

    @tool(read_only=True)
    def show(self, ctx, industry: Annotated[Industry, Lookup(get_industry), "Name"]):
        return {"name": industry.name}

    @tool(idempotent=True)
    def rename(
        self,
        ctx,
        industry: Annotated[Industry, Lookup(get_industry, write=True)],
        name: Annotated[str, Length(1, 50)],
    ):
        industry.name = name
        industry.save()
        return {"name": industry.name}

    @tool(read_only=True)
    def tags(
        self,
        ctx,
        tags: list[Annotated[Tag, Lookup("get_tag", type=int)]],
        industry: Optional[Annotated[Industry, Lookup(get_industry)]] = None,
    ):
        return {
            "tags": [tag.name for tag in tags],
            "industry": industry.name if industry else None,
        }

    @tool
    def create_and_fail(self, ctx, name: str):
        Industry.objects.create(name=name)
        Industry.objects.create(name=f"{name} 2")
        raise ToolError("Failed after writing.")

    @tool
    def create_and_return_error(self, ctx, name: str):
        Industry.objects.create(name=name)
        return ToolResult([{"type": "text", "text": "Not done."}], is_error=True)

    @tool(read_only=True)
    def read_only_create(self, ctx, name: str):
        Industry.objects.create(name=name)
        raise ToolError("Failed after writing.")

    @tool(atomic=False)
    def unwrapped_create(self, ctx, name: str):
        Industry.objects.create(name=name)
        raise ToolError("Failed after writing.")

    @tool(read_only=True)
    def plan(
        self,
        ctx,
        weeks: Annotated[int, Range(1, 52)],
        names: Annotated[list[str], Length(1, 3)] = ("a",),
    ):
        return {"weeks": weeks, "names": list(names)}


domain_mcp = MCPServer(
    name="domain",
    authentication=[access_token, auth.bearer_token],
    permission=allow_authenticated,
    atomic_writes=True,
)
domain_mcp.register(IndustryProvider)

# atomic_writes from the setting; stored tokens only
plain_mcp = MCPServer(
    name="plain",
    authentication=[auth.BearerToken(resolver=stored_token)],
    permission=allow_authenticated,
)


@plain_mcp.tool
def plain_create_and_fail(ctx, name: str):
    Industry.objects.create(name=name)
    raise ToolError("Failed after writing.")


@plain_mcp.tool(atomic=True)
def plain_atomic_create_and_fail(ctx, name: str):
    Industry.objects.create(name=name)
    raise ToolError("Failed after writing.")
