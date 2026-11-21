"""Servers writing file fields, on their own admin site."""

from django.contrib import admin

from xprez_mcp import AdminProvider, MCPServer, ModelProvider

from .models import Document, Page

site = admin.AdminSite(name="fileadmin")


class PageInline(admin.TabularInline):
    model = Page
    fields = ["number", "scan"]
    extra = 0


class DocumentAdmin(admin.ModelAdmin):
    fields = ["title", "file", "attachment"]
    inlines = [PageInline]


site.register(Document, DocumentAdmin)

files_mcp = MCPServer(name="files")
files_generic_mcp = MCPServer(name="files-generic", exposure="generic")
files_admin_mcp = MCPServer(name="files-admin")


class DocumentProvider(ModelProvider):
    fields = ["id", "title", "file", "attachment"]
    actions = ["list", "get", "create", "update"]
    file_sources = ["data", "path"]


class DocumentAdminProvider(AdminProvider):
    admin_site = site
    actions = ["list", "get", "create", "update"]
    file_sources = ["data", "path", "url"]


files_mcp.register(Document, DocumentProvider)
files_generic_mcp.register(Document, DocumentProvider)
files_admin_mcp.register(Document, DocumentAdminProvider)
