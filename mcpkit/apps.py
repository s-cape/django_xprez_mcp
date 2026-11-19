from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules

from . import conf


class MCPKitConfig(AppConfig):
    name = "mcpkit"
    verbose_name = "MCP kit"

    def ready(self):
        if conf.get("AUTODISCOVER"):
            autodiscover_modules(conf.get("AUTODISCOVER_MODULE"))
