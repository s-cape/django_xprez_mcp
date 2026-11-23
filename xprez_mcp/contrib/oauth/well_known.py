"""Metadata at the site root, where clients look for it:
``path("", include("xprez_mcp.contrib.oauth.well_known"))``.

``/.well-known/oauth-authorization-server/mcp`` describes the authorization
server of the endpoint at ``/mcp/`` (RFC 8414 path insertion).
"""

from django.http import Http404
from django.urls import Resolver404, ResolverMatch, URLPattern, path, resolve
from django.urls.resolvers import RegexPattern

from . import views


def not_found(request):
    raise Http404


class SlashlessEndpoints(URLPattern):
    """Serves ``/mcp`` like ``/mcp/``: users paste the URL either way, and a
    POST cannot be redirected to add the slash. Only MCP endpoints match."""

    def __init__(self):
        super().__init__(RegexPattern(r"^.*[^/]\Z"), not_found)

    def resolve(self, route):
        from xprez_mcp.views import MCPView

        if not self.pattern.match(route):
            return None
        try:
            match = resolve(f"/{route}/")
        except Resolver404:
            return None
        view_class = getattr(match.func, "view_class", None)
        if view_class is None or not issubclass(view_class, MCPView):
            return None
        return ResolverMatch(
            match.func,
            (),
            {},
            match.url_name,
            app_names=match.app_names,
            namespaces=match.namespaces,
            route=route,
            captured_kwargs={},
            extra_kwargs={},
        )


urlpatterns = [
    path(
        ".well-known/oauth-protected-resource",
        views.ResourceMetadataView.as_view(),
    ),
    path(
        ".well-known/oauth-protected-resource/<path:rest>",
        views.ResourceMetadataView.as_view(),
    ),
    path(
        ".well-known/oauth-authorization-server",
        views.ServerMetadataView.as_view(),
        name="xprez_mcp_oauth_server_metadata",
    ),
    path(
        ".well-known/oauth-authorization-server/<path:rest>",
        views.ServerMetadataView.as_view(),
    ),
    SlashlessEndpoints(),
]
