from django.contrib import admin, messages
from django.utils.html import format_html

from .models import Token


@admin.register(Token)
class TokenAdmin(admin.ModelAdmin):
    """Tokens under the model permissions; others' tokens need
    ``manage_others_token``.

    The key is shown once, in the admin message after "Save" (Django's message
    storage holds it for that one redirect).
    """

    mcp_exposed = False
    list_display = ["name", "user", "masked_key", "created", "last_used"]
    search_fields = ["name", "key_id"]
    raw_id_fields = ["user"]

    def has_manage_others_permission(self, request):
        """Whether the admin user sees and manages other users' tokens."""
        return request.user.has_perm("xprez_mcp_tokens.manage_others_token")

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if not self.has_manage_others_permission(request):
            queryset = queryset.filter(user=request.user)
        return queryset

    def get_readonly_fields(self, request, obj=None):
        return ["user", "masked_key", "created", "last_used"] if obj else []

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "user":
            kwargs["initial"] = request.user.pk
            if not self.has_manage_others_permission(request):
                users = db_field.related_model._default_manager
                kwargs["queryset"] = users.filter(pk=request.user.pk)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        key = None if change else obj.set_key()
        super().save_model(request, obj, form, change)
        if key:
            message = format_html(
                "The key of {}, shown only once: <code>{}</code><br>"
                "Clients send it as <code>Authorization: Bearer {}</code>",
                obj,
                key,
                key,
            )
            self.message_user(request, message, messages.WARNING)
