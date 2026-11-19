"""Extra servers: a curated one and one without authentication."""

from xprez_mcp import MCPServer, auth

from .mcp import StaffProvider

staff_mcp = MCPServer(
    name="staff", instructions="Staff tools.", authentication=[auth.session]
)
staff_mcp.register(StaffProvider)

open_mcp = MCPServer(name="open", authentication=[], allow_anonymous=True)


@open_mcp.tool
def hello(ctx, name: str = "world"):
    return f"Hello {name}"
