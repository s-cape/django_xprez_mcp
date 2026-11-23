"""The OAuth endpoints of one MCP server, added to ``server.urls`` while this
app is installed."""

from django.urls import path

from . import views


def server_urls(server):
    return [
        path(
            ".well-known/oauth-protected-resource",
            views.ResourceMetadataView.as_view(server=server),
            name="oauth-resource-metadata",
        ),
        path(
            "oauth/authorize/",
            views.AuthorizeView.as_view(server=server),
            name="oauth-authorize",
        ),
        path(
            "oauth/token/",
            views.TokenView.as_view(server=server),
            name="oauth-token",
        ),
        path(
            "oauth/register/",
            views.RegisterView.as_view(server=server),
            name="oauth-register",
        ),
        path(
            "oauth/revoke/",
            views.RevokeView.as_view(server=server),
            name="oauth-revoke",
        ),
    ]
