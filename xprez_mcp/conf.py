from django.conf import settings

DEFAULTS = {
    "NAME": "django",
    # "https://example.com" when the request does not know the public origin
    "BASE_URL": None,
    "VERSION": "0.0.0",
    "PERMISSION": None,
    "AUTODISCOVER": True,
    "AUTODISCOVER_MODULE": "mcp",
    "AUTHENTICATION": ["xprez_mcp.auth.bearer_token"],
    "TOKEN_RESOLVER": None,
    "OAUTH": {},
    "UPLOADS": {},
    "ALLOWED_ORIGINS": [],
    "EXCLUDE": [],
    "EXCLUDE_APPS": [],
    "ALLOW_WRITES": [],
    "LIST_PAGE_SIZE": None,
    "RELATED_LABELS": False,
    "ATOMIC_WRITES": False,
}


def get(key):
    return getattr(settings, "XPREZ_MCP", {}).get(key, DEFAULTS[key])
