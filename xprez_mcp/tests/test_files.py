import base64
import http.server
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django import forms
from django.db import models
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from django.test.utils import isolate_apps
from django.utils.datastructures import MultiValueDict

from xprez_mcp import MCPServer, ModelProvider
from xprez_mcp.core import files
from xprez_mcp.core.context import CallContext
from xprez_mcp.core.files import FileValue, file_argument
from xprez_mcp.core.schema import annotation_schema, coerce
from xprez_mcp.errors import InvalidArguments, ToolError
from xprez_mcp.providers.fields import upload_prefix, write_parameter

from .test_admin import grant
from .test_models import rest_framework
from .test_protocol import MCPTestCase
from .testapp.file_servers import (
    DocumentAdmin,
    DocumentAdminProvider,
    DocumentProvider,
)
from .testapp.models import Document, Page


def b64(content):
    return base64.b64encode(content).decode()


class FileArgumentTests(SimpleTestCase):
    def test_schema(self):
        schema = annotation_schema(file_argument(("data", "path"), False))
        self.assertEqual(
            [shape["required"] for shape in schema["anyOf"]],
            [["data", "name"], ["path"]],
        )
        self.assertIn("{path} uses a file already in storage", schema["description"])
        nullable = annotation_schema(file_argument(("data",), True))
        self.assertEqual(nullable["anyOf"][-1], {"type": "null"})
        self.assertIn("null clears it", nullable["description"])

    def test_public_addresses(self):
        cases = {
            "8.8.8.8": True,
            "2606:4700:4700::1111": True,
            "64:ff9b::808:808": True,  # NAT64 of 8.8.8.8
            "127.0.0.1": False,
            "10.0.0.1": False,
            "::1": False,
            "::ffff:127.0.0.1": False,  # IPv4-mapped
            "64:ff9b::a9fe:a9fe": False,  # NAT64 of 169.254.169.254
            "64:ff9b:1::a00:1": False,  # local-use NAT64
            "2002:7f00:1::1": False,  # 6to4 of 127.0.0.1
            "2001:0:4136:e378:8000:63bf:f5ff:fffe": False,  # Teredo, client 10.0.0.1
            "fe80::1%eth0": False,
            "::7f00:1": False,  # IPv4-compatible 127.0.0.1
            "::ffff:0:7f00:1": False,  # IPv4-translated 127.0.0.1
            "::808:808": True,  # IPv4-compatible 8.8.8.8
            "::": False,
            "not an address": False,
        }
        for address, public in cases.items():
            with self.subTest(address=address):
                self.assertIs(files.is_public_address(address), public)

    def test_shorten_name(self):
        self.assertEqual(files.shorten_name("abcdef.png", 8), "abcd.png")
        self.assertEqual(files.shorten_name("abc.png", 8), "abc.png")
        self.assertEqual(files.shorten_name("abcdef.toolong", 4), "abcd")
        self.assertEqual(files.shorten_name("abc", None), "abc")

    def test_path_is_opt_in(self):
        self.assertEqual(ModelProvider.file_sources, ("data",))
        provider_class = type(
            "Provider",
            (ModelProvider,),
            {"fields": ["id", "file"], "actions": ["update"]},
        )
        provider = provider_class(MCPServer(name="default-sources"), model=Document)
        params = {name: ann for name, ann, _ in provider.write_parameters(True)}
        self.assertEqual(params["file"].sources, ("data",))
        with self.assertRaisesMessage(InvalidArguments, "path is not accepted here"):
            coerce(params["file"], {"path": "docs/report.txt"}, "file")

    def test_declared_file_field_needs_a_source(self):
        class Provider(DocumentProvider):
            write_fields = ["title", "file"]
            file_sources = []

        with self.assertRaisesMessage(ImproperlyConfigured, "file_sources is empty"):
            Provider(MCPServer(name="declared"), model=Document)

    def test_fetch_finished_before_the_deadline(self):
        class LateTimer:
            """Fires only when cancelled, after the fetch has finished."""

            def __init__(self, interval, function):
                self.function = function

            def start(self):
                pass

            def cancel(self):
                self.function()

        result = (b"x", "x.txt", "text/plain")
        with mock.patch.object(files.threading, "Timer", LateTimer):
            with mock.patch.object(files, "_fetch", return_value=result):
                self.assertEqual(files.fetch("https://example.com/x.txt"), result)

    def test_sources_as_a_list(self):
        self.assertIs(file_argument(["data", "url"]), file_argument(("data", "url")))
        with self.assertRaises(ValueError):
            file_argument([])

    @isolate_apps("xprez_mcp.tests.testapp")
    def test_null_follows_blank(self):
        class Upload(models.Model):
            required = models.FileField(null=True)  # a form keeps the file
            optional = models.FileField(blank=True)

        opts = Upload._meta
        self.assertFalse(write_parameter(opts, "required", True)[1].nullable)
        self.assertTrue(write_parameter(opts, "optional", True)[1].nullable)

    def test_empty_file_sources_makes_file_fields_read_only(self):
        class Provider(DocumentProvider):
            file_sources = []

        provider = Provider(MCPServer(name="no-sources"), model=Document)
        names = [name for name, _, _ in provider.write_parameters(partial=True)]
        self.assertEqual(names, ["title"])

    @isolate_apps("xprez_mcp.tests.testapp")
    def test_upload_prefix(self):
        def upload_to(instance, name):
            return name

        class Upload(models.Model):
            folder = models.FileField(upload_to="docs")
            dated = models.FileField(upload_to="images/%Y/%m/")
            joined = models.FileField(upload_to="img_%Y")
            root = models.FileField()
            computed = models.FileField(upload_to=upload_to)

        prefixes = {
            field.name: upload_prefix(field)
            for field in Upload._meta.fields
            if isinstance(field, models.FileField)
        }
        self.assertEqual(
            prefixes,
            {
                "folder": "docs/",
                "dated": "images/",
                "joined": "img_",
                "root": None,
                "computed": None,
            },
        )

    def test_coerce(self):
        argument = file_argument(("data", "path", "url"), True)
        self.assertEqual(
            coerce(argument, {"data": b64(b"hi"), "name": "a/b\\hero.jpg"}, "f"),
            FileValue("data", b"hi", "hero.jpg"),
        )
        data_url = "data:text/plain;base64," + b64(b"hi")
        self.assertEqual(
            coerce(argument, {"data": data_url, "name": "x.txt"}, "f").value, b"hi"
        )
        self.assertEqual(
            coerce(argument, {"path": "images/2026/hero.jpg"}, "f"),
            FileValue("path", "images/2026/hero.jpg"),
        )
        self.assertEqual(
            coerce(argument, {"url": "https://cdn.example.com/a.jpg"}, "f").source,
            "url",
        )
        self.assertIsNone(coerce(argument, None, "f"))

    def test_malformed_url(self):
        argument = file_argument(("url",))
        for url in ("http://[abc/x", "http://[::1/x", "https://"):
            with self.subTest(url=url):
                with self.assertRaisesMessage(InvalidArguments, "http or https URL"):
                    coerce(argument, {"url": url}, "f")

    def test_permission_hint_handles_anonymous(self):
        from xprez_mcp.permissions import validate_permission

        with self.assertRaisesMessage(Exception, "ctx.user is not None and"):
            validate_permission("crm.use_mcp")

    def test_invalid(self):
        argument = file_argument(("data", "path"), False)
        cases = {
            "f is required and cannot be cleared.": None,
            "f must be {data: ...} or {path: ...}.": "hero.jpg",
            "f.data is not valid base64.": {"data": "**", "name": "a"},
            "f.name must be a non-empty string.": {"data": b64(b"x")},
            "f: unknown keys ['name']": {"path": "a", "name": "b"},
            "f: url is not accepted here.": {"url": "https://example.com/a"},
            "f.path must be a relative path in storage.": {"path": "../etc/passwd"},
        }
        for message, value in cases.items():
            with self.subTest(value=value):
                with self.assertRaisesMessage(InvalidArguments, message):
                    coerce(argument, value, "f")
        for path in ("/etc/passwd", "a//b", "a/./b", "a\\b"):
            with self.assertRaises(InvalidArguments):
                coerce(argument, {"path": path}, "f")
        with self.assertRaisesMessage(InvalidArguments, "http or https"):
            coerce(file_argument(("url",)), {"url": "file:///etc/passwd"}, "f")


class FilesTestCase(MCPTestCase):
    url = "/files/mcp/"

    def setUp(self):
        super().setUp()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        settings = override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/")
        settings.enable()
        self.addCleanup(settings.disable)
        self.document = Document.objects.create(
            title="Report",
            file=default_storage.save("docs/report.txt", ContentFile(b"v1")),
            attachment=default_storage.save("attachments/a.txt", ContentFile(b"a")),
        )

    def tools(self, **kwargs):
        return {
            tool["name"]: tool for tool in self.result("tools/list", **kwargs)["tools"]
        }

    def structured(self, name, arguments=None, **kwargs):
        result = self.call(name, arguments, **kwargs)
        self.assertFalse(result["isError"], result)
        return result.get("structuredContent")

    def failed(self, name, arguments=None, **kwargs):
        result = self.call(name, arguments, **kwargs)
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]

    def read(self, field_file):
        field_file.open("rb")
        with field_file:
            return field_file.read()


class ModelProviderFileTests(FilesTestCase):
    def test_schema(self):
        create = self.tools()["documents_create"]["inputSchema"]
        self.assertIn("file", create["required"])
        self.assertNotIn("attachment", create["required"])
        sources = [shape for shape in create["properties"]["file"]["anyOf"]]
        self.assertEqual(len(sources), 2)  # data, and path opted in
        self.assertEqual(
            create["properties"]["attachment"]["anyOf"][-1], {"type": "null"}
        )

    def test_create_with_data(self):
        result = self.structured(
            "documents_create",
            {"title": "New", "file": {"data": b64(b"hello"), "name": "hello.txt"}},
        )
        document = Document.objects.get(pk=result["id"])
        self.assertTrue(document.file.name.startswith("docs/hello"))
        self.assertEqual(result["file"], f"/media/{document.file.name}")
        self.assertEqual(self.read(document.file), b"hello")
        self.assertEqual(document.attachment.name, "")

    def test_create_requires_the_file(self):
        error = self.failed("documents_create", {"title": "New"})
        self.assertIn("Missing required argument: file", error)

    def test_update_with_path(self):
        stored = default_storage.save("docs/other.txt", ContentFile(b"other"))
        self.structured(
            "documents_update", {"id": self.document.pk, "file": {"path": stored}}
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.file.name, stored)  # referenced, not copied
        files_in_docs = sorted(default_storage.listdir("docs")[1])
        self.assertEqual(files_in_docs, ["other.txt", "report.txt"])

    def test_missing_path(self):
        error = self.failed(
            "documents_update", {"id": self.document.pk, "file": {"path": "docs/x"}}
        )
        self.assertIn("no file at 'docs/x'", error)

    def test_path_in_another_fields_nested_folder(self):
        secret = default_storage.save("docs/private/contract.txt", ContentFile(b"s"))
        error = self.failed(
            "documents_update", {"id": self.document.pk, "file": {"path": secret}}
        )
        self.assertIn("is in another file field's folder", error)
        self.document.refresh_from_db()
        self.assertEqual(self.document.file.name, "docs/report.txt")

    def test_path_outside_upload_to(self):
        error = self.failed(
            "documents_update",
            {"id": self.document.pk, "file": {"path": "attachments/a.txt"}},
        )
        self.assertIn("file: path must be under 'docs/'", error)
        self.document.refresh_from_db()
        self.assertEqual(self.document.file.name, "docs/report.txt")

    def provider(self, **attrs):
        provider_class = type("Provider", (DocumentProvider,), attrs)
        provider = provider_class(MCPServer(name="direct"), model=Document)
        return provider, CallContext(server=provider.server)

    def test_path_size_limit(self):
        stored = default_storage.save("docs/other.txt", ContentFile(b"other"))
        provider, ctx = self.provider(max_file_size=3)
        with self.assertRaisesMessage(ToolError, "larger than 3 bytes"):
            provider.validate(ctx, {"file": FileValue("path", stored)}, self.document)

    def test_stored_file_closed_after_validation(self):
        stored = default_storage.save("docs/other.txt", ContentFile(b"other"))

        class Form(forms.ModelForm):
            def clean_file(self):
                file = self.cleaned_data["file"]
                file.read()  # as ImageField validation does
                return file

        provider, ctx = self.provider(form=Form)
        data = provider.validate(
            ctx, {"file": FileValue("path", stored)}, self.document
        )
        self.assertTrue(data["file"].closed)

    def test_overridden_path_rule_message(self):
        def allows_file_path(provider, ctx, field, path):
            return path.startswith("docs/public/")

        stored = default_storage.save("docs/other.txt", ContentFile(b"other"))
        provider, ctx = self.provider(allows_file_path=allows_file_path)
        with self.assertRaisesMessage(
            ToolError, "path 'docs/other.txt' is not allowed"
        ):
            provider.validate(ctx, {"file": FileValue("path", stored)}, self.document)

    def test_clear_the_form_refuses(self):
        class Form(forms.ModelForm):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.fields["attachment"].required = True

        provider, ctx = self.provider(form=Form)
        with self.assertRaisesMessage(ToolError, "attachment is required"):
            provider.validate(ctx, {"attachment": None}, self.document)

    def test_form_gets_a_multi_value_dict(self):
        seen = []

        class Form(forms.ModelForm):
            def __init__(self, *args, files=None, **kwargs):
                seen.append(files)
                super().__init__(*args, files=files, **kwargs)

        provider, ctx = self.provider(form=Form)
        provider.validate(ctx, {"file": FileValue("data", b"x", "x.txt")})
        self.assertIsInstance(seen[0], MultiValueDict)
        self.assertEqual(seen[0].getlist("file")[0].name, "x.txt")

    def test_cleanup_failure_keeps_the_original_error(self):
        def perform_update(provider, ctx, instance, data):
            DocumentProvider.perform_update(provider, ctx, instance, data)
            raise ToolError("Rejected after the save.")

        provider, ctx = self.provider(perform_update=perform_update)
        broken = mock.patch.object(
            FileSystemStorage, "delete", side_effect=OSError("storage down")
        )
        with broken, self.assertLogs("xprez_mcp", "ERROR") as logs:
            with self.assertRaisesMessage(ToolError, "Rejected after the save."):
                provider.run_update(
                    ctx, id=self.document.pk, file=FileValue("data", b"x", "n.txt")
                )
        self.assertIn("Could not delete docs/n", logs.output[0])

    def test_failed_write_removes_the_upload(self):
        def perform_update(provider, ctx, instance, data):
            DocumentProvider.perform_update(provider, ctx, instance, data)
            raise ToolError("Rejected after the save.")

        provider, ctx = self.provider(perform_update=perform_update)
        with self.assertRaisesMessage(ToolError, "Rejected after the save."):
            provider.run_update(
                ctx, id=self.document.pk, file=FileValue("data", b"x", "new.txt")
            )
        self.assertEqual(default_storage.listdir("docs")[1], ["report.txt"])
        self.document.refresh_from_db()
        self.assertEqual(self.document.file.name, "docs/report.txt")

    def test_directory_path(self):
        default_storage.save("docs/sub/a.txt", ContentFile(b"a"))
        error = self.failed(
            "documents_update", {"id": self.document.pk, "file": {"path": "docs/sub"}}
        )
        self.assertIn("no file at 'docs/sub'", error)
        self.document.refresh_from_db()
        self.assertEqual(self.document.file.name, "docs/report.txt")

    def test_clear_and_unchanged(self):
        self.structured(
            "documents_update", {"id": self.document.pk, "attachment": None}
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.attachment.name, "")
        self.assertEqual(self.document.file.name, "docs/report.txt")
        error = self.failed("documents_update", {"id": self.document.pk, "file": None})
        self.assertIn("file is required and cannot be cleared", error)

    def test_url_is_opt_in(self):
        error = self.failed(
            "documents_update",
            {"id": self.document.pk, "file": {"url": "https://example.com/a.txt"}},
        )
        self.assertIn("url is not accepted here", error)

    def test_size_limit(self):
        with mock.patch.object(DocumentProvider, "max_file_size", 3):
            error = self.failed(
                "documents_update",
                {"id": self.document.pk, "file": {"data": b64(b"four"), "name": "a"}},
            )
        self.assertIn("larger than 3 bytes", error)

    def test_generic(self):
        self.url = "/files-generic/mcp/"
        writable = self.structured("models_list")["models"][0]["writable"]
        self.assertIn("anyOf", writable["file"])
        self.structured(
            "record_update",
            {
                "model": "documents",
                "id": self.document.pk,
                "data": {"file": {"data": b64(b"v2"), "name": "v2.txt"}},
            },
        )
        self.document.refresh_from_db()
        self.assertEqual(self.read(self.document.file), b"v2")


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/files/logo")
            self.end_headers()
            return
        if self.path == "/slow":
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:  # the client hung up
                pass
            return
        self.send_response(200)
        if self.path != "/images/hero":
            self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(b"PNG!")

    def log_message(self, *args):
        pass


class AdminFileTests(FilesTestCase):
    url = "/files-admin/mcp/"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.editor = User.objects.create_user("editor", is_staff=True)
        grant(
            cls.editor,
            "view_document",
            "add_document",
            "change_document",
            "add_page",
            "change_page",
        )

    def call(self, name, arguments=None, token="token-editor", **kwargs):
        return super().call(name, arguments, token=token, **kwargs)

    def test_create_and_log(self):
        result = self.structured(
            "documents_create",
            {"title": "New", "file": {"data": b64(b"hello"), "name": "hello.txt"}},
        )
        document = Document.objects.get(pk=result["id"])
        self.assertEqual(self.read(document.file), b"hello")
        entry = LogEntry.objects.get(object_id=str(document.pk))
        self.assertEqual(entry.action_flag, ADDITION)

    def test_update_path_and_clear(self):
        stored = default_storage.save("docs/other.txt", ContentFile(b"other"))
        self.structured(
            "documents_update",
            {"id": self.document.pk, "file": {"path": stored}, "attachment": None},
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.file.name, stored)
        self.assertEqual(self.document.attachment.name, "")
        entry = LogEntry.objects.get(object_id=str(self.document.pk))
        self.assertEqual(entry.action_flag, CHANGE)
        self.assertIn("File", entry.get_change_message())

    def test_request_files_on_main_and_inline_writes(self):
        seen = {}
        save_model = DocumentAdmin.save_model
        save_formset = DocumentAdmin.save_formset

        def record_model(model_admin, request, obj, form, change):
            seen["model"] = list(request.FILES)
            return save_model(model_admin, request, obj, form, change)

        def record_formset(model_admin, request, form, formset, change):
            seen["formset"] = list(request.FILES)
            return save_formset(model_admin, request, form, formset, change)

        upload = {"data": b64(b"x"), "name": "x.txt"}
        with mock.patch.object(DocumentAdmin, "save_model", record_model):
            with mock.patch.object(DocumentAdmin, "save_formset", record_formset):
                self.structured(
                    "documents_update", {"id": self.document.pk, "file": upload}
                )
                self.structured(
                    "documents_pages_create",
                    {"parent": self.document.pk, "number": 1, "scan": upload},
                )
        # as documented: the shared request has no FILES, the inline copy has
        self.assertEqual(seen["model"], [])
        self.assertEqual(seen["formset"], ["pages-0-scan"])

    def test_admin_code_shares_the_request(self):
        seen = []
        get_fields = DocumentAdmin.get_fields
        save_model = DocumentAdmin.save_model

        def record_fields(model_admin, request, obj=None):
            seen.append(request)
            return get_fields(model_admin, request, obj)

        def record_save(model_admin, request, obj, form, change):
            seen.append(request)
            return save_model(model_admin, request, obj, form, change)

        with mock.patch.object(DocumentAdmin, "get_fields", record_fields):
            with mock.patch.object(DocumentAdmin, "save_model", record_save):
                self.structured(
                    "documents_update",
                    {
                        "id": self.document.pk,
                        "file": {"data": b64(b"v2"), "name": "v2.txt"},
                    },
                )
        self.assertGreater(len(seen), 2)  # the form, the save, the result
        self.assertEqual({id(request) for request in seen}, {id(seen[0])})

    def test_failed_admin_write_removes_the_upload(self):
        with mock.patch.object(
            DocumentAdmin, "log_change", side_effect=RuntimeError("log failed")
        ):
            result = self.call(
                "documents_update",
                {"id": self.document.pk, "file": {"data": b64(b"x"), "name": "n.txt"}},
            )
        self.assertTrue(result["isError"])
        self.assertEqual(default_storage.listdir("docs")[1], ["report.txt"])

    def test_settable_checked_once(self):
        check = DocumentAdminProvider.check_settable
        with mock.patch.object(
            DocumentAdminProvider, "check_settable", autospec=True, side_effect=check
        ) as checked:
            self.structured(
                "documents_update", {"id": self.document.pk, "title": "Renamed"}
            )
        self.assertEqual(checked.call_count, 1)

    def test_long_fetched_name(self):
        base = self.serve()
        url = f"{base}/{'a' * 150}.png"
        self.structured(
            "documents_update", {"id": self.document.pk, "file": {"url": url}}
        )
        self.document.refresh_from_db()
        name = self.document.file.name
        self.assertTrue(name.startswith("docs/aaa") and name.endswith(".png"), name)
        self.assertLessEqual(len(name), 100)

    def test_inline_unknown_row_does_not_fetch(self):
        with mock.patch.object(DocumentAdminProvider, "fetch_file") as fetch_file:
            error = self.failed(
                "documents_pages_update",
                {
                    "parent": self.document.pk,
                    "row": 999,
                    "scan": {"url": "https://example.com/a.png"},
                },
            )
        self.assertIn("No pages row 999", error)
        fetch_file.assert_not_called()

    def test_inline(self):
        row = self.structured(
            "documents_pages_create",
            {
                "parent": self.document.pk,
                "number": 1,
                "scan": {"data": b64(b"scan"), "name": "p1.png"},
            },
        )
        page = Page.objects.get(pk=row["id"])
        self.assertEqual(self.read(page.scan), b"scan")
        self.structured(
            "documents_pages_update",
            {"parent": self.document.pk, "row": page.pk, "scan": None},
        )
        page.refresh_from_db()
        self.assertEqual(page.scan.name, "")
        self.assertEqual(page.number, 1)

    def test_url_refuses_internal_addresses(self):
        error = self.failed(
            "documents_update",
            {"id": self.document.pk, "file": {"url": "http://127.0.0.1:1/a.png"}},
        )
        self.assertIn("127.0.0.1 is not a public address", error)

    def test_url(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/redirect"
        with mock.patch.object(files, "is_public_address", return_value=True):
            self.structured(
                "documents_update", {"id": self.document.pk, "file": {"url": url}}
            )
        self.document.refresh_from_db()
        self.assertTrue(self.document.file.name.startswith("docs/logo"))
        self.assertTrue(self.document.file.name.endswith(".png"))
        self.assertEqual(self.read(self.document.file), b"PNG!")

    def serve(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        public = mock.patch.object(files, "is_public_address", return_value=True)
        public.start()
        self.addCleanup(public.stop)
        return f"http://127.0.0.1:{server.server_port}"

    def test_fetch_size_limit(self):
        base = self.serve()
        with self.assertRaisesMessage(ToolError, "larger than 3 bytes"):
            files.fetch(f"{base}/a", max_size=3)

    def test_fetch_deadline(self):
        base = self.serve()
        started = time.monotonic()
        with self.assertRaisesMessage(ToolError, "took longer than 0.5 seconds"):
            files.fetch(f"{base}/slow", timeout=0.5)  # each byte within 0.5 s
        self.assertLess(time.monotonic() - started, 3)

    def test_fetch_dot_dot_name(self):
        base = self.serve()
        _, name, _ = files.fetch(f"{base}/files/%2e%2e")
        self.assertEqual(name, "download.png")

    def test_fetch_without_content_type(self):
        base = self.serve()
        content, name, content_type = files.fetch(f"{base}/images/hero")
        self.assertEqual((content, name, content_type), (b"PNG!", "hero", None))


@unittest.skipIf(rest_framework is None, "DRF is not installed")
class SerializerFileTests(FilesTestCase):
    def test_serializer_gets_the_file(self):
        from rest_framework import serializers

        class DocumentSerializer(serializers.ModelSerializer):
            class Meta:
                model = Document
                fields = ["id", "title", "file", "attachment"]

        class Provider(ModelProvider):
            serializer = DocumentSerializer
            actions = ["update"]

        provider = Provider(MCPServer(name="drf-files"), model=Document)
        ctx = CallContext(server=provider.server)
        data = provider.validate(
            ctx,
            {"file": FileValue("data", b"v2", "v2.txt")},
            instance=self.document,
        )
        self.assertEqual(data["file"].read(), b"v2")
        provider.perform_update(ctx, self.document, data)
        self.document.refresh_from_db()
        self.assertEqual(self.read(self.document.file), b"v2")

    def test_null_follows_the_serializer(self):
        from rest_framework import serializers

        class Strict(serializers.ModelSerializer):
            class Meta:
                model = Document
                fields = ["id", "title", "file", "attachment"]

        class Lenient(Strict):
            attachment = serializers.FileField(allow_null=True, required=False)

        def nullable(serializer):
            provider_class = type(
                "Provider", (ModelProvider,), {"serializer": serializer}
            )
            provider = provider_class(MCPServer(name="drf-null"), model=Document)
            params = {name: ann for name, ann, _ in provider.write_parameters(True)}
            return params["attachment"].nullable

        # ModelSerializer allows null only for null=True model fields
        self.assertFalse(nullable(Strict))
        self.assertTrue(nullable(Lenient))

    def test_upload_fields_only(self):
        from rest_framework import serializers

        from xprez_mcp.providers.model import _is_serializer_file_field

        class Base64File(serializers.FileField):
            """Like drf-extra-fields' Base64FileField: decodes a string."""

            def to_internal_value(self, data):
                return data

        self.assertTrue(_is_serializer_file_field(serializers.FileField()))
        self.assertTrue(_is_serializer_file_field(serializers.ImageField()))
        self.assertFalse(_is_serializer_file_field(Base64File()))
        self.assertFalse(_is_serializer_file_field(serializers.CharField()))

        class Base64Serializer(serializers.ModelSerializer):
            file = Base64File()

            class Meta:
                model = Document
                fields = ["id", "title", "file"]

        provider_class = type(
            "Provider", (ModelProvider,), {"serializer": Base64Serializer}
        )
        provider = provider_class(MCPServer(name="drf-b64-file"), model=Document)
        params = {name: ann for name, ann, _ in provider.write_parameters(True)}
        self.assertIs(params["file"], str)

    def test_non_file_serializer_field_keeps_its_value(self):
        from rest_framework import serializers

        class Base64Serializer(serializers.ModelSerializer):
            file = serializers.CharField()  # say, base64 the serializer decodes

            class Meta:
                model = Document
                fields = ["id", "title", "file"]

        provider_class = type(
            "Provider", (ModelProvider,), {"serializer": Base64Serializer}
        )
        provider = provider_class(MCPServer(name="drf-b64"), model=Document)
        params = {name: ann for name, ann, _ in provider.write_parameters(True)}
        self.assertIs(params["file"], str)
        data = provider.validate(
            CallContext(server=provider.server), {"file": "aGk="}, self.document
        )
        self.assertEqual(data["file"], "aGk=")
