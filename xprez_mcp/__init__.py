"""Admin-style MCP servers for Django."""

from importlib.metadata import PackageNotFoundError, version

from .context import CallContext
from .decorators import prompt, resource, tool
from .errors import (
    AlreadyRegistered,
    AuthenticationFailed,
    InvalidArguments,
    InvalidName,
    NotRegistered,
    RpcError,
    ToolError,
)
from .model_provider import ModelProvider
from .providers import Provider
from .results import ToolResult
from .server import MCPServer, mcp

try:
    __version__ = version("django-xprez-mcp")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"


def __getattr__(name):
    # lazy: django.contrib.admin is optional and needs the app registry
    if name == "AdminProvider":
        from .admin_provider import AdminProvider

        return AdminProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AdminProvider",
    "AlreadyRegistered",
    "AuthenticationFailed",
    "CallContext",
    "InvalidArguments",
    "InvalidName",
    "MCPServer",
    "ModelProvider",
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
