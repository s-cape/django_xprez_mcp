from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from xprez_mcp import AdminProvider, MCPServer, ToolError
from xprez_mcp.context import CallContext
from xprez_mcp.errors import InvalidArguments

from .test_models import ModelTestCase
from .testapp.admin import ClientAdmin, site
from .testapp.admin_servers import admin_mcp
from .testapp.models import Client, ColoredTag, Contact, Industry, Swatch, Tag


def grant(user, *codenames):
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))


class AdminTestCase(ModelTestCase):
    url = "/admin/mcp/"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.viewer = User.objects.create_user("viewer", is_staff=True)
        grant(cls.viewer, "view_client", "view_contact")
        cls.editor = User.objects.create_user("editor", is_staff=True)
        grant(
            cls.editor,
            "view_client",
            "add_client",
            "change_client",
            "delete_client",
            "add_contact",
            "change_contact",
            "view_industry",
        )
        cls.root = User.objects.create_superuser("root")
        cls.anna = Contact.objects.create(client=cls.acme, name="Anna", primary=True)
        cls.ben = Contact.objects.create(client=cls.acme, name="Ben")


class AdminToolsTests(AdminTestCase):
    def test_closed_to_non_staff(self):
        self.assertEqual(set(self.tools()), set())  # alice is not staff

    def test_viewer_tools(self):
        names = set(self.tools(token="token-viewer"))
        # export has no permissions, so the admin offers it to viewers too
        self.assertEqual(
            names,
            {"clients_list", "clients_get", "clients_action", "clients_contacts_list"},
        )
        error = self.failed(
            "clients_action",
            {"action": "deactivate", "ids": [self.acme.pk]},
            token="token-viewer",
        )
        self.assertIn("Available: ['export']", error)

    def test_editor_tools(self):
        names = set(self.tools(token="token-editor"))
        self.assertTrue(
            {
                "clients_create",
                "clients_update",
                "clients_delete",
                "clients_action",
                "clients_contacts_create",
                "clients_contacts_update",
            }
            <= names
        )
        # no delete_contact permission
        self.assertNotIn("clients_contacts_delete", names)
        # industries come through the generic tools, tags are not viewable
        self.assertIn("models_list", names)
        schema = self.tools(token="token-editor")["record_get"]["inputSchema"]
        self.assertEqual(schema["properties"]["model"]["enum"], ["industries"])

    def test_superuser_sees_everything_generic(self):
        tools = self.tools(token="token-root")
        enum = tools["record_get"]["inputSchema"]["properties"]["model"]["enum"]
        self.assertEqual(enum, ["industries", "tags"])  # clients have own tools

    def test_list_fields_and_admin_queryset(self):
        data = self.structured("clients_list", token="token-viewer")
        # ordering = ["slug"]; inactive clients are hidden from non-superusers
        self.assertEqual([row["slug"] for row in data["results"]], ["acme", "bistro"])
        self.assertEqual(
            data["results"][0],
            {
                "id": self.acme.pk,
                "name": "Acme",
                "slug": "acme",
                "industry": self.tech.pk,
                "size": "l",
                "upper_name": "ACME",
            },
        )
        data = self.structured("clients_list", token="token-root")
        self.assertEqual(data["count"], 4)

    def test_search_uses_admin(self):
        data = self.structured("clients_list", {"query": "acme"}, token="token-viewer")
        self.assertEqual([row["slug"] for row in data["results"]], ["acme"])

    def test_list_filters(self):
        schema = self.tools(token="token-viewer")["clients_list"]["inputSchema"]
        self.assertTrue(
            {"size", "industry", "founded__gte", "founded__lte", "owned"}
            <= set(schema["properties"])
        )
        rows = lambda **args: [  # noqa: E731
            row["slug"]
            for row in self.structured("clients_list", args, token="token-root")[
                "results"
            ]
        ]
        self.assertEqual(rows(size="l"), ["acme"])
        self.assertEqual(rows(industry=self.food.pk), ["bistro"])
        self.assertEqual(rows(founded__gte="2000-01-01"), ["acme"])
        self.assertEqual(rows(owned="yes"), ["bistro", "mine"])
        error = self.failed("clients_list", {"owned": "maybe"}, token="token-root")
        self.assertIn("owned must be one of ['yes', 'no']", error)

    def test_get_with_inlines(self):
        data = self.structured(
            "clients_get", {"id": self.acme.pk}, token="token-viewer"
        )
        self.assertEqual(data["active"], True)
        self.assertEqual(data["tags"], [self.vip.pk])
        self.assertEqual(
            data["inlines"]["contacts"],
            {
                "results": [
                    {
                        "id": self.anna.pk,
                        "name": "Anna",
                        "email": "",
                        "primary": True,
                    },
                    {"id": self.ben.pk, "name": "Ben", "email": "", "primary": False},
                ],
                "count": 2,
            },
        )

    def test_hidden_row_not_found(self):
        error = self.failed("clients_get", {"id": self.hidden.pk}, token="token-viewer")
        self.assertIn("No client", error)


class AdminWriteTests(AdminTestCase):
    def test_create_through_save_model(self):
        data = self.structured(
            "clients_create", {"slug": "new", "name": "New"}, token="token-editor"
        )
        client = Client.objects.get(slug="new")
        self.assertEqual(client.owner, self.editor)  # set by save_model
        self.assertEqual(data["messages"], [{"level": "info", "message": "Saved New."}])
        entry = LogEntry.objects.get(object_id=str(client.pk))
        self.assertEqual(entry.action_flag, ADDITION)
        self.assertEqual(entry.user, self.editor)

    def test_create_validates_with_admin_form(self):
        error = self.failed(
            "clients_create", {"slug": "acme", "name": "Dup"}, token="token-editor"
        )
        self.assertIn("slug", error)

    def test_readonly_fields_per_object(self):
        schema = self.tools(token="token-editor")["clients_update"]["inputSchema"]
        self.assertNotIn("active", schema["properties"])  # readonly_fields
        error = self.failed(
            "clients_update", {"id": self.acme.pk, "slug": "x"}, token="token-editor"
        )
        self.assertIn("You cannot set slug", error)
        self.structured(
            "clients_update", {"id": self.acme.pk, "slug": "x"}, token="token-root"
        )

    def test_update_logs_change(self):
        self.structured(
            "clients_update",
            {"id": self.acme.pk, "name": "Acme Corp"},
            token="token-editor",
        )
        self.acme.refresh_from_db()
        self.assertEqual(self.acme.name, "Acme Corp")
        entry = LogEntry.objects.get(action_flag=CHANGE)
        self.assertIn("Name", entry.get_change_message())

    def test_object_permission(self):
        # bistro belongs to bob; the admin refuses changes by others
        error = self.failed(
            "clients_update",
            {"id": self.bistro.pk, "name": "Mine"},
            token="token-editor",
        )
        self.assertIn("You cannot update this client", error)

    def test_delete_logs(self):
        self.structured("clients_delete", {"id": self.acme.pk}, token="token-editor")
        self.assertFalse(Client.objects.filter(pk=self.acme.pk).exists())
        self.assertTrue(LogEntry.objects.filter(action_flag=DELETION).exists())

    def test_delete_needs_permission_for_registered_related(self):
        site.register(Contact)  # get_deleted_objects checks registered models
        try:
            error = self.failed(
                "clients_delete", {"id": self.acme.pk}, token="token-editor"
            )
        finally:
            site.unregister(Contact)
            admin_mcp.clear_cache()  # register_admin may have seen Contact
        self.assertIn("would also delete contact", error)
        self.assertTrue(Client.objects.filter(pk=self.acme.pk).exists())

    def test_admin_action(self):
        data = self.structured(
            "clients_action",
            {"action": "deactivate", "ids": [self.acme.pk]},
            token="token-editor",
        )
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["messages"][0]["message"], "1 deactivated.")
        self.acme.refresh_from_db()
        self.assertFalse(self.acme.active)

    def test_admin_action_returning_a_page_rolls_back(self):
        error = self.failed(
            "clients_action",
            {"action": "export", "ids": [self.acme.pk]},
            token="token-editor",
        )
        self.assertIn("needs the admin's own page", error)
        self.acme.refresh_from_db()
        self.assertEqual(self.acme.group, "Acme Group")

    def test_admin_action_description(self):
        description = self.tools(token="token-editor")["clients_action"]["description"]
        self.assertIn("deactivate: Deactivate selected clients", description)


class InlineTests(AdminTestCase):
    def test_list(self):
        data = self.structured(
            "clients_contacts_list",
            {"parent": self.acme.pk, "limit": 1},
            token="token-viewer",
        )
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["results"][0]["name"], "Anna")
        self.assertIsNotNone(data["next_cursor"])

    def test_create(self):
        data = self.structured(
            "clients_contacts_create",
            {"parent": self.acme.pk, "name": "Cecil", "email": "c@example.com"},
            token="token-editor",
        )
        contact = Contact.objects.get(name="Cecil")
        self.assertEqual(contact.client, self.acme)
        self.assertEqual(data["id"], contact.pk)
        entry = LogEntry.objects.get(action_flag=CHANGE, object_id=str(self.acme.pk))
        self.assertIn("Cecil", entry.get_change_message())

    def test_update(self):
        self.structured(
            "clients_contacts_update",
            {"parent": self.acme.pk, "row": self.ben.pk, "email": "ben@example.com"},
            token="token-editor",
        )
        self.ben.refresh_from_db()
        self.assertEqual(self.ben.email, "ben@example.com")
        self.anna.refresh_from_db()
        self.assertTrue(self.anna.primary)  # other rows untouched

    def test_formset_validation(self):
        error = self.failed(
            "clients_contacts_update",
            {"parent": self.acme.pk, "row": self.ben.pk, "primary": True},
            token="token-editor",
        )
        self.assertIn("Only one contact can be primary.", error)
        error = self.failed(
            "clients_contacts_create",
            {"parent": self.acme.pk, "name": "Dora", "email": "nope"},
            token="token-editor",
        )
        self.assertIn("email", error)

    def test_delete(self):
        self.structured(
            "clients_contacts_delete",
            {"parent": self.acme.pk, "row": self.ben.pk},
            token="token-root",
        )
        self.assertFalse(Contact.objects.filter(pk=self.ben.pk).exists())

    def test_wrong_parent(self):
        error = self.failed(
            "clients_contacts_update",
            {"parent": self.hidden.pk, "row": self.ben.pk, "name": "x"},
            token="token-root",
        )
        self.assertIn("No contacts row", error)


class GenericAdminTests(AdminTestCase):
    url = "/admin-generic/mcp/"

    def test_models_list(self):
        data = self.structured("models_list", token="token-editor")
        clients = next(m for m in data["models"] if m["model"] == "clients")
        self.assertEqual(clients["actions"], ["list", "get", "update"])
        self.assertEqual(
            clients["inlines"]["contacts"]["fields"], ["name", "email", "primary"]
        )
        self.assertIn("email", clients["inlines"]["contacts"]["writable"])

    def test_inline_tools(self):
        data = self.structured(
            "record_inline_list",
            {"model": "clients", "parent": self.acme.pk, "inline": "contacts"},
            token="token-editor",
        )
        self.assertEqual(data["count"], 2)
        self.structured(
            "record_inline_update",
            {
                "model": "clients",
                "parent": self.acme.pk,
                "inline": "contacts",
                "row": self.ben.pk,
                "data": {"name": "Benjamin"},
            },
            token="token-editor",
        )
        self.ben.refresh_from_db()
        self.assertEqual(self.ben.name, "Benjamin")
        error = self.failed(
            "record_inline_create",
            {
                "model": "clients",
                "parent": self.acme.pk,
                "inline": "contacts",
                "data": {"nope": 1},
            },
            token="token-editor",
        )
        self.assertIn("Unknown argument", error)

    def test_no_create_without_opt_in(self):
        self.assertNotIn("record_create", self.tools(token="token-root"))


class AdminConfigurationTests(SimpleTestCase):
    def test_permissions_not_supported(self):
        class Bad(AdminProvider):
            admin_site = site
            permissions = {"list": "staff"}

        with self.assertRaisesMessage(ImproperlyConfigured, "admin's permissions"):
            Bad(MCPServer(), model=Client)

    def test_not_registered(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "is not registered"):
            AdminProvider(MCPServer(), model=Client)  # not on admin.site

    def test_delete_selected_refused(self):
        class Bad(AdminProvider):
            admin_site = site
            admin_actions = ["delete_selected"]

        with self.assertRaisesMessage(ImproperlyConfigured, "never exposed"):
            Bad(MCPServer(), model=Client)

    def test_register_admin_exclude(self):
        server = MCPServer(exposure="generic")
        server.register_admin(site, exclude=[Tag, "testapp.client"])
        models = {provider.model for provider in server.get_providers()}
        self.assertEqual(models, {Industry})

    def test_register_admin_name_template(self):
        server = MCPServer(exposure="generic")
        server.register_admin(site, name_template="{app_label}_{plural}")
        names = {provider.get_name() for provider in server.get_providers()}
        self.assertEqual(
            names, {"testapp_clients", "testapp_industries", "testapp_tags"}
        )

    def test_explicit_registration_wins(self):
        server = MCPServer()
        server.register_admin(site)
        provider = server.get_providers()[0]
        server.register(Industry, type(provider))
        models = [provider.model for provider in server.get_providers()]
        self.assertEqual(models.count(Industry), 1)
        self.assertIn(Client, models)


class HiddenFieldsTests(TestCase):
    def provider(self, **attrs):
        local_site = admin.AdminSite(name="users")
        local_site.register(User, UserAdmin)
        attrs = {
            "admin_site": local_site,
            "actions": ["list", "get", "update"],
            **attrs,
        }
        server = MCPServer(name="users")
        return server, type("P", (AdminProvider,), attrs)(server, model=User)

    def test_masked_password_left_out(self):
        server, provider = self.provider()
        root = User.objects.create_superuser("hidden-root", password="secret")
        data = provider.serialize(
            CallContext(server=server, user=root), root, detail=True
        )
        self.assertEqual(data["username"], "hidden-root")
        self.assertNotIn("password", data)
        self.assertNotIn("password", provider.get_write_fields())

    def test_hidden_fields(self):
        server, provider = self.provider(hidden_fields=["email"])
        root = User.objects.create_superuser("hidden-root", email="root@example.com")
        ctx = CallContext(server=server, user=root)
        self.assertNotIn("email", provider.serialize(ctx, root, detail=True))
        rows = provider.serialize(ctx, User.objects.filter(pk=root.pk), many=True)
        self.assertNotIn("email", rows[0])
        self.assertNotIn("email", provider.get_write_fields())

    def test_hidden_fields_not_matched(self):
        server, provider = self.provider(hidden_fields=["email", "is_staff"])
        root = User.objects.create_superuser("hidden-root", email="root@example.com")
        ctx = CallContext(server=server, user=root)
        self.assertEqual(provider.run_list(ctx, query="example.com")["count"], 0)
        self.assertEqual(provider.run_list(ctx, query="hidden-root")["count"], 1)
        self.assertNotIn("email", provider.search_fields)
        self.assertNotIn("is_staff", provider.get_filters())
        self.assertIn("is_active", provider.get_filters())

    def test_unexposed_admin_refused(self):
        from xprez_mcp.contrib.tokens.admin import TokenAdmin
        from xprez_mcp.contrib.tokens.models import Token

        local_site = admin.AdminSite(name="tokens")
        local_site.register(Token, TokenAdmin)
        attrs = {"admin_site": local_site}
        with self.assertRaisesMessage(ImproperlyConfigured, "mcp_exposed"):
            type("P", (AdminProvider,), attrs)(MCPServer(name="t"), model=Token)


class AdminVisibilityTests(AdminTestCase):
    def test_hidden_fields_on_inlines(self):
        local_site = admin.AdminSite(name="hidden-inlines")
        local_site.register(Client, ClientAdmin)
        attrs = {
            "admin_site": local_site,
            "actions": ["list", "get", "update"],
            "hidden_fields": ["email"],
        }
        server = MCPServer(name="hidden-inlines")
        provider = type("P", (AdminProvider,), attrs)(server, model=Client)
        ctx = CallContext(server=server, user=self.root)
        data = provider.serialize(ctx, self.acme, detail=True)
        self.assertNotIn("email", data["inlines"]["contacts"]["results"][0])
        listed = provider.run_inline_list(ctx, "contacts", self.acme.pk)
        self.assertNotIn("email", listed["results"][0])
        names = [param[0] for param in provider.inline_parameters("contacts", "update")]
        self.assertNotIn("email", names)
        self.assertNotIn(
            "email", provider.describe(ctx)["inlines"]["contacts"]["fields"]
        )
        with self.assertRaisesMessage(ToolError, "You cannot set email."):
            provider.run_inline(
                ctx, "update", "contacts", self.acme.pk, self.ben.pk, email="x@y.z"
            )

    def test_inline_data_stays_off_the_shared_request(self):
        server = MCPServer(name="inline-request")
        provider = type("P", (AdminProvider,), {"admin_site": site})(
            server, model=Client
        )
        ctx = CallContext(server=server, user=self.root)
        shared = provider.admin_request(ctx)
        provider.run_inline(
            ctx, "update", "contacts", self.acme.pk, self.ben.pk, name="B"
        )
        self.assertEqual(dict(shared.POST), {})
        self.assertIs(provider.admin_request(ctx, write=True), shared)
        self.assertEqual(dict(shared.POST), {})

    def test_list_display_per_user(self):
        class PerUserAdmin(ClientAdmin):
            def get_list_display(self, request):
                if request.user.is_superuser:
                    return [*self.list_display, secret]
                return ["name"]

        def secret(obj):
            return f"secret-{obj.slug}"

        local_site = admin.AdminSite(name="per-user")
        local_site.register(Client, PerUserAdmin)
        server = MCPServer(name="per-user")
        provider = type("P", (AdminProvider,), {"admin_site": local_site})(
            server, model=Client
        )
        queryset = Client.objects.filter(pk=self.acme.pk)
        viewer = CallContext(server=server, user=self.viewer)
        self.assertEqual(
            provider.serialize(viewer, queryset, many=True),
            [{"id": self.acme.pk, "name": "Acme"}],
        )
        self.assertEqual(provider.describe(viewer)["fields"], ["id", "name"])
        root = CallContext(server=server, user=self.root)
        row = provider.serialize(root, queryset, many=True)[0]
        self.assertEqual(row["upper_name"], "ACME")
        self.assertEqual(row["secret"], "secret-acme")

    def test_list_filter_per_user(self):
        class PerUserAdmin(ClientAdmin):
            def get_list_filter(self, request):
                if request.user.is_superuser:
                    return self.list_filter
                return ["size"]

        local_site = admin.AdminSite(name="per-user-filter")
        local_site.register(Client, PerUserAdmin)
        server = MCPServer(name="per-user-filter")
        provider = type("P", (AdminProvider,), {"admin_site": local_site})(
            server, model=Client
        )
        viewer = CallContext(server=server, user=self.viewer)
        self.assertEqual(provider.run_list(viewer, size="l")["count"], 1)
        with self.assertRaisesMessage(InvalidArguments, "cannot filter by industry"):
            provider.run_list(viewer, industry=self.tech.pk)
        root = CallContext(server=server, user=self.root)
        self.assertEqual(provider.run_list(root, industry=self.tech.pk)["count"], 1)


class InlineOfInheritedParentTests(TestCase):
    """A multi-table child (pk tag_ptr) with grouped fields and an inline."""

    def test_get_includes_inline_rows(self):
        class SwatchInline(admin.TabularInline):
            model = Swatch
            fields = [("hex", "source")]  # grouped, as on one admin row
            readonly_fields = ["source"]

            def source(self, obj):  # the admin shows its empty value
                raise Tag.DoesNotExist

        class ColoredTagAdmin(admin.ModelAdmin):
            fields = [("name", "color")]
            list_display = ["name", "broken"]
            inlines = [SwatchInline]

            def broken(self, obj):
                raise Tag.DoesNotExist

        local_site = admin.AdminSite(name="inherited")
        local_site.register(ColoredTag, ColoredTagAdmin)
        provider_class = type("P", (AdminProvider,), {"admin_site": local_site})
        server = MCPServer(name="inherited")
        provider = provider_class(server, model=ColoredTag)
        tag = ColoredTag.objects.create(name="red", color="#f00")
        swatch = Swatch.objects.create(tag=tag, hex="#f00")
        root = User.objects.create_superuser("inherited-root")
        ctx = CallContext(server=server, user=root)
        data = provider.serialize(ctx, ColoredTag.objects.get(pk=tag.pk), detail=True)
        self.assertEqual(data["tag_ptr"], tag.pk)
        self.assertEqual(data["name"], "red")
        self.assertEqual(data["color"], "#f00")  # a Hue, sent as its string
        self.assertEqual(
            data["inlines"]["swatches"],
            {"results": [{"id": swatch.pk, "hex": "#f00", "source": None}], "count": 1},
        )
        described = provider.describe(ctx)
        self.assertEqual(described["lookup"], "tag_ptr")
        self.assertEqual(described["inlines"]["swatches"]["fields"], ["hex", "source"])
        rows = provider.serialize(ctx, ColoredTag.objects.all(), many=True)
        self.assertIsNone(rows[0]["broken"])
