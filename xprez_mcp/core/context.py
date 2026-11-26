import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("xprez_mcp")


@dataclass
class CallContext:
    """Passed as the first argument to every tool, resource, prompt and completion.

    Projects can subclass it and set ``MCPServer.context_class``.
    """

    server: Any
    request: Any = None
    user: Any = None
    protocol_version: Optional[str] = None
    meta: dict = field(default_factory=dict)
    # set during tools/call: the database alias the call runs in a
    # transaction on, or None
    tool_database: Optional[str] = None
    _call_end: list = field(default_factory=list, init=False, repr=False)

    def on_call_end(self, func):
        """Run ``func(ok)`` when the tool call ends: ``ok`` true on success,
        still inside the call's transaction (raising turns the call into an
        error and rolls it back), false after an error."""
        self._call_end.append(func)

    def end_call(self, ok):
        funcs, self._call_end = self._call_end, []
        error = None
        for func in funcs:
            try:
                func(ok)
            except Exception as exception:
                if not ok:
                    logger.exception("A call-end hook failed")
                elif error is None:
                    error = exception
        if error is not None:
            raise error
