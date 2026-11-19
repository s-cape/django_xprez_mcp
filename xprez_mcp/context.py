from dataclasses import dataclass, field
from typing import Any


@dataclass
class CallContext:
    """Passed as the first argument to every tool, resource, prompt and completion.

    Projects can subclass it and set ``MCPServer.context_class``.
    """

    server: Any
    request: Any = None
    user: Any = None
    protocol_version: str | None = None
    meta: dict = field(default_factory=dict)
