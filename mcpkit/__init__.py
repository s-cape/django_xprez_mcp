"""Admin-style MCP servers for Django."""

from .context import CallContext
from .decorators import prompt, resource, tool
from .errors import (
    AlreadyRegistered,
    AuthenticationFailed,
    InvalidArguments,
    NotRegistered,
    RpcError,
    ToolError,
)
from .providers import Provider
from .results import ToolResult
from .server import MCPServer, mcp

__version__ = "0.1.0.dev0"

__all__ = [
    "AlreadyRegistered",
    "AuthenticationFailed",
    "CallContext",
    "InvalidArguments",
    "MCPServer",
    "NotRegistered",
    "Provider",
    "RpcError",
    "ToolError",
    "ToolResult",
    "mcp",
    "prompt",
    "resource",
    "tool",
]
