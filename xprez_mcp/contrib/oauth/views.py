import base64
import hashlib
import json
import re
import secrets
from datetime import timedelta
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, Resolver404, resolve, reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_deny
from django.views.decorators.csrf import csrf_exempt

from . import conf, connections, resources
from .clients import (
    AUTH_METHODS,
    get_client,
    is_loopback,
    match_redirect_uri,
    register_client,
)
from .errors import ClientError, OAuthError
from .throttling import client_ip, throttled
from .models import (
    ACCESS_PREFIX,
    REFRESH_PREFIX,
    AccessToken,
    AuthorizationCode,
    Client,
    Grant,
    RefreshToken,
    expiry,
    hash_key,
    new_key,
)

CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")


def cors(response):
    """Browser-based clients read these endpoints cross-origin; none of them
    uses cookies, so any origin is fine."""
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, MCP-Protocol-Version"
    )
    return response


@method_decorator(csrf_exempt, name="dispatch")
class CorsView(View):
    server = None

    def dispatch(self, request, *args, **kwargs):
        return cors(super().dispatch(request, *args, **kwargs))

    def options(self, request, *args, **kwargs):
        return HttpResponse(status=204)


def locate(rest):
    """``(server, namespace)`` of the MCP endpoint at ``/<rest>``, for the
    well-known URLs at the site root."""
    from xprez_mcp.server import _servers
    from xprez_mcp.views import MCPView

    path = "/" + (rest or "")
    for candidate in dict.fromkeys([path, path.rstrip("/") + "/"]):
        try:
            match = resolve(candidate)
        except Resolver404:
            continue
        view_class = getattr(match.func, "view_class", None)
        if view_class is not None and issubclass(view_class, MCPView):
            return match.func.view_initkwargs["server"], match.namespace
    if not rest:
        mounted = [server for server in _servers if server.mounted]
        if len(mounted) == 1:
            try:
                reverse(f"{mounted[0].name}:endpoint")
            except NoReverseMatch:  # mounted inside another namespace
                raise Http404
            return mounted[0], mounted[0].name
    raise Http404


class MetadataView(CorsView):
    http_method_names = ["get", "options"]

    def get(self, request, rest=None):
        if self.server is not None:
            server, namespace = self.server, request.resolver_match.namespace
        else:
            server, namespace = locate(rest)
        return JsonResponse(self.get_metadata(request, server, namespace))


class ResourceMetadataView(MetadataView):
    """RFC 9728."""

    def get_metadata(self, request, server, namespace):
        return {
            "resource": resources.resource(request, namespace),
            "authorization_servers": [resources.issuer(request, namespace)],
            "bearer_methods_supported": ["header"],
            "resource_name": server.title or server.name,
        }


class ServerMetadataView(MetadataView):
    """RFC 8414."""

    def get_metadata(self, request, server, namespace):
        def url(name):
            return resources.url(request, name, namespace)

        metadata = {
            "issuer": resources.issuer(request, namespace),
            "authorization_endpoint": url("oauth-authorize"),
            "token_endpoint": url("oauth-token"),
            "revocation_endpoint": url("oauth-revoke"),
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": AUTH_METHODS,
            "revocation_endpoint_auth_methods_supported": AUTH_METHODS,
            "authorization_response_iss_parameter_supported": True,
        }
        if conf.get("DYNAMIC_REGISTRATION"):
            metadata["registration_endpoint"] = url("oauth-register")
        if conf.get("CLIENT_ID_METADATA_DOCUMENTS"):
            metadata["client_id_metadata_document_supported"] = True
        return metadata


def add_query(url, values):
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True) + list(values.items())
    return urlunsplit(parts._replace(query=urlencode(query)))


@method_decorator([never_cache, xframe_options_deny], name="dispatch")
class AuthorizeView(View):
    """Checks the client and its redirect URI before anything else: until
    both are known good, errors are shown here and never redirected."""

    server = None
    template_name = "xprez_mcp/oauth/authorize.html"
    error_template_name = "xprez_mcp/oauth/error.html"

    def get(self, request):
        return self.handle(request, None)

    def post(self, request):
        return self.handle(request, "allow" in request.POST)

    def handle(self, request, allowed):
        params = request.GET
        try:
            client = get_client(params.get("client_id"), request)
            redirect_uri = match_redirect_uri(client, params.get("redirect_uri"))
        except ClientError as error:
            return render(
                request, self.error_template_name, {"error": error}, status=400
            )

        def back(**values):
            if "state" in params:
                values["state"] = params["state"]
            values["iss"] = resources.issuer(request)
            return HttpResponseRedirect(add_query(redirect_uri, values))

        if params.get("response_type") != "code":
            return back(error="unsupported_response_type")
        if params.get("code_challenge_method") != "S256" or not CHALLENGE.fullmatch(
            params.get("code_challenge", "")
        ):
            return back(
                error="invalid_request",
                error_description="PKCE with S256 is required.",
            )
        resource = resources.resource(request)
        if "resource" in params and resources.canonical(
            params["resource"]
        ) != resources.canonical(resource):
            return back(error="invalid_target")
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if allowed is None:
            return render(
                request,
                self.template_name,
                self.get_context(request, client, redirect_uri),
            )
        if not allowed:
            return back(error="access_denied")
        code, code_hash = new_key()
        AuthorizationCode.objects.create(
            code_hash=code_hash,
            user=request.user,
            client=client,
            resource=resource,
            redirect_uri=params.get("redirect_uri", ""),
            code_challenge=params["code_challenge"],
            expires=expiry(conf.get("CODE_LIFETIME")),
        )
        return back(code=code)

    def get_context(self, request, client, redirect_uri):
        return {
            "client": client,
            "client_name": client.name or client.client_id,
            "client_host": client.host,
            "redirect_host": urlsplit(redirect_uri).hostname,
            "loopback": is_loopback(redirect_uri),
            "user": request.user,
            "server": self.server,
            "resource": resources.resource(request),
        }


def authenticate_client(request):
    """The client of a token or revocation request (RFC 6749 2.3.1)."""
    header = request.headers.get("Authorization", "")
    secret = None
    if header[:6].lower() == "basic ":
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            raise OAuthError("invalid_client", status=401)
        client_id, _sep, secret = decoded.partition(":")
        client_id, secret = unquote(client_id), unquote(secret)
    else:
        client_id = request.POST.get("client_id")
        secret = request.POST.get("client_secret")
    try:
        client = get_client(client_id, request)
    except ClientError:
        raise OAuthError("invalid_client", status=401)
    if not client.is_public and not (secret and client.check_secret(secret)):
        raise OAuthError("invalid_client", status=401)
    return client


def s256(verifier):
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class TokenView(CorsView):
    http_method_names = ["post", "options"]

    def post(self, request):
        try:
            client = authenticate_client(request)
            grant_type = request.POST.get("grant_type")
            if grant_type == "authorization_code":
                tokens = self.exchange_code(request, client)
            elif grant_type == "refresh_token":
                tokens = self.refresh(request, client)
            else:
                raise OAuthError("unsupported_grant_type")
        except OAuthError as error:
            return error.response()
        response = JsonResponse(tokens)
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response

    def check_resource(self, request, resource):
        requested = request.POST.get("resource")
        if requested and resources.canonical(requested) != resources.canonical(
            resource
        ):
            raise OAuthError("invalid_target")

    def exchange_code(self, request, client):
        code = (
            AuthorizationCode.objects.select_related("user")
            .filter(code_hash=hash_key(request.POST.get("code", "")))
            .first()
        )
        if code is None or code.client_id != client.pk:
            raise OAuthError("invalid_grant")
        # before claiming: a wrong verifier must not burn the code
        verifier = request.POST.get("code_verifier", "")
        if not VERIFIER.fullmatch(verifier) or not secrets.compare_digest(
            s256(verifier), code.code_challenge
        ):
            raise OAuthError("invalid_grant", "PKCE verification failed.")
        with transaction.atomic():
            claimed = AuthorizationCode.objects.filter(pk=code.pk, used=False).update(
                used=True
            )
        if not claimed:
            # a code used twice: revoke what the first use issued
            if code.grant_id:
                Grant.objects.filter(pk=code.grant_id).delete()
            raise OAuthError("invalid_grant")
        if code.expires <= timezone.now():
            raise OAuthError("invalid_grant")
        # required exactly when the authorization request had one
        if request.POST.get("redirect_uri", "") != code.redirect_uri:
            raise OAuthError("invalid_grant")
        self.check_resource(request, code.resource)
        if not code.user.is_active:
            raise OAuthError("invalid_grant")
        # locked, so cleanup cannot delete a token-less grant mid-exchange
        with transaction.atomic():
            grant, _created = Grant.objects.select_for_update().get_or_create(
                user=code.user, client=client, resource=code.resource
            )
            AuthorizationCode.objects.filter(pk=code.pk).update(grant=grant)
            return self.issue(grant, client)

    def refresh(self, request, client):
        token = (
            RefreshToken.objects.select_related("grant__user")
            .filter(key_hash=hash_key(request.POST.get("refresh_token", "")))
            .first()
        )
        if token is None or token.grant.client_id != client.pk:
            raise OAuthError("invalid_grant")
        now = timezone.now()
        claimed = RefreshToken.objects.filter(pk=token.pk, used__isnull=True).update(
            used=now
        )
        if not claimed:
            token.refresh_from_db(fields=["used"])
            retry = timedelta(seconds=conf.get("REFRESH_REUSE_INTERVAL"))
            if token.used is None or now - token.used > retry:
                # a rotated token came back: it may be stolen, so disconnect
                token.grant.delete()
                raise OAuthError("invalid_grant")
            # a client retrying a lost response gets a new pair again
        if token.expires <= now:
            raise OAuthError("invalid_grant")
        self.check_resource(request, token.grant.resource)
        if not token.grant.user.is_active:
            raise OAuthError("invalid_grant")
        with transaction.atomic():
            grant = Grant.objects.select_for_update().filter(pk=token.grant_id).first()
            if grant is None:  # disconnected meanwhile
                raise OAuthError("invalid_grant")
            return self.issue(grant, client)

    def issue(self, grant, client):
        access, access_hash = new_key(ACCESS_PREFIX)
        refresh, refresh_hash = new_key(REFRESH_PREFIX)
        lifetime = conf.get("ACCESS_TOKEN_LIFETIME")
        AccessToken.objects.create(
            grant=grant, key_hash=access_hash, expires=expiry(lifetime)
        )
        RefreshToken.objects.create(
            grant=grant,
            key_hash=refresh_hash,
            expires=expiry(conf.get("REFRESH_TOKEN_LIFETIME")),
        )
        now = timezone.now()
        Grant.objects.filter(pk=grant.pk).update(last_used=now)
        Client.objects.filter(pk=client.pk).update(last_used=now)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": lifetime,
            "refresh_token": refresh,
        }


class RegisterView(CorsView):
    """Dynamic client registration (RFC 7591), open to anyone but rate
    limited per IP; ``mcp_oauth_cleanup`` deletes clients never used."""

    http_method_names = ["post", "options"]
    max_body = 10_000

    def post(self, request):
        if not conf.get("DYNAMIC_REGISTRATION"):
            raise Http404
        if throttled("register", client_ip(request), conf.get("REGISTRATION_RATE")):
            return OAuthError("too_many_requests", status=429).response()
        try:
            if len(request.body) > self.max_body:
                raise ValueError
            data = json.loads(request.body)
            client, secret = register_client(data)
        except ValueError:
            return OAuthError("invalid_client_metadata").response()
        except ClientError as error:
            return OAuthError(error.code, str(error)).response()
        body = {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created.timestamp()),
            "client_name": client.name,
            "redirect_uris": client.redirect_uris,
            "grant_types": data.get("grant_types", ["authorization_code"]),
            "response_types": ["code"],
            "token_endpoint_auth_method": client.auth_method,
        }
        if secret is not None:
            body["client_secret"] = secret
            body["client_secret_expires_at"] = 0
        response = JsonResponse(body, status=201)
        response["Cache-Control"] = "no-store"
        return response


class RevokeView(CorsView):
    """RFC 7009: a refresh token disconnects its grant, an access token is
    revoked alone; an unknown token is not an error."""

    http_method_names = ["post", "options"]

    def post(self, request):
        try:
            client = authenticate_client(request)
        except OAuthError as error:
            return error.response()
        key = request.POST.get("token", "")
        key_hash = hash_key(key)
        if key.startswith(REFRESH_PREFIX):
            Grant.objects.filter(
                client=client, refresh_tokens__key_hash=key_hash
            ).delete()
        elif key.startswith(ACCESS_PREFIX):
            AccessToken.objects.filter(grant__client=client, key_hash=key_hash).delete()
        return HttpResponse(status=200)


@method_decorator([login_required, never_cache], name="dispatch")
class ConnectionsView(View):
    """The user's connected clients, each with "Disconnect"."""

    template_name = "xprez_mcp/oauth/connections.html"

    def get(self, request):
        return render(
            request,
            self.template_name,
            {"connections": connections.for_user(request.user)},
        )

    def post(self, request):
        connections.disconnect(request.user, request.POST.get("grant"))
        return redirect(request.get_full_path())
