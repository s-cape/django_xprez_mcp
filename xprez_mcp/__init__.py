"""Admin-style MCP servers for Django."""

from importlib.metadata import PackageNotFoundError, version

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

try:
    __version__ = version("django-xprez-mcp")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"

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
