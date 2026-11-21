from django.contrib import messages
from django.contrib.admin import AdminSite
from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpRequest, QueryDict
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils.datastructures import MultiValueDict

from xprez_mcp import AdminProvider, MCPServer
from xprez_mcp.admin_request import (
    build_admin_request,
    collected_messages,
    get_admin_request,
    with_messages,
)
from xprez_mcp.context import CallContext

from .testapp.admin import site
from .testapp.models import Client


class BuildAdminRequestTests(SimpleTestCase):
    def test_copy_of_the_mcp_request(self):
        mcp_request = RequestFactory().post("/mcp/?page=2", {"a": "1"})
        user = User(username="anna")
        ctx = CallContext(server=None, request=mcp_request, user=user)
        request = build_admin_request(ctx, site)
        self.assertIsNot(request, mcp_request)
        self.assertEqual(request.path, "/mcp/")
        self.assertEqual(dict(request.GET), {})
        self.assertEqual(dict(request.POST), {})
        self.assertEqual(dict(request.FILES), {})
        self.assertIs(request.user, user)
        self.assertEqual(request.current_app, site.name)
        self.assertEqual(mcp_request.POST["a"], "1")  # the original is untouched

    def test_without_request_or_user(self):
        request = build_admin_request(CallContext(server=None), site)
        self.assertIsInstance(request, HttpRequest)
        self.assertIsInstance(request.user, AnonymousUser)


class GetAdminRequestTests(SimpleTestCase):
    def setUp(self):
        self.ctx = CallContext(server=None, request=RequestFactory().get("/mcp/"))

    def test_one_per_call_and_site(self):
        other = AdminSite(name="other")
        first = get_admin_request(self.ctx, site)
        self.assertIs(get_admin_request(self.ctx, site, write=True), first)
        self.assertIsNot(get_admin_request(self.ctx, other), first)
        self.assertIsNot(
            get_admin_request(CallContext(server=None), site), first
        )  # another call

    def test_method(self):
        self.assertEqual(get_admin_request(self.ctx, site).method, "GET")
        self.assertEqual(get_admin_request(self.ctx, site, write=True).method, "POST")

    def test_data_for_one_use(self):
        data = QueryDict("title=New")
        files = MultiValueDict({"file": ["f"]})
        request = get_admin_request(self.ctx, site, write=True, data=data, files=files)
        self.assertEqual(request.POST["title"], "New")
        self.assertEqual(request.FILES["file"], "f")
        request = get_admin_request(self.ctx, site)
        self.assertEqual(dict(request.POST), {})
        self.assertEqual(dict(request.FILES), {})

    def test_data_does_not_reach_other_users(self):
        shared = get_admin_request(self.ctx, site)
        with_data = get_admin_request(self.ctx, site, write=True, data={"a": "1"})
        self.assertIsNot(with_data, shared)
        self.assertEqual(dict(shared.POST), {})
        get_admin_request(self.ctx, site)
        self.assertEqual(with_data.POST["a"], "1")
        self.assertEqual(with_data.method, "POST")
        messages.info(with_data, "Saved.")
        self.assertEqual(len(collected_messages(self.ctx, site)), 1)

    def test_plain_mapping_data(self):
        data = {"tags": ["1", "2"], "title": "New"}
        request = get_admin_request(self.ctx, site, write=True, data=data)
        self.assertEqual(request.POST.getlist("tags"), ["1", "2"])
        self.assertEqual(request.POST["title"], "New")

    def test_factory_data_kept(self):
        def factory(ctx):
            request = build_admin_request(ctx, site)
            request.POST = QueryDict("kept=1")
            return request

        get_admin_request(self.ctx, site, factory=factory)
        get_admin_request(self.ctx, site, data=QueryDict("a=1"))
        self.assertEqual(get_admin_request(self.ctx, site).POST["kept"], "1")

    def test_factory_after_default(self):
        def factory(ctx):
            request = build_admin_request(ctx, site)
            request.tenant = "t"
            return request

        messages.info(get_admin_request(self.ctx, site), "First.")
        request = get_admin_request(self.ctx, site, factory=factory)
        self.assertEqual(request.tenant, "t")
        self.assertIs(get_admin_request(self.ctx, site), request)
        self.assertIs(get_admin_request(self.ctx, site, factory=factory), request)
        messages.info(request, "Second.")
        self.assertEqual(len(collected_messages(self.ctx, site)), 2)


class MessagesTests(SimpleTestCase):
    def test_collected(self):
        ctx = CallContext(server=None, request=RequestFactory().get("/mcp/"))
        self.assertEqual(collected_messages(ctx, site), [])
        self.assertEqual(with_messages(ctx, site, {"a": 1}), {"a": 1})
        messages.info(get_admin_request(ctx, site), "Saved.")
        expected = [{"level": "info", "message": "Saved."}]
        self.assertEqual(collected_messages(ctx, site), expected)
        self.assertEqual(
            with_messages(ctx, site, {"a": 1}), {"a": 1, "messages": expected}
        )
        self.assertEqual(with_messages(ctx, site, "text"), "text")


class AdminProviderSharesRequestTests(TestCase):
    def test_provider_and_helper_share_the_request(self):
        user = User.objects.create_superuser("root")
        server = MCPServer(name="shared-request")
        ctx = CallContext(server=server, request=RequestFactory().get("/"), user=user)
        provider = type("P", (AdminProvider,), {"admin_site": site})(
            server, model=Client
        )
        request = provider.admin_request(ctx)
        self.assertIs(get_admin_request(ctx, provider.site), request)
        messages.info(request, "From admin code.")
        self.assertEqual(
            provider.with_messages(ctx, {})["messages"][0]["message"],
            "From admin code.",
        )

    def test_provider_override_wins_over_helper(self):
        class P(AdminProvider):
            admin_site = site

            def get_admin_request(self, ctx):
                request = super().get_admin_request(ctx)
                request.tenant = "t"
                return request

        server = MCPServer(name="override-request")
        ctx = CallContext(server=server, request=RequestFactory().get("/"))
        get_admin_request(ctx, site)
        self.assertEqual(P(server, model=Client).admin_request(ctx).tenant, "t")
