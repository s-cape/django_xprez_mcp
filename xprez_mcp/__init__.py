"""Admin-style MCP servers for Django."""

from importlib.metadata import PackageNotFoundError, version

from .core.arguments import Length, Lookup, Range
from .core.context import CallContext
from .core.decorators import prompt, resource, tool
from .core.results import ToolResult
from .errors import (
    AlreadyRegistered,
    AuthenticationFailed,
    InvalidArguments,
    InvalidName,
    NotRegistered,
    RpcError,
    ToolError,
)
from .providers.base import Provider
from .providers.model import ModelProvider
from .server import MCPServer, mcp

try:
    __version__ = version("django-xprez-mcp")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"


def __getattr__(name):
    # lazy: django.contrib.admin is optional and needs the app registry
    if name == "AdminProvider":
        from .providers.admin.provider import AdminProvider

        return AdminProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AdminProvider",
    "AlreadyRegistered",
    "AuthenticationFailed",
    "CallContext",
    "InvalidArguments",
    "InvalidName",
    "Length",
    "Lookup",
    "MCPServer",
    "ModelProvider",
    "NotRegistered",
    "Provider",
    "Range",
    "RpcError",
    "ToolError",
    "ToolResult",
    "mcp",
    "prompt",
    "resource",
    "tool",
]
