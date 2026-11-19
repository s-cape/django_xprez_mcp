"""Admin-based servers on the test admin site."""

from xprez_mcp import AdminProvider, MCPServer

from .admin import site
from .models import Client

admin_mcp = MCPServer(name="admin")
admin_generic_mcp = MCPServer(name="admin-generic", models="generic")


class ClientAdminProvider(AdminProvider):
    admin_site = site
    actions = ["list", "get", "create", "update", "delete"]
    admin_actions = ["deactivate", "export"]


admin_mcp.register(Client, ClientAdminProvider)
# the rest of the site through the generic tools, next to clients_* tools
admin_mcp.register_admin(site, exposure="generic")

admin_generic_mcp.register_admin(
    site, model_actions={Client: ["list", "get", "update"]}
)
