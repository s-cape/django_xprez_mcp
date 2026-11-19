"""An admin site with users and MCP tokens."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from xprez_mcp import MCPServer
from xprez_mcp.contrib.tokens.admin import TokenAdmin
from xprez_mcp.contrib.tokens.models import Token

site = admin.AdminSite(name="tokenadmin")

site.register(User, UserAdmin)
site.register(Token, TokenAdmin)

token_admin_mcp = MCPServer(name="token-admin", permission="staff", models="generic")
token_admin_mcp.register_admin(site)
