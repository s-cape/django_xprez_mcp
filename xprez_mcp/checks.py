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
        if server.permission is None and server.permission_callback is None:
            errors.append(
                checks.Error(
                    f"MCP server {server.name!r} has no permission, so it lets "
                    "nobody in.",
                    hint='Set XPREZ_MCP["PERMISSION_CALLBACK"] (a dotted path to '
                    'f(ctx) -> bool) or XPREZ_MCP["PERMISSION"] (Django permission '
                    "names), or the MCPServer arguments of the same name.",
                    obj=server,
                    id="xprez_mcp.E001",
                )
            )
        names.setdefault(server.name, []).append(server)
        errors += check_names(server)
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


def check_names(server):
    from .errors import InvalidName
    from .server import LONG_NAME, NAME_PATTERN

    try:
        catalog = server.catalog
    except InvalidName as error:
        return [
            checks.Error(
                str(error),
                hint="Rename them (tool_names, name, namespace or the function).",
                obj=server,
                id="xprez_mcp.E003",
            )
        ]
    long = [
        identifier
        for kind in ("tool", "prompt")
        for identifier in catalog[kind]
        if NAME_PATTERN.match(identifier) and len(identifier) > LONG_NAME
    ]
    if not long:
        return []
    return [
        checks.Warning(
            f"MCP server {server.name!r} has names over {LONG_NAME} characters: "
            f"{', '.join(sorted(long))}.",
            hint="Clients that prefix the server name may reject long names.",
            obj=server,
            id="xprez_mcp.W001",
        )
    ]
