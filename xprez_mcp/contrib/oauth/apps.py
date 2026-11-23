from django.apps import AppConfig


class OAuthConfig(AppConfig):
    name = "xprez_mcp.contrib.oauth"
    label = "xprez_mcp_oauth"
    verbose_name = "MCP OAuth"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401  (registers the system checks)
