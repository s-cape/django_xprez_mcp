"""Fixed-window counters in Django's cache (per process with the default
local-memory cache; share one cache between workers for a global limit)."""

import hashlib

from django.core.cache import cache


def throttled(scope, ident, rate):
    limit, window = rate
    digest = hashlib.sha256(str(ident).encode()).hexdigest()[:32]
    key = f"xprez_mcp_oauth:{scope}:{digest}"
    cache.add(key, 0, window)
    try:
        count = cache.incr(key)
    except ValueError:  # expired in between
        cache.set(key, 1, window)
        count = 1
    return count > limit


def client_ip(request):
    return request.META.get("REMOTE_ADDR", "")
