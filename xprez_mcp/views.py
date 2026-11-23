"""Stateless Streamable HTTP endpoint: one JSON-RPC message per POST, JSON replies."""

import json
import logging
from urllib.parse import urlsplit

from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .errors import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    AuthenticationFailed,
    RpcError,
)
from .core.results import dumps

logger = logging.getLogger("xprez_mcp")


@method_decorator(csrf_exempt, name="dispatch")
class MCPView(View):
    server = None
    http_method_names = ["post", "options"]

    def post(self, request):
        server = self.server
        if not origin_allowed(request, server.allowed_origins):
            return HttpResponse("Origin not allowed.", status=403)
        if request.content_type != "application/json":
            return HttpResponse("Content-Type must be application/json.", status=415)
        header_version = request.headers.get("MCP-Protocol-Version")
        # only a supported version reaches the context; checked further down
        version = header_version if header_version in server.protocol_versions else None

        # before the version check: an unauthenticated client of a newer spec
        # must get the 401 that points it to sign-in, not a 400
        try:
            user = server.authenticate(request)
        except AuthenticationFailed as error:
            return unauthorized(request, server, error)
        if user is None and not server.allow_anonymous:
            return unauthorized(request, server)
        if not server.has_access(server.get_context(request, user, version)):
            return JsonResponse({"error": "Forbidden"}, status=403)

        try:
            message = json.loads(request.body)
        except RequestDataTooBig:
            return rpc_error(None, INVALID_REQUEST, "Request too large.")
        except (ValueError, RecursionError):  # RecursionError: deeply nested
            return rpc_error(None, PARSE_ERROR, "Parse error.")
        if isinstance(message, list):
            return rpc_error(None, INVALID_REQUEST, "Batches are not supported.")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return rpc_error(None, INVALID_REQUEST, "Invalid request.")
        # initialize negotiates the version in its body, so its header is not
        # checked; later requests carry the negotiated one
        if header_version and version is None and message.get("method") != "initialize":
            return HttpResponse("Unsupported MCP-Protocol-Version.", status=400)
        if "method" not in message or "id" not in message:
            # notifications and responses to server requests
            return HttpResponse(status=202)

        message_id, method = message["id"], message["method"]
        params = message.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            return rpc_error(message_id, INVALID_REQUEST, "Invalid request.")

        ctx = server.get_context(
            request, user, protocol_version=version, meta=params.get("_meta") or {}
        )
        try:
            result = server.dispatch(ctx, method, params)
        except RpcError as error:
            return rpc_response(message_id, error=error.to_dict())
        except Exception:
            logger.exception("MCP method %s failed", method)
            return rpc_error(message_id, INTERNAL_ERROR, "Internal error.")
        return rpc_response(message_id, result=result)


def rpc_response(message_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return HttpResponse(dumps(payload), content_type="application/json")


def rpc_error(message_id, code, message):
    return rpc_response(message_id, error={"code": code, "message": message})


def origin_allowed(request, allowed):
    """DNS rebinding protection: browsers send Origin, CLI clients do not."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    if origin in allowed or "*" in allowed:
        return True
    return urlsplit(origin).netloc == request.get_host()


def unauthorized(request=None, server=None, error=None):
    """``401`` with the challenge of the first backend that has one
    (``backend.challenge(request, error) -> str | None``), else ``Bearer``."""
    challenge = None
    if server is not None:
        for backend in server.get_authentication_backends():
            get_challenge = getattr(backend, "challenge", None)
            challenge = get_challenge(request, error) if get_challenge else None
            if challenge:
                break
    response = JsonResponse({"error": "Unauthorized"}, status=401)
    response["WWW-Authenticate"] = challenge or "Bearer"
    return response
