"""A separate admin site, so the tests control every registered model."""

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponse

from .models import Client, Contact, Industry, Tag

site = admin.AdminSite(name="testadmin")


class OwnedFilter(admin.SimpleListFilter):
    title = "Has an owner"
    parameter_name = "owned"

    def lookups(self, request, model_admin):
        return [("yes", "Yes"), ("no", "No")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(owner__isnull=False)
        if self.value() == "no":
            return queryset.filter(owner__isnull=True)
        return queryset


class OnePrimaryFormSet(BaseInlineFormSet):
    def clean(self):
        primary = [
            form
            for form in self.forms
            if form.cleaned_data.get("primary") and not form.cleaned_data.get("DELETE")
        ]
        if len(primary) > 1:
            raise ValidationError("Only one contact can be primary.")


class ContactInline(admin.TabularInline):
    model = Contact
    formset = OnePrimaryFormSet
    fields = ["name", "email", "primary"]
    extra = 0


@admin.action(
    description="Deactivate selected %(verbose_name_plural)s",
    permissions=["change"],
)
def deactivate(modeladmin, request, queryset):
    count = queryset.update(active=False)
    modeladmin.message_user(request, f"{count} deactivated.")


@admin.action(description="Export")
def export(modeladmin, request, queryset):
    queryset.update(group="exported")  # rolled back: the action returns a page
    return HttpResponse("csv")


class ClientAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "industry", "size", "upper_name"]
    search_fields = ["name", "=group"]
    list_filter = ["size", "industry", "founded", OwnedFilter]
    fields = ["slug", "name", "group", "industry", "tags", "size", "founded", "active"]
    readonly_fields = ["active"]
    ordering = ["slug"]
    inlines = [ContactInline]
    actions = [deactivate, export]

    @admin.display(description="Name in capitals")
    def upper_name(self, obj):
        return obj.name.upper()

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        return queryset.filter(active=True)

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and not request.user.is_superuser:
            return ["slug", "active"]
        return ["active"]

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.owner_id not in (None, request.user.pk):
            return False
        return super().has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.owner = request.user
        super().save_model(request, obj, form, change)
        self.message_user(request, f"Saved {obj}.")


site.register(Client, ClientAdmin)
site.register(Industry)
site.register(Tag, search_fields=["name"])
