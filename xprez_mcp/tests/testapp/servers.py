"""Extra servers: a curated one and one without authentication."""

from xprez_mcp import MCPServer, auth
from xprez_mcp.contrib.oauth.auth import access_token
from xprez_mcp.permissions import allow_authenticated

from .mcp import StaffProvider

staff_mcp = MCPServer(
    name="staff", instructions="Staff tools.", authentication=[auth.session]
)
staff_mcp.register(StaffProvider)

open_mcp = MCPServer(
    name="open", authentication=[], allow_anonymous=True, permission=True
)


@open_mcp.tool
def hello(ctx, name: str = "world"):
    return f"Hello {name}"


oauth_mcp = MCPServer(
    name="oauth",
    title="OAuth tools",
    authentication=[access_token, auth.bearer_token],
    permission=allow_authenticated,
)


@oauth_mcp.tool
def whoami(ctx):
    return ctx.user.username
