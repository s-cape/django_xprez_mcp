"""The request admin code receives during an MCP call.

``ModelAdmin`` and ``AdminSite`` methods take a request. These helpers give them
a copy of the MCP request with the caller as ``user``, for providers that call
admin code without being an ``AdminProvider``.
"""

import copy

from django.contrib.messages.storage.base import BaseStorage
from django.http import HttpRequest, QueryDict
from django.utils.datastructures import MultiValueDict


class CollectedMessages(BaseStorage):
    """Message storage keeping what admin code adds, for the tool result."""

    def _get(self, *args, **kwargs):
        return [], True

    def _store(self, messages, response, *args, **kwargs):
        return []

    def collected(self):
        return [
            {"level": message.level_tag, "message": str(message.message)}
            for message in self._queued_messages
        ]


def build_admin_request(ctx, site):
    """A new copy of the MCP request for admin code on ``site``.

    ``GET``, ``POST`` and ``FILES`` are empty, ``current_app`` is the site's
    name and messages are collected instead of stored.
    """
    from django.contrib.auth.models import AnonymousUser

    if ctx.request is not None:
        request = copy.copy(ctx.request)
    else:
        request = HttpRequest()
    request.GET = QueryDict()
    set_request_data(request)
    request.user = ctx.user if ctx.user is not None else AnonymousUser()
    request.current_app = site.name
    request._messages = CollectedMessages(request)
    return request


def set_request_data(request, data=None, files=None):
    """Set ``POST`` and ``FILES``; ``None`` empties them.

    A plain mapping becomes a ``MultiValueDict``, a list value its list.
    """
    request.POST = multi_value(data) if data is not None else QueryDict()
    files = multi_value(files) if files is not None else MultiValueDict()
    try:
        request.FILES = files
    except AttributeError:  # WSGIRequest.FILES has no setter
        request._files = files


def multi_value(data):
    if isinstance(data, MultiValueDict):
        return data
    result = MultiValueDict()
    for key, value in data.items():
        result.setlist(
            key, list(value) if isinstance(value, (list, tuple)) else [value]
        )
    return result


def get_admin_request(ctx, site, write=False, data=None, files=None, factory=None):
    """The admin request for ``site`` in this call, created on first use.

    One request per call and site, so messages from several admin methods end
    up in one result. ``method`` is ``POST`` for writes, ``GET`` otherwise.
    With ``data`` or ``files`` the result is a copy of that request with them
    as ``POST`` and ``FILES``; the shared request and its other users keep
    their own. ``factory(ctx)`` builds the request (default:
    ``build_admin_request``); a request built by default is rebuilt by the
    first ``factory`` given, keeping the messages collected so far.
    """
    requests = ctx.__dict__.setdefault("_admin_requests", {})
    request = requests.get(site.name)
    if request is None or (
        factory is not None and getattr(request, "_mcp_default", False)
    ):
        if factory is None:
            built = build_admin_request(ctx, site)
            built._mcp_default = True
        else:
            built = factory(ctx)
        if request is not None:
            built._messages = request._messages
        request = requests[site.name] = built
    if data is not None or files is not None:
        request = copy.copy(request)
        set_request_data(request, data, files)
    request.method = "POST" if write else "GET"
    return request


def collected_messages(ctx, site):
    """Messages admin code on ``site`` added during this call."""
    request = ctx.__dict__.get("_admin_requests", {}).get(site.name)
    if request is None:
        return []
    return request._messages.collected()


def with_messages(ctx, site, result):
    """``result`` with ``"messages"`` added when admin code added any."""
    collected = collected_messages(ctx, site)
    if collected and isinstance(result, dict):
        result = {**result, "messages": collected}
    return result
