"""Model providers on their own servers: tools per model and generic."""

from xprez_mcp import MCPServer, ModelProvider, tool
from xprez_mcp.permissions import allow_staff

from .models import Client, Industry, Tag

model_mcp = MCPServer(name="models")
generic_mcp = MCPServer(name="generic", exposure="generic")


def filter_industry(ctx, queryset, value: str):
    return queryset.filter(industry__name__iexact=value)


class ClientProvider(ModelProvider):
    fields = ["slug", "name", "industry", "size", "active"]
    detail_fields = ["slug", "name", "group", "industry", "tags", "size", "label"]
    write_fields = ["slug", "name", "group", "industry", "tags", "size", "founded"]
    lookup_field = "slug"
    search_fields = ["name", "=group"]
    filters = {
        "industry": filter_industry,
        "size": "size",
        "active": "active",
        "founded_after": "founded__gte",
        "tag": "tags__name",
    }
    ordering_fields = ["name", "founded"]
    actions = ["list", "get", "create", "update", "delete"]
    permissions = {
        "create": "{app_label}.add_{model_name}",
        "delete": allow_staff,
    }
    resources = True
    resource_scheme = "crm"

    def get_queryset(self, ctx):
        return Client.objects.filter(active=True) | Client.objects.filter(
            owner=ctx.user
        )

    def has_object_permission(self, ctx, action, instance):
        if action == "update":
            return instance.owner_id in (None, ctx.user.pk)
        return True

    @tool(read_only=True)
    def stats(self, ctx):
        """Count clients."""
        return {"count": self.get_queryset(ctx).count()}


class IndustryProvider(ModelProvider):
    model = Industry
    fields = ["id", "name"]


class TagProvider(ModelProvider):
    fields = ["id", "name"]
    actions = ["list"]

    def has_permission(self, ctx):
        return ctx.user.is_staff


for server in (model_mcp, generic_mcp):
    server.register(Client, ClientProvider)
    server.register(IndustryProvider)
    server.register(Tag, TagProvider)
