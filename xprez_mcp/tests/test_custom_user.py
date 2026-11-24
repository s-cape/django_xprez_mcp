from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings

from xprez_mcp.contrib.oauth.models import Client, Grant
from xprez_mcp.contrib.tokens.models import Token
from xprez_mcp.permissions import allow_staff

from .test_contrib_tokens import ADMIN, STORED
from .test_protocol import MCPTestCase


def missing(name):
    def get(user):
        raise AttributeError(f"'User' object has no attribute {name!r}")

    # the setter lets model __init__ assign the database value
    return property(get, lambda user, value: None)


@contextmanager
def without(*names):
    """User as a custom user model without PermissionsMixin (or is_staff):
    the attributes are missing and its own has_perm() grants everything."""
    attributes = {name: missing(name) for name in names}
    if "is_superuser" in names:
        attributes["has_perm"] = lambda user, perm, obj=None: True
        attributes["has_module_perms"] = lambda user, app_label: True
    with mock.patch.multiple(User, **attributes):
        yield


@override_settings(XPREZ_MCP=STORED)
class CustomUserTests(MCPTestCase):
    def test_admin_without_is_superuser(self):
        Token.objects.create_token(self.user, "their token")
        client, _ = Client.objects.create_client("Desk", ["https://desk.example/cb"])
        Grant.objects.create(user=self.user, client=client, resource="their grant")
        self.client.force_login(self.staff)
        with without("is_superuser"):
            for url, text in [
                (f"{ADMIN}xprez_mcp_tokens/token/", "their token"),
                (f"{ADMIN}xprez_mcp_oauth/grant/", "their grant"),
            ]:
                with self.subTest(url=url):
                    self.assertContains(self.client.get(url), text)
            page = self.client.get(f"{ADMIN}xprez_mcp_tokens/token/add/")
            users = page.context["adminform"].form.fields["user"].queryset
            self.assertIn(self.user, users)

    def test_allow_staff_without_is_staff(self):
        with without("is_staff"):
            self.assertFalse(allow_staff(SimpleNamespace(user=self.staff)))
