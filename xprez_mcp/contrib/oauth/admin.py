from django import forms
from django.contrib import admin

from .clients import validate_redirect_uri
from .errors import ClientError
from .models import Client, Grant


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ["name", "redirect_uris"]

    def clean_redirect_uris(self):
        """The rules of registration: https, or http on localhost."""
        uris = self.cleaned_data["redirect_uris"]
        if not isinstance(uris, list) or not uris:
            raise forms.ValidationError("A list of one or more URIs.")
        for uri in uris:
            try:
                validate_redirect_uri(uri)
            except ClientError as error:
                raise forms.ValidationError(f"{uri!r}: {error}")
        return uris


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    """Clients appear by themselves (metadata documents, registration);
    pre-registered ones come from ``Client.objects.create_client()``."""

    mcp_exposed = False
    form = ClientForm
    list_display = ["__str__", "kind", "client_id", "created", "last_used"]
    list_filter = ["kind"]
    search_fields = ["name", "client_id"]
    readonly_fields = [
        "client_id",
        "kind",
        "auth_method",
        "metadata",
        "created",
        "last_used",
        "refresh_after",
    ]

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.kind == Client.METADATA_DOCUMENT:
            # the document is the source of truth
            return self.readonly_fields + ["name", "redirect_uris"]
        return self.readonly_fields


@admin.register(Grant)
class GrantAdmin(admin.ModelAdmin):
    """Connections; deleting one disconnects the client. Others' connections
    are for superusers only."""

    mcp_exposed = False
    list_display = ["client", "user", "resource", "created", "last_used"]
    search_fields = ["client__name", "resource"]
    raw_id_fields = ["user", "client"]
    readonly_fields = ["user", "client", "resource", "created", "last_used"]

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related("client", "user")
        if not request.user.is_superuser:
            queryset = queryset.filter(user=request.user)
        return queryset

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
