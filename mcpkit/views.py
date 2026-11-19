"""Stateless Streamable HTTP endpoint: one JSON-RPC message per POST, JSON replies."""

import json
import logging
from urllib.parse import urlsplit

from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from . import conf
from .errors import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    RpcError,
)
from .results import dumps

logger = logging.getLogger("mcpkit")


def rpc_response(message_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return HttpResponse(dumps(payload), content_type="application/json")


def rpc_error(message_id, code, message):
    return rpc_response(message_id, error={"code": code, "message": message})


def origin_allowed(request):
    """DNS rebinding protection: browsers send Origin, CLI clients do not."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    allowed = conf.get("ALLOWED_ORIGINS")
    if origin in allowed or "*" in allowed:
        return True
    return urlsplit(origin).netloc == request.get_host()


@method_decorator(csrf_exempt, name="dispatch")
class MCPView(View):
    server = None
    http_method_names = ["post", "options"]

    def post(self, request):
        server = self.server
        if not origin_allowed(request):
            return HttpResponse("Origin not allowed.", status=403)
        if request.content_type != "application/json":
            return HttpResponse("Content-Type must be application/json.", status=415)
        version = request.headers.get("MCP-Protocol-Version")
        if version and version not in server.protocol_versions:
            return HttpResponse("Unsupported MCP-Protocol-Version.", status=400)

        user = server.authenticate(request)
        if user is None and not server.allow_anonymous:
            response = JsonResponse({"error": "Unauthorized"}, status=401)
            response["WWW-Authenticate"] = "Bearer"
            return response

        try:
            message = json.loads(request.body)
        except RequestDataTooBig:
            return rpc_error(None, INVALID_REQUEST, "Request too large.")
        except ValueError:
            return rpc_error(None, PARSE_ERROR, "Parse error.")
        if isinstance(message, list):
            return rpc_error(None, INVALID_REQUEST, "Batches are not supported.")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return rpc_error(None, INVALID_REQUEST, "Invalid request.")
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
