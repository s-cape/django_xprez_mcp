from django.apps import AppConfig


class UploadsConfig(AppConfig):
    name = "xprez_mcp.contrib.uploads"
    label = "xprez_mcp_uploads"
    verbose_name = "MCP uploads"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401  (registers the system checks)

    def mcp_cleanup(self, now):
        """Run by ``mcp_cleanup``."""
        from .cleanup import cleanup

        return cleanup(now)
