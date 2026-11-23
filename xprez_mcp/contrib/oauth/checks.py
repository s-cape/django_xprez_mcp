from django.core import checks


@checks.register()
def check_oauth(app_configs=None, **kwargs):
    from django.urls import NoReverseMatch, get_resolver, reverse

    from xprez_mcp.auth import BearerToken
    from xprez_mcp.server import _servers

    from .auth import AccessTokenBackend

    try:
        get_resolver().url_patterns  # imports the URLconf, which mounts servers
    except Exception:
        return []
    errors = []
    try:
        reverse("xprez_mcp_oauth_server_metadata")
    except NoReverseMatch:
        errors.append(
            checks.Warning(
                "The OAuth metadata URLs are not in the URLconf, so clients "
                "cannot find the authorization server.",
                hint='Add path("", include("xprez_mcp.contrib.oauth.well_known")) '
                "to the root URLconf.",
                id="xprez_mcp_oauth.W001",
            )
        )
    for server in sorted(_servers, key=lambda server: server.name or ""):
        if not server.mounted:
            continue
        try:
            backends = server.get_authentication_backends()
        except Exception:
            continue
        kinds = [
            "oauth" if isinstance(backend, AccessTokenBackend) else "bearer"
            for backend in backends
            if isinstance(backend, (AccessTokenBackend, BearerToken))
        ]
        if "oauth" in kinds and "bearer" in kinds[: kinds.index("oauth")]:
            errors.append(
                checks.Warning(
                    f"MCP server {server.name!r} tries bearer_token before the "
                    "OAuth backend, which rejects OAuth access tokens.",
                    hint="List xprez_mcp.contrib.oauth.auth.access_token first.",
                    obj=server,
                    id="xprez_mcp_oauth.W002",
                )
            )
    return errors
