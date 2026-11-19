from django.contrib.auth import get_user_model


def resolve(request, token):
    """Test tokens are ``token-<username>``."""
    if not token.startswith("token-"):
        return None
    return get_user_model().objects.filter(username=token[len("token-") :]).first()
