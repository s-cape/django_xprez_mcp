"""A user's connected clients, for an account page next to personal tokens."""

from .models import Grant


def for_user(user):
    return Grant.objects.filter(user=user).select_related("client")


def disconnect(user, grant_id):
    """Revokes every token of the connection; True if it existed."""
    try:
        grant_id = int(grant_id)
    except (TypeError, ValueError):
        return False
    deleted, _rows = Grant.objects.filter(user=user, pk=grant_id).delete()
    return bool(deleted)
