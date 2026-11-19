from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules

from . import conf


class XprezMCPConfig(AppConfig):
    name = "xprez_mcp"
    verbose_name = "Xprez MCP"

    def ready(self):
        if conf.get("AUTODISCOVER"):
            autodiscover_modules(conf.get("AUTODISCOVER_MODULE"))
