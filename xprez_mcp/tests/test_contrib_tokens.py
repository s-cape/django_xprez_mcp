import json
import re
from datetime import timedelta

from django.contrib.auth.models import Permission, User
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.utils import timezone

from xprez_mcp import mcp
from xprez_mcp.auth import BearerToken
from xprez_mcp.contrib.tokens.auth import resolve
from xprez_mcp.contrib.tokens.models import Token, hash_key
from xprez_mcp.permissions import allow_authenticated

from .test_protocol import MCPTestCase

PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
# the test settings without TOKEN_RESOLVER: the installed app resolves keys
STORED = {
    "NAME": "test",
    "PERMISSION": allow_authenticated,
}
ADMIN = "/token-admin/"


def perms(*codenames):
    return Permission.objects.filter(
        content_type__app_label="xprez_mcp_tokens", codename__in=codenames
    )


@override_settings(XPREZ_MCP=STORED)
class ResolverTests(MCPTestCase):
    def key(self, user=None, **kwargs):
        return Token.objects.create_token(user or self.user, "laptop", **kwargs)

    def ping(self, key, url=None):
        return self.post(PING, token=key, url=url).status_code

    def test_default_resolver(self):
        self.assertIs(BearerToken().get_resolver(), resolve)
        token, key = self.key()
        self.assertEqual(self.ping(key), 200)
        with override_settings(
            XPREZ_MCP={
                **STORED,
                "TOKEN_RESOLVER": "xprez_mcp.tests.testapp.tokens.resolve",
            }
        ):
            self.assertEqual(self.ping(key), 401)

    def test_needs_resolver_or_app(self):
        with self.modify_settings(
            INSTALLED_APPS={"remove": "xprez_mcp.contrib.tokens"}
        ):
            with self.assertRaises(ImproperlyConfigured):
                BearerToken().get_resolver()

    def test_only_hash_stored(self):
        token, key = self.key()
        token.refresh_from_db()
        self.assertEqual(token.key_hash, hash_key(key))
        prefix = f"mcp_{token.key_id}_"
        self.assertTrue(key.startswith(prefix))
        # the whole secret: token_urlsafe() output can itself contain "_"
        self.assertNotIn(
            key[len(prefix) :],
            json.dumps(list(Token.objects.values()), default=str),
        )

    def test_without_request(self):
        token, key = self.key()
        self.assertEqual(resolve(None, key), self.user)

    def test_revoked_by_deleting(self):
        token, key = self.key()
        token.delete()
        self.assertEqual(self.ping(key), 401)

    def test_several_tokens_per_user(self):
        first, first_key = self.key()
        second, second_key = self.key()
        first.delete()
        self.assertEqual(self.ping(first_key), 401)
        self.assertEqual(self.ping(second_key), 200)

    def test_expiry(self):
        past = timezone.now() - timedelta(seconds=1)
        self.assertEqual(self.ping(self.key(expires=past)[1]), 401)
        future = timezone.now() + timedelta(days=1)
        self.assertEqual(self.ping(self.key(expires=future)[1]), 200)

    def test_inactive_user(self):
        user = User.objects.create_user("carol", is_active=False)
        self.assertEqual(self.ping(self.key(user)[1]), 401)
        backends = ["django.contrib.auth.backends.AllowAllUsersModelBackend"]
        with self.settings(AUTHENTICATION_BACKENDS=backends):
            self.assertEqual(self.ping(self.key(user)[1]), 401)

    def test_last_used(self):
        token, key = self.key()
        self.ping(key)
        token.refresh_from_db()
        first = token.last_used
        self.assertIsNotNone(first)
        self.ping(key)
        token.refresh_from_db()
        self.assertEqual(token.last_used, first)  # at most once a minute
        Token.objects.filter(pk=token.pk).update(last_used=first - timedelta(minutes=2))
        self.ping(key)
        token.refresh_from_db()
        self.assertGreater(token.last_used, first)

    def test_forged_keys(self):
        token, key = self.key()
        _, key_id, secret = key.split("_", 2)
        other = self.key(self.staff)[0].key_id
        for forged in [
            "mcp",
            "mcp_",
            "mcp__",
            f"mcp_{key_id}",
            f"mcp_{key_id}_",
            f"mcp_{key_id}_{secret[:-1]}",
            f"mcp_{key_id}_{secret}x",
            f"mcp_{other}_{secret}",
            f"xyz_{key_id}_{secret}",
            f"mcp_nothere_{secret}",
            token.key_hash,
        ]:
            with self.subTest(forged=forged):
                self.assertEqual(self.ping(forged), 401)


class TokenAdminTests(MCPTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.staff.user_permissions.add(
            *perms("add_token", "view_token", "change_token", "delete_token")
        )
        cls.root = User.objects.create_superuser("root")

    def add(self, viewer, data):
        self.client.force_login(viewer)
        data = {"user": viewer.pk, "expires": "", **data}
        url = f"{ADMIN}xprez_mcp_tokens/token/add/"
        return self.client.post(url, data, follow=True)

    def key(self, response):
        text = " ".join(str(message) for message in response.context["messages"])
        key, header = re.findall(r"<code>(.+?)</code>", text)
        self.assertEqual(header, f"Authorization: Bearer {key}")
        return key

    def test_key_shown_once(self):
        response = self.add(self.staff, {"name": "laptop"})
        self.assertEqual(response.redirect_chain[0][1], 302)
        token = Token.objects.get()
        self.assertEqual(token.user, self.staff)
        key = self.key(response)
        self.assertEqual(token.key_hash, hash_key(key))
        for url in [
            f"{ADMIN}xprez_mcp_tokens/token/{token.pk}/change/",
            f"{ADMIN}xprez_mcp_tokens/token/",
        ]:
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertNotContains(page, key)
                self.assertContains(page, token.masked_key)
        with override_settings(XPREZ_MCP=STORED):
            self.assertEqual(self.post(PING, token=key).status_code, 200)

    def test_needs_add_permission(self):
        self.user.is_staff = True
        self.user.save()
        self.assertEqual(self.add(self.user, {"name": "x"}).status_code, 403)
        self.assertFalse(Token.objects.exists())

    def test_only_for_yourself(self):
        response = self.add(self.staff, {"user": self.user.pk, "name": "x"})
        self.assertIn("user", response.context["adminform"].form.errors)
        self.assertFalse(Token.objects.exists())
        page = self.client.get(f"{ADMIN}xprez_mcp_tokens/token/add/")
        self.assertEqual(page.context["adminform"].form["user"].value(), self.staff.pk)

    def test_superuser_for_others(self):
        self.add(self.root, {"user": self.user.pk, "name": "x"})
        self.assertEqual(Token.objects.get().user, self.user)

    def test_others_tokens_for_superusers(self):
        theirs, _ = Token.objects.create_token(self.user, "theirs")
        Token.objects.create_token(self.staff, "mine")
        self.client.force_login(self.staff)
        page = self.client.get(f"{ADMIN}xprez_mcp_tokens/token/")
        self.assertContains(page, "mine")
        self.assertNotContains(page, "theirs")
        url = f"{ADMIN}xprez_mcp_tokens/token/{theirs.pk}/"
        self.assertEqual(self.client.get(f"{url}change/").status_code, 302)
        self.client.post(f"{url}delete/", {"post": "yes"})
        self.assertTrue(Token.objects.filter(pk=theirs.pk).exists())
        self.client.force_login(self.root)
        self.assertContains(
            self.client.get(f"{ADMIN}xprez_mcp_tokens/token/"), "theirs"
        )

    def test_not_exposed_through_mcp(self):
        tools = self.result("tools/list", token="token-bob", url="/token-admin/mcp/")
        tools = {tool["name"]: tool for tool in tools["tools"]}
        enum = tools["record_get"]["inputSchema"]["properties"]["model"]["enum"]
        self.assertEqual(enum, ["users"])
