"""What ``mcp_cleanup`` deletes for the OAuth app."""

from datetime import timedelta

from django.db.models import Exists, OuterRef

from . import conf
from .models import AccessToken, AuthorizationCode, Client, Grant, RefreshToken


def delete(queryset):
    """How many rows of the queryset's model were deleted (not cascades)."""
    return queryset.delete()[1].get(queryset.model._meta.label, 0)


def cleanup(now):
    """Expired codes and tokens, connections that can no longer be refreshed,
    and clients nobody connected within ``UNUSED_CLIENT_DAYS``."""
    counts = {
        "codes": delete(AuthorizationCode.objects.filter(expires__lte=now)),
        "access tokens": delete(AccessToken.objects.filter(expires__lte=now)),
        "refresh tokens": delete(RefreshToken.objects.filter(expires__lte=now)),
    }
    live = RefreshToken.objects.filter(
        grant=OuterRef("pk"), used__isnull=True, expires__gt=now
    )
    active = AccessToken.objects.filter(grant=OuterRef("pk"), expires__gt=now)
    counts["connections"] = delete(Grant.objects.filter(~Exists(live), ~Exists(active)))
    cutoff = now - timedelta(days=conf.get("UNUSED_CLIENT_DAYS"))
    unused = Client.objects.filter(
        kind__in=[Client.REGISTERED, Client.METADATA_DOCUMENT],
        created__lte=cutoff,
        grants__isnull=True,
    ).exclude(last_used__gt=cutoff)
    counts["clients"] = delete(unused)
    return counts
