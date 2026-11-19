from django.conf import settings

DEFAULTS = {
    "NAME": "django",
    "VERSION": "0.0.0",
    "AUTODISCOVER": True,
    "AUTODISCOVER_MODULE": "mcp",
    "AUTHENTICATION": ["mcpkit.auth.bearer_token"],
    "TOKEN_RESOLVER": None,
    "ALLOWED_ORIGINS": [],
    "EXCLUDE": [],
    "EXCLUDE_APPS": [],
}


def get(key):
    return getattr(settings, "MCPKIT", {}).get(key, DEFAULTS[key])
