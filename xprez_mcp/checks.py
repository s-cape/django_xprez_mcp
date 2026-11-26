from django.core import checks
from django.core.exceptions import ImproperlyConfigured


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
        try:
            permission = server.permission
        except ImproperlyConfigured as error:
            errors.append(
                checks.Error(
                    f"MCP server {server.name!r}: {error}",
                    obj=server,
                    id="xprez_mcp.E004",
                )
            )
        else:
            if permission is None:
                errors.append(
                    checks.Error(
                        f"MCP server {server.name!r} has no permission, so it "
                        "lets nobody in.",
                        hint='Set XPREZ_MCP["PERMISSION"] or '
                        "MCPServer(permission=...) to True, False or a function "
                        "f(ctx) -> bool, such as xprez_mcp.permissions.allow_staff "
                        '(in the setting also "xprez_mcp.permissions.allow_staff").',
                        obj=server,
                        id="xprez_mcp.E001",
                    )
                )
        names.setdefault(server.name, []).append(server)
        errors += check_names(server)
        errors += check_atomic(server)
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


def check_atomic(server):
    """``atomic_writes`` and ``@tool(atomic=...)`` name configured databases."""
    try:
        tools = list(server.catalog["tool"].values())
    except ImproperlyConfigured:
        return []  # reported by check_names or when the catalog is used
    bad = set()
    for item in tools:
        try:
            server.get_tool_database(item)
        except ImproperlyConfigured:
            bad.add(item.name)
    if not bad:
        return []
    return [
        checks.Error(
            f"MCP server {server.name!r}: atomic writes of {', '.join(sorted(bad))} "
            "name no configured database.",
            hint='Set XPREZ_MCP["ATOMIC_WRITES"], MCPServer(atomic_writes=...) or '
            "@tool(atomic=...) to True, False or a key of DATABASES.",
            obj=server,
            id="xprez_mcp.E005",
        )
    ]


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
