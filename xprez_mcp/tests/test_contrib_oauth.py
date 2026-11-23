import base64
import json
import secrets
import socket
import time
from datetime import timedelta
from io import StringIO
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from django.core.cache import cache
from django.core.management import call_command
from django.test import Client as DjangoClient
from django.test import override_settings
from django.utils import timezone

from xprez_mcp import MCPServer, auth
from xprez_mcp.contrib.oauth import cimd, connections
from xprez_mcp.contrib.oauth.admin import ClientForm
from xprez_mcp.contrib.oauth.auth import access_token
from xprez_mcp.contrib.oauth.checks import check_oauth
from xprez_mcp.contrib.oauth.errors import ClientError
from xprez_mcp.contrib.oauth.models import (
    AccessToken,
    AuthorizationCode,
    Client,
    Grant,
    RefreshToken,
)
from xprez_mcp.contrib.oauth.views import s256

from .test_protocol import MCPTestCase

MCP = "/oauth/mcp/"
RESOURCE = "http://testserver/oauth/mcp/"
AUTHORIZE = "/oauth/mcp/oauth/authorize/"
TOKEN = "/oauth/mcp/oauth/token/"
REGISTER = "/oauth/mcp/oauth/register/"
REVOKE = "/oauth/mcp/oauth/revoke/"
CONNECTIONS = "/account/connections/"
CSRF_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
REDIRECT = "https://client.example/callback"
WHOAMI = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "whoami", "arguments": {}},
}
CIMD_URL = "https://assistant.example/oauth/client.json"
DOCUMENT = {
    "client_id": CIMD_URL,
    "client_name": "Assistant",
    "redirect_uris": ["https://assistant.example/callback"],
    "token_endpoint_auth_method": "none",
}


class OAuthTestCase(MCPTestCase):
    url = MCP

    def setUp(self):
        cache.clear()

    def register(self, **data):
        data.setdefault("redirect_uris", [REDIRECT])
        data.setdefault("client_name", "Test client")
        data.setdefault("token_endpoint_auth_method", "none")
        return self.client.post(REGISTER, data, content_type="application/json")

    def public_client(self, **data):
        response = self.register(**data)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def authorize(self, client_id, verifier=None, method="get", **params):
        verifier = verifier or secrets.token_urlsafe(32)
        query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": s256(verifier),
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": RESOURCE,
            **params,
        }
        query = {key: value for key, value in query.items() if value is not None}
        if method == "get":
            return self.client.get(AUTHORIZE, query)

        return self.client.post(f"{AUTHORIZE}?{urlencode(query)}", {"allow": ""})

    def code(self, client_id, verifier, **params):
        self.client.force_login(self.user)
        response = self.authorize(client_id, verifier, method="post", **params)
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlsplit(response["Location"]).query)["code"][0]

    def exchange(self, client_id, code, verifier, **data):
        headers = {key: data.pop(key) for key in list(data) if key.startswith("HTTP_")}
        return self.client.post(
            TOKEN,
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT,
                "resource": RESOURCE,
                **data,
            },
            **headers,
        )

    def tokens(self, client_id=None):
        client_id = client_id or self.public_client()["client_id"]
        verifier = secrets.token_urlsafe(32)
        response = self.exchange(client_id, self.code(client_id, verifier), verifier)
        self.assertEqual(response.status_code, 200, response.content)
        return client_id, response.json()

    def whoami(self, token):
        return self.post(WHOAMI, token=token)


class MetadataTests(OAuthTestCase):
    def test_protected_resource(self):
        for url in (
            "/oauth/mcp/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/oauth/mcp",
            "/.well-known/oauth-protected-resource/oauth/mcp/",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertEqual(
                response.json(),
                {
                    "resource": RESOURCE,
                    "authorization_servers": ["http://testserver/oauth/mcp"],
                    "bearer_methods_supported": ["header"],
                    "resource_name": "OAuth tools",
                },
            )
            self.assertEqual(response["Access-Control-Allow-Origin"], "*")

    def test_authorization_server(self):
        response = self.client.get("/.well-known/oauth-authorization-server/oauth/mcp")
        metadata = response.json()
        self.assertEqual(metadata["issuer"], "http://testserver/oauth/mcp")
        self.assertEqual(
            metadata["authorization_endpoint"], f"http://testserver{AUTHORIZE}"
        )
        self.assertEqual(metadata["token_endpoint"], f"http://testserver{TOKEN}")
        self.assertEqual(
            metadata["registration_endpoint"], f"http://testserver{REGISTER}"
        )
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])
        self.assertIs(metadata["client_id_metadata_document_supported"], True)

    def test_features_off(self):
        with override_settings(
            XPREZ_MCP={
                "OAUTH": {
                    "DYNAMIC_REGISTRATION": False,
                    "CLIENT_ID_METADATA_DOCUMENTS": False,
                }
            }
        ):
            metadata = self.client.get(
                "/.well-known/oauth-authorization-server/oauth/mcp"
            ).json()
            self.assertEqual(self.register().status_code, 404)
        self.assertNotIn("registration_endpoint", metadata)
        self.assertNotIn("client_id_metadata_document_supported", metadata)

    def test_root_fallback_outside_namespace(self):
        server = MCPServer(name="not-reversible", permission=True)
        server.mounted = True
        with mock.patch("xprez_mcp.server._servers", {server}):
            response = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(response.status_code, 404)

    def test_unknown_path(self):
        self.assertEqual(
            self.client.get("/.well-known/oauth-authorization-server/nope").status_code,
            404,
        )
        # several servers are mounted: the root cannot pick one
        self.assertEqual(
            self.client.get("/.well-known/oauth-authorization-server").status_code,
            404,
        )

    def test_base_url(self):
        with override_settings(
            XPREZ_MCP={"OAUTH": {"BASE_URL": "https://mcp.example/"}}
        ):
            response = self.client.get(
                "/oauth/mcp/.well-known/oauth-protected-resource"
            )
        self.assertEqual(response.json()["resource"], "https://mcp.example/oauth/mcp/")

    def test_challenge(self):
        response = self.post(WHOAMI, token=None)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response["WWW-Authenticate"],
            'Bearer resource_metadata="http://testserver/oauth/mcp/.well-known/'
            'oauth-protected-resource"',
        )
        response = self.post(WHOAMI, token="mcpoat_unknown")
        self.assertTrue(
            response["WWW-Authenticate"].endswith(', error="invalid_token"')
        )

    def test_without_trailing_slash(self):
        client_id, tokens = self.tokens()
        response = self.post(WHOAMI, token=tokens["access_token"], url="/oauth/mcp")
        self.assertEqual(response.status_code, 200, response.content)
        response = self.post(WHOAMI, token=None, url="/oauth/mcp")
        self.assertIn("/oauth/mcp/.well-known/", response["WWW-Authenticate"])
        # other paths without the slash are left alone
        self.assertEqual(self.client.get("/oauth/mcp/oauth/token").status_code, 404)
        self.assertEqual(self.client.get("/nothing").status_code, 404)

    def test_challenge_for_newer_protocol_version(self):
        """A client of a newer spec learns about sign-in before anything else."""
        initialize = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2099-01-01",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        response = self.post(
            initialize, token=None, HTTP_MCP_PROTOCOL_VERSION="2099-01-01"
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("resource_metadata=", response["WWW-Authenticate"])

    def test_servers_without_backend_unchanged(self):
        response = self.post(WHOAMI, token=None, url="/mcp/")
        self.assertEqual(response["WWW-Authenticate"], "Bearer")


class FlowTests(OAuthTestCase):
    def test_full_flow(self):
        client_id = self.public_client()["client_id"]
        verifier = secrets.token_urlsafe(32)

        response = self.authorize(client_id, verifier)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/accounts/login/?next="))

        self.client.force_login(self.user)
        response = self.authorize(client_id, verifier)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test client")
        self.assertContains(response, "client.example")
        self.assertContains(response, "alice")
        self.assertEqual(response["X-Frame-Options"], "DENY")

        response = self.authorize(client_id, verifier, method="post")
        location = urlsplit(response["Location"])
        self.assertEqual(
            f"{location.scheme}://{location.netloc}{location.path}", REDIRECT
        )
        query = parse_qs(location.query)
        self.assertEqual(query["state"], ["xyz"])
        self.assertEqual(query["iss"], ["http://testserver/oauth/mcp"])

        response = self.exchange(client_id, query["code"][0], verifier)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Cache-Control"], "no-store")
        tokens = response.json()
        self.assertEqual(tokens["token_type"], "Bearer")
        self.assertEqual(tokens["expires_in"], 3600)

        response = self.whoami(tokens["access_token"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["result"]["content"][0]["text"], self.user.username
        )
        grant = Grant.objects.get()
        self.assertEqual(grant.resource, RESOURCE)
        self.assertEqual(grant.user, self.user)

    @override_settings(MIDDLEWARE=CSRF_MIDDLEWARE)
    def test_allow_passes_csrf_check(self):
        """A browser sends the page's Origin on the consent POST; with a
        "no-referrer" policy it would send "Origin: null" and fail CSRF."""
        client_id = self.public_client()["client_id"]
        browser = DjangoClient(enforce_csrf_checks=True)
        browser.force_login(self.user)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "code_challenge": s256(secrets.token_urlsafe(32)),
                "code_challenge_method": "S256",
            }
        )
        url = f"{AUTHORIZE}?{query}"
        page = browser.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="referrer"')
        token = page.cookies["csrftoken"].value
        data = {"allow": "", "csrfmiddlewaretoken": token}
        response = browser.post(url, data, HTTP_ORIGIN="null")
        self.assertEqual(response.status_code, 403)
        response = browser.post(url, data, HTTP_ORIGIN="http://testserver")
        self.assertEqual(response.status_code, 302)
        self.assertIn("code=", response["Location"])

    def test_referrer_policy(self):
        client_id = self.public_client()["client_id"]
        responses = [self.authorize(client_id)]  # signed out: login redirect
        self.client.force_login(self.user)
        responses += [
            self.authorize(client_id),  # consent page
            self.authorize(client_id, method="post"),  # redirect with the code
            self.authorize("mcpc_unknown"),  # error page
        ]
        self.assertEqual(
            [response.status_code for response in responses], [302, 200, 302, 400]
        )
        for response in responses:
            self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_only_hashes_stored(self):
        client_id, tokens = self.tokens()
        stored = [
            *AccessToken.objects.values_list("key_hash", flat=True),
            *RefreshToken.objects.values_list("key_hash", flat=True),
        ]
        self.assertNotIn(tokens["access_token"], stored)
        self.assertNotIn(tokens["refresh_token"], stored)
        self.assertTrue(tokens["access_token"].startswith("mcpoat_"))

    def test_deny(self):
        client_id = self.public_client()["client_id"]
        self.client.force_login(self.user)
        query = f"client_id={client_id}&redirect_uri={REDIRECT}&response_type=code"
        query += f"&code_challenge={s256('x' * 43)}&code_challenge_method=S256"
        response = self.client.post(f"{AUTHORIZE}?{query}", {"deny": ""})
        self.assertIn("error=access_denied", response["Location"])
        self.assertFalse(AuthorizationCode.objects.exists())

    def test_pkce_required(self):
        client_id = self.public_client()["client_id"]
        self.client.force_login(self.user)
        for params in ({"code_challenge_method": "plain"}, {"code_challenge": None}):
            response = self.authorize(client_id, **params)
            self.assertEqual(response.status_code, 302)
            self.assertIn("error=invalid_request", response["Location"])

    def test_wrong_verifier(self):
        client_id = self.public_client()["client_id"]
        verifier = secrets.token_urlsafe(32)
        code = self.code(client_id, verifier)
        response = self.exchange(client_id, code, secrets.token_urlsafe(32))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")
        # a wrong verifier does not burn the code
        self.assertEqual(self.exchange(client_id, code, verifier).status_code, 200)

    def test_code_reuse_revokes(self):
        client_id = self.public_client()["client_id"]
        verifier = secrets.token_urlsafe(32)
        code = self.code(client_id, verifier)
        tokens = self.exchange(client_id, code, verifier).json()
        response = self.exchange(client_id, code, verifier)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertEqual(self.whoami(tokens["access_token"]).status_code, 401)

    def test_expired_code(self):
        client_id = self.public_client()["client_id"]
        verifier = secrets.token_urlsafe(32)
        code = self.code(client_id, verifier)
        AuthorizationCode.objects.update(expires=timezone.now())
        self.assertEqual(self.exchange(client_id, code, verifier).status_code, 400)

    def test_code_of_another_client(self):
        first = self.public_client()["client_id"]
        second = self.public_client()["client_id"]
        verifier = secrets.token_urlsafe(32)
        code = self.code(first, verifier)
        response = self.exchange(second, code, verifier)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_wrong_resource(self):
        client_id = self.public_client()["client_id"]
        self.client.force_login(self.user)
        response = self.authorize(client_id, resource="https://other.example/mcp")
        self.assertIn("error=invalid_target", response["Location"])

        verifier = secrets.token_urlsafe(32)
        code = self.code(client_id, verifier)
        response = self.exchange(
            client_id, code, verifier, resource="https://other.example/mcp"
        )
        self.assertEqual(response.json()["error"], "invalid_target")

    def test_redirect_uri_as_in_authorization(self):
        client_id = self.public_client()["client_id"]
        verifier = secrets.token_urlsafe(32)
        code = self.code(client_id, verifier)
        response = self.exchange(client_id, code, verifier, redirect_uri="")
        self.assertEqual(response.json()["error"], "invalid_grant")
        # left out in both: fine for a client with one redirect URI
        code = self.code(client_id, verifier, redirect_uri=None)
        response = self.exchange(client_id, code, verifier, redirect_uri="")
        self.assertEqual(response.status_code, 200, response.content)

    def test_challenge_format(self):
        client_id = self.public_client()["client_id"]
        self.client.force_login(self.user)
        response = self.authorize(client_id, code_challenge="A" * 43 + "\n")
        self.assertIn("error=invalid_request", response["Location"])

    def test_resource_forms(self):
        client_id = self.public_client()["client_id"]
        for resource in (None, "http://TESTSERVER/oauth/mcp"):
            verifier = secrets.token_urlsafe(32)
            code = self.code(client_id, verifier, resource=resource)
            response = self.exchange(client_id, code, verifier, resource=resource or "")
            self.assertEqual(response.status_code, 200, response.content)

    def test_token_for_another_endpoint(self):
        client_id, tokens = self.tokens()
        Grant.objects.update(resource="http://testserver/mcp/")
        response = self.whoami(tokens["access_token"])
        self.assertEqual(response.status_code, 401)

    def test_expired_token(self):
        client_id, tokens = self.tokens()
        AccessToken.objects.update(expires=timezone.now())
        response = self.whoami(tokens["access_token"])
        self.assertEqual(response.status_code, 401)
        self.assertIn('error="invalid_token"', response["WWW-Authenticate"])

    def test_inactive_user(self):
        client_id, tokens = self.tokens()
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.whoami(tokens["access_token"]).status_code, 401)

    def test_refresh_rotates(self):
        client_id, tokens = self.tokens()
        refresh = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
        }
        response = self.client.post(TOKEN, refresh)
        self.assertEqual(response.status_code, 200, response.content)
        new = response.json()
        self.assertNotEqual(new["refresh_token"], tokens["refresh_token"])
        self.assertEqual(self.whoami(new["access_token"]).status_code, 200)

        # the old refresh token again, a minute later: stolen, so the
        # connection is revoked
        RefreshToken.objects.filter(used__isnull=False).update(
            used=timezone.now() - timedelta(minutes=1)
        )
        response = self.client.post(TOKEN, refresh)
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertFalse(Grant.objects.exists())
        self.assertEqual(self.whoami(new["access_token"]).status_code, 401)

    def test_refresh_retry(self):
        client_id, tokens = self.tokens()
        refresh = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
        }
        first = self.client.post(TOKEN, refresh).json()
        # the response was lost and the client sends the same token again
        response = self.client.post(TOKEN, refresh)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.whoami(response.json()["access_token"]).status_code, 200)
        self.assertEqual(self.whoami(first["access_token"]).status_code, 200)

    def test_refresh_by_another_client(self):
        client_id, tokens = self.tokens()
        other = self.public_client()["client_id"]
        response = self.client.post(
            TOKEN,
            {
                "grant_type": "refresh_token",
                "client_id": other,
                "refresh_token": tokens["refresh_token"],
            },
        )
        self.assertEqual(response.json()["error"], "invalid_grant")
        self.assertTrue(Grant.objects.exists())

    def test_expired_refresh_token(self):
        client_id, tokens = self.tokens()
        RefreshToken.objects.update(expires=timezone.now())
        response = self.client.post(
            TOKEN,
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
            },
        )
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_unsupported_grant_type(self):
        client_id = self.public_client()["client_id"]
        response = self.client.post(
            TOKEN, {"grant_type": "password", "client_id": client_id}
        )
        self.assertEqual(response.json()["error"], "unsupported_grant_type")

    def test_bearer_tokens_still_work(self):
        self.assertEqual(self.whoami("token-alice").status_code, 200)

    @override_settings(MIDDLEWARE=CSRF_MIDDLEWARE)
    def test_disconnect_page_passes_csrf_check(self):
        """The connections page shares base.html with the consent page."""
        client_id, tokens = self.tokens()
        grant = Grant.objects.get()
        browser = DjangoClient(enforce_csrf_checks=True)
        browser.force_login(self.user)
        page = browser.get(CONNECTIONS)
        self.assertContains(page, "Test client")
        self.assertNotContains(page, 'name="referrer"')
        data = {
            "grant": grant.pk,
            "csrfmiddlewaretoken": page.cookies["csrftoken"].value,
        }
        response = browser.post(CONNECTIONS, data, HTTP_ORIGIN="http://testserver")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Grant.objects.exists())
        self.assertEqual(self.whoami(tokens["access_token"]).status_code, 401)

    def test_disconnect(self):
        client_id, tokens = self.tokens()
        grant = connections.for_user(self.user).get()
        self.assertFalse(connections.disconnect(self.staff, grant.pk))
        self.assertTrue(connections.disconnect(self.user, grant.pk))
        self.assertEqual(self.whoami(tokens["access_token"]).status_code, 401)

    def test_revoke(self):
        client_id, tokens = self.tokens()
        response = self.client.post(
            REVOKE, {"client_id": client_id, "token": tokens["access_token"]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.whoami(tokens["access_token"]).status_code, 401)
        self.assertTrue(Grant.objects.exists())
        self.client.post(
            REVOKE, {"client_id": client_id, "token": tokens["refresh_token"]}
        )
        self.assertFalse(Grant.objects.exists())


class ClientTests(OAuthTestCase):
    def test_unregistered_redirect_uri(self):
        client_id = self.public_client()["client_id"]
        self.client.force_login(self.user)
        response = self.authorize(client_id, redirect_uri="https://evil.example/cb")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Location", response)

    def test_unknown_client(self):
        self.client.force_login(self.user)
        self.assertEqual(self.authorize("mcpc_nope").status_code, 400)

    def test_registration_validates_redirect_uris(self):
        for uris in (
            ["http://client.example/cb"],
            ["https://client.example/cb#x"],
            ["javascript:alert(1)"],
            [],
        ):
            response = self.register(redirect_uris=uris)
            self.assertEqual(response.status_code, 400, uris)
            self.assertEqual(response.json()["error"], "invalid_redirect_uri")

    def test_loopback_any_port(self):
        client_id = self.public_client(redirect_uris=["http://127.0.0.1:3000/cb"])[
            "client_id"
        ]
        self.client.force_login(self.user)
        response = self.authorize(client_id, redirect_uri="http://127.0.0.1:51234/cb")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "this computer")
        response = self.authorize(client_id, redirect_uri="http://127.0.0.1:5/other")
        self.assertEqual(response.status_code, 400)

    def test_confidential_client(self):
        response = self.register(token_endpoint_auth_method="client_secret_basic")
        data = response.json()
        secret = data["client_secret"]
        client_id = data["client_id"]
        verifier = secrets.token_urlsafe(32)
        code = self.code(client_id, verifier)
        response = self.exchange(client_id, code, verifier)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "invalid_client")

        basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        code = self.code(client_id, verifier)
        response = self.exchange(
            client_id, code, verifier, HTTP_AUTHORIZATION=f"Basic {basic}"
        )
        self.assertEqual(response.status_code, 200, response.content)
        client = Client.objects.get(client_id=client_id)
        self.assertNotEqual(client.secret_hash, secret)

    def test_registration_rate_limit(self):
        with override_settings(XPREZ_MCP={"OAUTH": {"REGISTRATION_RATE": (2, 60)}}):
            statuses = [self.register().status_code for _ in range(3)]
        self.assertEqual(statuses, [201, 201, 429])

    def test_registration_rejects_other_flows(self):
        response = self.register(grant_types=["client_credentials"])
        self.assertEqual(response.json()["error"], "invalid_client_metadata")
        response = self.client.post(REGISTER, "[]", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_admin_validates_redirect_uris(self):
        client, secret = Client.objects.create_client("Desk", [REDIRECT])
        for uris, valid in (
            ([REDIRECT], True),
            (["http://evil.example/cb"], False),
            (["javascript:alert(1)"], False),
            ([], False),
        ):
            form = ClientForm(
                {"name": "Desk", "redirect_uris": json.dumps(uris)}, instance=client
            )
            self.assertEqual(form.is_valid(), valid, uris)

    def test_manual_client(self):
        client, secret = Client.objects.create_client("Desk", [REDIRECT])
        self.assertIsNone(secret)
        verifier = secrets.token_urlsafe(32)
        code = self.code(client.client_id, verifier)
        response = self.exchange(client.client_id, code, verifier)
        self.assertEqual(response.status_code, 200)


@mock.patch.object(cimd, "fetch")
class MetadataDocumentTests(OAuthTestCase):
    def authorize_cimd(self, **params):
        self.client.force_login(self.user)
        return self.authorize(
            CIMD_URL, redirect_uri=DOCUMENT["redirect_uris"][0], **params
        )

    def test_consent_and_tokens(self, fetch):
        fetch.return_value = (DOCUMENT, None)
        response = self.authorize_cimd()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assistant")
        self.assertContains(response, "published by <span")
        verifier = secrets.token_urlsafe(32)
        code = self.code(CIMD_URL, verifier, redirect_uri=DOCUMENT["redirect_uris"][0])
        response = self.exchange(
            CIMD_URL, code, verifier, redirect_uri=DOCUMENT["redirect_uris"][0]
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(fetch.call_count, 1)  # cached in the Client row

    def test_cache_expiry(self, fetch):
        fetch.return_value = (DOCUMENT, 0)
        self.authorize_cimd()
        Client.objects.update(refresh_after=timezone.now())
        self.authorize_cimd()
        self.assertEqual(fetch.call_count, 2)

    def test_client_id_mismatch(self, fetch):
        fetch.return_value = ({**DOCUMENT, "client_id": "https://x.example/c"}, None)
        self.assertEqual(self.authorize_cimd().status_code, 400)
        self.assertFalse(Client.objects.exists())

    def test_redirect_uri_not_in_document(self, fetch):
        fetch.return_value = (DOCUMENT, None)
        self.client.force_login(self.user)
        response = self.authorize(CIMD_URL, redirect_uri=REDIRECT)
        self.assertEqual(response.status_code, 400)

    def test_invalid_documents(self, fetch):
        for document in (
            [],
            {**DOCUMENT, "redirect_uris": []},
            {**DOCUMENT, "redirect_uris": ["http://assistant.example/cb"]},
            {**DOCUMENT, "token_endpoint_auth_method": "private_key_jwt"},
        ):
            fetch.return_value = (document, None)
            self.assertEqual(self.authorize_cimd().status_code, 400, document)

    def test_fetch_error(self, fetch):
        fetch.side_effect = ClientError("unreachable")
        response = self.authorize_cimd()
        self.assertContains(response, "unreachable", status_code=400)
        # a failed URL is not fetched again for a minute
        self.assertEqual(self.authorize_cimd().status_code, 400)
        self.assertEqual(fetch.call_count, 1)

    def test_stale_copy_when_fetch_fails(self, fetch):
        fetch.return_value = (DOCUMENT, None)
        self.authorize_cimd()
        fetch.side_effect = ClientError("down")
        Client.objects.update(refresh_after=timezone.now())
        self.assertEqual(self.authorize_cimd().status_code, 200)
        # also while the failure is remembered
        self.assertEqual(self.authorize_cimd().status_code, 200)
        self.assertEqual(fetch.call_count, 2)
        # but not a copy older than the longest cache period
        cache.clear()
        Client.objects.update(refresh_after=timezone.now() - timedelta(days=2))
        self.assertEqual(self.authorize_cimd().status_code, 400)

    def test_fetch_rate(self, fetch):
        def document(url):
            return {**DOCUMENT, "client_id": url}, None

        fetch.side_effect = document
        self.client.force_login(self.user)
        redirect_uri = DOCUMENT["redirect_uris"][0]
        with override_settings(XPREZ_MCP={"OAUTH": {"METADATA_FETCH_RATE": (2, 60)}}):
            statuses = [
                self.authorize(
                    f"https://assistant.example/c{number}.json",
                    redirect_uri=redirect_uri,
                ).status_code
                for number in range(3)
            ]
            self.assertEqual(statuses, [200, 200, 400])
            # a stale copy is still served while fetching is throttled
            Client.objects.update(refresh_after=timezone.now())
            response = self.authorize(
                "https://assistant.example/c0.json", redirect_uri=redirect_uri
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch.call_count, 2)

    def test_disabled(self, fetch):
        fetch.return_value = (DOCUMENT, None)
        with override_settings(
            XPREZ_MCP={"OAUTH": {"CLIENT_ID_METADATA_DOCUMENTS": False}}
        ):
            self.assertEqual(self.authorize_cimd().status_code, 400)
        fetch.assert_not_called()


def addresses(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


class SafeFetchTests(OAuthTestCase):
    def test_public_host(self):
        with mock.patch("socket.getaddrinfo", return_value=addresses("93.184.215.14")):
            self.assertEqual(
                cimd.public_addresses(CIMD_URL),
                ("assistant.example", 443, ["93.184.215.14"]),
            )

    def test_internal_hosts(self):
        for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1"):
            with mock.patch("socket.getaddrinfo", return_value=addresses(ip)):
                with self.assertRaises(ClientError, msg=ip):
                    cimd.public_addresses(CIMD_URL)
        # one internal address among public ones is enough to refuse
        with mock.patch(
            "socket.getaddrinfo", return_value=addresses("93.184.215.14", "10.0.0.1")
        ):
            with self.assertRaises(ClientError):
                cimd.public_addresses(CIMD_URL)

    def test_resolver_order_kept(self):
        with mock.patch(
            "socket.getaddrinfo",
            return_value=addresses("93.184.215.14", "2606:2800::1", "93.184.215.14"),
        ):
            self.assertEqual(
                cimd.public_addresses(CIMD_URL)[2], ["93.184.215.14", "2606:2800::1"]
            )

    def test_next_address_when_one_fails(self):
        tried = []

        def create_connection(address, timeout):
            tried.append(address[0])
            if address[0] == "2606:2800::1":
                raise OSError("no route")
            return mock.Mock()

        connection = cimd.PinnedHTTPSConnection(
            "assistant.example",
            443,
            ["2606:2800::1", "93.184.215.14"],
            time.monotonic() + 5,
        )
        connection.ssl_context = mock.Mock()
        with mock.patch("socket.create_connection", create_connection):
            connection.connect()
        self.assertEqual(tried, ["2606:2800::1", "93.184.215.14"])
        self.assertIsInstance(connection.sock, cimd.DeadlineSocket)

    def test_deadline_covers_slow_reads(self):
        sock = mock.Mock()
        sock.recv_into.return_value = 1
        deadline_socket = cimd.DeadlineSocket(sock, time.monotonic() + 60)
        self.assertEqual(deadline_socket.recv_into(bytearray(1)), 1)
        # the remaining time, not a fresh timeout, applies to each read
        self.assertLessEqual(sock.settimeout.call_args[0][0], 60)
        deadline_socket.deadline = time.monotonic() - 1
        with self.assertRaises(socket.timeout):
            deadline_socket.recv_into(bytearray(1))
        with self.assertRaises(socket.timeout):
            deadline_socket.makefile("rb").read(1)

    def test_https_only(self):
        for url in (
            "http://assistant.example/c.json",
            "https://user@assistant.example/c.json",
            "https://assistant.example/c.json#x",
        ):
            with self.assertRaises(ClientError, msg=url):
                cimd.public_addresses(url)

    def test_is_metadata_url(self):
        self.assertTrue(cimd.is_metadata_url(CIMD_URL))
        self.assertFalse(cimd.is_metadata_url("https://assistant.example/"))
        self.assertFalse(cimd.is_metadata_url("mcpc_abc"))

    def test_max_age(self):
        self.assertEqual(cimd.max_age("public, max-age=600"), 600)
        self.assertEqual(cimd.max_age("no-store"), 0)
        self.assertIsNone(cimd.max_age(""))


class MaintenanceTests(OAuthTestCase):
    def test_cleanup(self):
        client_id, tokens = self.tokens()
        unused = self.public_client()["client_id"]
        Client.objects.filter(client_id=unused).update(
            created=timezone.now() - timedelta(days=30)
        )
        out = StringIO()
        call_command("mcp_oauth_cleanup", stdout=out)
        self.assertIn("1 clients", out.getvalue())
        self.assertTrue(Grant.objects.exists())

        AccessToken.objects.update(expires=timezone.now())
        RefreshToken.objects.update(expires=timezone.now())
        call_command("mcp_oauth_cleanup", stdout=out)
        self.assertFalse(Grant.objects.exists())

    def test_backend_order_check(self):
        server = MCPServer(
            name="wrong-order",
            authentication=[auth.bearer_token, access_token],
            permission=True,
        )
        server.mounted = True
        ids = [error.id for error in check_oauth()]
        self.assertIn("xprez_mcp_oauth.W002", ids)
        self.assertNotIn("xprez_mcp_oauth.W001", ids)
        server.mounted = False
