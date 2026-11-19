"""A model provider using a DRF serializer; only loaded when DRF is installed."""

from rest_framework import serializers

from xprez_mcp import MCPServer, ModelProvider

from .models import Client


class ClientSerializer(serializers.ModelSerializer):
    industry_name = serializers.CharField(source="industry.name", read_only=True)

    class Meta:
        model = Client
        fields = ["slug", "name", "group", "industry", "industry_name"]


class ClientProvider(ModelProvider):
    serializer = ClientSerializer
    lookup_field = "slug"
    actions = ["list", "get", "update"]


drf_mcp = MCPServer(name="drf")
drf_mcp.register(Client, ClientProvider)
