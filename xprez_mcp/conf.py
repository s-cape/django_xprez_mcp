from django.conf import settings

DEFAULTS = {
    "NAME": "django",
    "VERSION": "0.0.0",
    "PERMISSION": None,
    "AUTODISCOVER": True,
    "AUTODISCOVER_MODULE": "mcp",
    "AUTHENTICATION": ["xprez_mcp.auth.bearer_token"],
    "TOKEN_RESOLVER": None,
    "ALLOWED_ORIGINS": [],
    "EXCLUDE": [],
    "EXCLUDE_APPS": [],
    "ALLOW_WRITES": [],
    "LIST_PAGE_SIZE": None,
}


def get(key):
    return getattr(settings, "XPREZ_MCP", {}).get(key, DEFAULTS[key])
