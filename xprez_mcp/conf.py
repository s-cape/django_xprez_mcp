from django.conf import settings

DEFAULTS = {
    "NAME": "django",
    "VERSION": "0.0.0",
    "AUTODISCOVER": True,
    "AUTODISCOVER_MODULE": "mcp",
    "AUTHENTICATION": ["xprez_mcp.auth.bearer_token"],
    "TOKEN_RESOLVER": None,
    "ALLOWED_ORIGINS": [],
    "EXCLUDE": [],
    "EXCLUDE_APPS": [],
}


def get(key):
    return getattr(settings, "XPREZ_MCP", {}).get(key, DEFAULTS[key])
