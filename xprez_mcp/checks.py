from django.core import checks


@checks.register()
def check_servers(app_configs=None, **kwargs):
    """Checks the servers mounted in the URLconf (unmounted ones serve nothing)."""
    from django.urls import get_resolver

    from .server import _servers

    try:
        get_resolver().url_patterns  # imports the URLconf, which mounts servers
    except Exception:
        return []  # Django's own URL checks report this
    errors = []
    names = {}
    for server in sorted(_servers, key=lambda server: server.name or ""):
        if not server.mounted:
            continue
        if server.permission is None:
            errors.append(
                checks.Error(
                    f"MCP server {server.name!r} has no permission, so it lets "
                    "nobody in.",
                    hint='Set XPREZ_MCP["PERMISSION"] or MCPServer(permission=...), '
                    'for example "app_label.use_mcp" or "authenticated".',
                    obj=server,
                    id="xprez_mcp.E001",
                )
            )
        names.setdefault(server.name, []).append(server)
    for name, servers in names.items():
        if len(servers) > 1:
            errors.append(
                checks.Error(
                    f"{len(servers)} MCP servers are named {name!r}; the name is "
                    "also the URL namespace.",
                    hint="Pass a unique MCPServer(name=...).",
                    obj=servers[1],
                    id="xprez_mcp.E002",
                )
            )
    return errors
