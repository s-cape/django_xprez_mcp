PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002


class AlreadyRegistered(Exception):
    pass


class NotRegistered(Exception):
    pass


class AuthenticationFailed(Exception):
    """Raised by an authentication backend to reject the request outright."""


class ToolError(Exception):
    """An error the model should see: returned as a tool result with isError."""

    def __init__(self, message, data=None):
        super().__init__(message)
        self.message = message
        self.data = data


class InvalidArguments(ToolError):
    pass


class RpcError(Exception):
    """A JSON-RPC protocol error."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self):
        error = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error
