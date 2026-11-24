"""Servers writing file fields, on their own admin site."""

from typing import Any

from django.contrib import admin
from django.db import transaction
from django.forms import modelform_factory

from xprez_mcp import AdminProvider, MCPServer, ModelProvider, Provider, tool
from xprez_mcp.errors import ToolError
from xprez_mcp.providers import FileArgumentsMixin
from xprez_mcp.providers.files import check_cleared, closing_stored_files, form_files

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
files_form_mcp = MCPServer(name="files-form")


class DocumentProvider(ModelProvider):
    fields = ["id", "title", "file", "attachment"]
    actions = ["list", "get", "create", "update"]
    file_sources = ["data", "path"]


class DocumentAdminProvider(AdminProvider):
    admin_site = site
    actions = ["list", "get", "create", "update"]
    file_sources = ["data", "path", "url"]


class DocumentFormProvider(FileArgumentsMixin, Provider):
    """A plain provider writing file fields through its own form."""

    file_sources = ["data", "path"]

    @tool
    def update_document(self, ctx, id: int, fields: dict[str, Any]):
        document = Document.objects.get(pk=id)
        data, files = dict(fields), {}
        for name in ("file", "attachment"):
            if name in data:
                field = Document._meta.get_field(name)
                value = self.file_value(data.pop(name), field, f"fields.{name}")
                files[name] = (
                    False if value is None else self.resolve_file(ctx, field, value)
                )
        form_class = modelform_factory(Document, fields=[*data, *files])
        form = form_class(data, form_files(files), instance=document)
        with closing_stored_files(files):
            valid = form.is_valid()
        if not valid:
            raise ToolError(f"Invalid data: {form.errors.as_text()}")
        check_cleared(form, files, "fields.")
        with self.removing_uploads_on_error(files.values()), transaction.atomic():
            form.save()
            self.after_save(document)
        return {"file": document.file.name, "attachment": document.attachment.name}

    def after_save(self, document):
        pass


files_mcp.register(Document, DocumentProvider)
files_generic_mcp.register(Document, DocumentProvider)
files_admin_mcp.register(Document, DocumentAdminProvider)
files_form_mcp.register(DocumentFormProvider)
