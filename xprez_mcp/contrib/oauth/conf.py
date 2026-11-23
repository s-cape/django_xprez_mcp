from xprez_mcp import conf

DEFAULTS = {
    # "https://example.com" when the request does not know the public origin
    "BASE_URL": None,
    "ACCESS_TOKEN_LIFETIME": 3600,
    "REFRESH_TOKEN_LIFETIME": 30 * 24 * 3600,
    # a rotated refresh token sent again this soon is a retry, not a theft
    "REFRESH_REUSE_INTERVAL": 10,
    "CODE_LIFETIME": 120,
    "CLIENT_ID_METADATA_DOCUMENTS": True,
    "METADATA_TIMEOUT": 5,
    "METADATA_MAX_BYTES": 5120,
    # bounds for the document's Cache-Control max-age, and the default
    "METADATA_CACHE": (300, 3600, 86400),
    # document fetches per client IP, and per document host, and window
    "METADATA_FETCH_RATE": (10, 60),
    "DYNAMIC_REGISTRATION": True,
    # registrations per client IP and window (seconds)
    "REGISTRATION_RATE": (20, 3600),
    # registered clients never used for this long are deleted by cleanup
    "UNUSED_CLIENT_DAYS": 7,
}


def get(key):
    return {**DEFAULTS, **(conf.get("OAUTH") or {})}[key]
