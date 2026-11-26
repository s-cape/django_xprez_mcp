import hashlib
import io
from collections import defaultdict
import os
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from xprez_mcp.contrib.uploads import checks, cleanup, conf
from xprez_mcp.contrib.uploads.batches import normalize_path, prepare_uploads
from xprez_mcp.core.context import CallContext
from xprez_mcp.core.files import resolve_uploads
from xprez_mcp.contrib.uploads.content import detect
from xprez_mcp.contrib.uploads.models import Upload
from xprez_mcp.contrib.uploads.views import UploadView
from xprez_mcp.errors import ToolError
from xprez_mcp.testing import McpTestCase

from .testapp.models import Document, Tag
from .testapp.storages import upload_storage
from .testapp.upload_servers import DocumentProvider, uploads_mcp

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'


def entry(path, content):
    return {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def uploads_setting(**options):
    return override_settings(
        XPREZ_MCP={
            **settings.XPREZ_MCP,
            "UPLOADS": {**settings.XPREZ_MCP["UPLOADS"], **options},
        }
    )


class UploadTestCase(McpTestCase):
    mcp_url = "/uploads/mcp/"

    @classmethod
    def setUpTestData(cls):
        cls.mcp_user = User.objects.create_user("alice")
        cls.bob = User.objects.create_user("bob")

    def tearDown(self):
        for name in os.listdir(upload_storage.location):
            shutil.rmtree(os.path.join(upload_storage.location, name))

    def prepare(self, *files, user=None, **arguments):
        structured, _ = self.call(
            "files_prepare",
            user=user or self.mcp_user,
            files=[entry(path, content) for path, content in files],
            **arguments,
        )
        return structured

    def put(self, upload, content, token=None):
        token = token or upload["headers"]["Authorization"].split(" ", 1)[1]
        return self.client.put(
            upload["url"],
            content,
            content_type="application/octet-stream",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def upload(self, path, content, **arguments):
        prepared = self.prepare((path, content), **arguments)
        response = self.put(prepared["uploads"][0], content)
        self.assertEqual(response.status_code, 201, response.content)
        return prepared["batch_id"]

    def staged(self):
        try:
            return upload_storage.listdir(conf.PREFIX.rstrip("/"))[1]
        except FileNotFoundError:
            return []


class FlowTests(UploadTestCase):
    def test_prepare(self):
        prepared = self.prepare(("notes/a.md", b"# A"), ("notes/b.md", b"# B"))
        self.assertEqual(len(prepared["batch_id"]), 32)
        first = prepared["uploads"][0]
        self.assertEqual(first["path"], "notes/a.md")
        self.assertEqual(first["method"], "PUT")
        self.assertRegex(
            first["url"], r"^http://testserver/uploads/mcp/uploads/[0-9a-f]{16}/$"
        )
        self.assertTrue(first["headers"]["Authorization"].startswith("Bearer mcpup_"))
        self.assertIn("curl", prepared["upload_hint"])
        self.assertIn("expires_at", prepared)

    def test_full_flow(self):
        batch = self.upload("notes/intro.md", b"# Hello")
        self.assertEqual(len(self.staged()), 1)
        argument = {"upload": {"batch": batch, "path": "notes/intro.md"}}
        with self.captureOnCommitCallbacks(execute=True):
            structured, _ = self.call("files_store", file=argument)
        self.assertEqual(
            structured,
            {
                "name": "intro.md",
                "size": 7,
                "sha256": hashlib.sha256(b"# Hello").hexdigest(),
                "content_type": "text/markdown",
                "content": "# Hello",
                "streamed": "# Hello",
            },
        )
        upload = Upload.objects.get()
        self.assertIsNotNone(upload.consumed_at)
        self.assertEqual(upload.stored_name, "")
        self.assertEqual(self.staged(), [])  # deleted when the call committed
        text = self.assertToolError("files_store", file=argument)
        self.assertEqual(text, "Upload 'notes/intro.md' was already used.")

    def test_model_provider(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        batch = self.upload("scans/report.pdf", b"%PDF-1.7 report")
        # max_file_size bounds data and url, not uploads (they have their own)
        small = mock.patch.object(DocumentProvider, "max_file_size", 4)
        with override_settings(MEDIA_ROOT=media), small:
            structured, _ = self.call(
                "documents_create",
                title="Report",
                file={"upload": {"batch": batch, "path": "scans/report.pdf"}},
            )
            document = Document.objects.get(pk=structured["id"])
            self.assertEqual(document.file.name, "docs/report.pdf")
            self.assertEqual(document.file.read(), b"%PDF-1.7 report")
            document.file.close()
        self.assertIsNotNone(Upload.objects.get().consumed_at)

    def test_rollback_keeps_upload(self):
        batch = self.upload("a.txt", b"text")
        argument = {"upload": {"batch": batch, "path": "a.txt"}}
        self.assertToolError("files_store_and_fail", file=argument)
        self.assertFalse(Tag.objects.exists())
        self.assertIsNone(Upload.objects.get().consumed_at)
        self.call("files_store", file=argument)
        self.assertTrue(Tag.objects.filter(name="a.txt").exists())

    def test_same_upload_twice_in_one_call(self):
        batch = self.upload("a.txt", b"text")
        argument = {"upload": {"batch": batch, "path": "a.txt"}}
        structured, _ = self.call("files_store_two", file=argument, other=argument)
        self.assertEqual(structured, {"same": True})
        self.assertIsNotNone(Upload.objects.get().consumed_at)

    def test_concurrent_use_rolls_back(self):
        batch = self.upload("a.txt", b"text")
        text = self.assertToolError(
            "files_store_raced", file={"upload": {"batch": batch, "path": "a.txt"}}
        )
        self.assertEqual(text, "Upload 'a.txt' was already used.")
        self.assertFalse(Tag.objects.exists())

    def test_data_still_works(self):
        structured, _ = self.call(
            "files_store", file={"data": "aGk=", "name": "hi.txt"}
        )
        self.assertEqual(structured["content"], "hi")
        self.assertEqual(structured["content_type"], "text/plain")
        self.assertEqual(structured["size"], 2)

    def test_schema(self):
        tools = {
            tool["name"]: tool
            for tool in self.rpc("tools/list").json()["result"]["tools"]
        }
        schema = tools["files_store"]["inputSchema"]["properties"]["file"]
        self.assertEqual(len(schema["anyOf"]), 2)
        self.assertIn("{upload: {batch, path}}", schema["description"])

    def test_reference_errors(self):
        batch = self.prepare(("a.txt", b"text"))["batch_id"]
        cases = {
            "Upload 'a.txt' has not been uploaded yet.": {
                "batch": batch,
                "path": "a.txt",
            },
            "Upload 'b.txt' is not in batch": {"batch": batch, "path": "b.txt"},
            "file.upload must be {batch, path}.": {"batch": batch},
            "upload.path must be a relative path": {"batch": batch, "path": "/a.txt"},
        }
        for message, reference in cases.items():
            with self.subTest(message):
                text = self.assertToolError("files_store", file={"upload": reference})
                self.assertIn(message, text)
        Upload.objects.update(expires=timezone.now())
        text = self.assertToolError(
            "files_store", file={"upload": {"batch": batch, "path": "a.txt"}}
        )
        self.assertEqual(text, "Upload 'a.txt' expired. Prepare it again.")

    def test_received_too_long_ago(self):
        batch = self.upload("a.txt", b"text")
        Upload.objects.update(received_at=timezone.now() - timedelta(days=2))
        text = self.assertToolError(
            "files_store", file={"upload": {"batch": batch, "path": "a.txt"}}
        )
        self.assertEqual(text, "Upload 'a.txt' expired. Prepare it again.")

    def test_other_users_batch(self):
        batch = self.upload("a.txt", b"text")
        reference = {"upload": {"batch": batch, "path": "a.txt"}}
        text = self.assertToolError("files_store", user=self.bob, file=reference)
        self.assertIn("is not in batch", text)
        text = self.assertToolError("files_batch", user=self.bob, batch_id=batch)
        self.assertEqual(text, f"Unknown batch {batch!r}.")
        text = self.assertToolError(
            "files_prepare", user=self.bob, files=[entry("b.txt", b"b")], batch=batch
        )
        self.assertEqual(text, f"Unknown batch {batch!r}.")
        self.assertIsNone(Upload.objects.get().consumed_at)


class BatchTests(UploadTestCase):
    def test_states_and_consume(self):
        prepared = self.prepare(
            ("a.txt", b"a"), ("b.txt", b"b"), ("c.txt", b"c"), ("d.txt", b"d")
        )
        batch, uploads = prepared["batch_id"], prepared["uploads"]
        self.assertEqual(self.put(uploads[0], b"a").status_code, 201)
        self.assertEqual(self.put(uploads[1], b"b").status_code, 201)
        self.assertEqual(self.put(uploads[2], b"x").status_code, 400)
        structured, _ = self.call("files_batch", batch_id=batch)
        self.assertEqual(
            structured,
            {
                "received": ["a.txt", "b.txt"],
                "missing": ["d.txt"],
                "expired": [],
                "failed": ["c.txt"],
                "consumed": [],
            },
        )
        with self.captureOnCommitCallbacks(execute=True):
            structured, _ = self.call("files_import_batch", batch_id=batch)
        self.assertEqual(
            structured, {"consumed": 2, "contents": {"a.txt": "a", "b.txt": "b"}}
        )
        self.assertEqual(self.staged(), [])
        structured, _ = self.call("files_batch", batch_id=batch)
        self.assertEqual(structured["consumed"], ["a.txt", "b.txt"])

    def test_add_and_replace(self):
        first = self.prepare(("a.txt", b"a"), ("b.txt", b"b"))
        batch = first["batch_id"]
        self.assertEqual(self.put(first["uploads"][0], b"a").status_code, 201)
        with self.captureOnCommitCallbacks(execute=True):
            second = self.prepare(("a.txt", b"A2"), ("c.txt", b"c"), batch=batch)
        self.assertEqual(second["batch_id"], batch)
        self.assertEqual(
            sorted(Upload.objects.values_list("path", flat=True)),
            ["a.txt", "b.txt", "c.txt"],
        )
        self.assertEqual(self.staged(), [])  # the replaced file is gone
        self.assertEqual(self.put(first["uploads"][0], b"a").status_code, 404)
        self.assertEqual(self.put(second["uploads"][0], b"A2").status_code, 201)

    def test_validation(self):
        cases = {
            "files must be a non-empty list.": [],
            "files[1].path 'a.txt' is listed twice.": [
                entry("a.txt", b"a"),
                entry("./a.txt", b"a"),
            ],
            "files[0].sha256 must be 64 hexadecimal characters.": [
                {"path": "a.txt", "sha256": "abc", "size": 1}
            ],
            "files[0].size must be a whole number of bytes.": [
                {**entry("a.txt", b"a"), "size": -1}
            ],
            "files[0]: unknown keys ['name'].": [{**entry("a.txt", b"a"), "name": "x"}],
            "files[0].path must be a relative path without '..'.": [
                entry("../a.txt", b"a")
            ],
        }
        for message, files in cases.items():
            with self.subTest(message):
                self.assertEqual(
                    self.assertToolError("files_prepare", files=files), message
                )
        with uploads_setting(MAX_FILE_SIZE=2, MAX_FILES=1):
            text = self.assertToolError("files_prepare", files=[entry("a.txt", b"abc")])
            self.assertEqual(text, "files[0]: a.txt is larger than 2 bytes.")
            text = self.assertToolError(
                "files_prepare", files=[entry("a.txt", b"a"), entry("b.txt", b"b")]
            )
            self.assertEqual(text, "A batch holds at most 1 files, not 2.")
        self.assertFalse(Upload.objects.exists())

    def test_pending_limit(self):
        with uploads_setting(MAX_PENDING_BYTES=5):
            self.prepare(("a.txt", b"abc"))
            text = self.assertToolError("files_prepare", files=[entry("b.txt", b"abc")])
            self.assertIn("would pass the limit", text)
            Upload.objects.update(expires=timezone.now())  # expired: no longer waiting
            self.prepare(("b.txt", b"abc"))

    def test_paths(self):
        self.assertEqual(normalize_path("a//b/./c.md"), "a/b/c.md")
        for bad in ("", "/a", "a/../b", "a\\b", "a\x00b", "a\nb", ".", "x" * 501):
            with self.subTest(bad), self.assertRaises(ToolError):
                normalize_path(bad)


class EndpointTests(UploadTestCase):
    def test_responses(self):
        prepared = self.prepare(("a.png", PNG))
        upload = prepared["uploads"][0]
        self.assertEqual(self.client.get(upload["url"]).status_code, 405)
        self.assertEqual(self.client.post(upload["url"]).status_code, 405)
        self.assertEqual(self.put(upload, PNG, token="mcpup_wrong").status_code, 404)
        self.assertEqual(
            self.client.put(upload["url"], PNG, content_type="image/png").status_code,
            404,
        )
        response = self.put(upload, PNG)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(), {"path": "a.png", "size": 40, "content_type": "image/png"}
        )
        self.assertEqual(self.put(upload, PNG).status_code, 409)
        self.assertEqual(
            self.client.put("/uploads/mcp/uploads/0000000000000000/", PNG).status_code,
            404,
        )

    def test_mismatches(self):
        prepared = self.prepare(("a.txt", b"abc"), ("b.txt", b"abc"), ("c.txt", b"abc"))
        a, b, c = prepared["uploads"]
        response = self.put(a, b"abd")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"], "The file does not match its sha256."
        )
        response = self.put(b, b"ab")
        self.assertEqual(response.json()["error"], "The file has 2 bytes, not 3.")
        self.assertEqual(self.put(c, b"abcd").status_code, 413)
        self.assertEqual(
            set(Upload.objects.values_list("failure", flat=True)),
            {
                "The file does not match its sha256.",
                "The file has 2 bytes, not 3.",
                "The file is larger than 3 bytes.",
            },
        )
        response = self.put(a, b"abc")
        self.assertEqual(response.status_code, 410)
        self.assertIn("failed", response.json()["error"])
        self.assertEqual(self.staged(), [])

    def test_streaming_stops_early(self):
        upload = Upload(size=3, sha256="0" * 64, path="a.txt", content_types=[])
        request = mock.Mock()
        request.read = io.BytesIO(b"x" * 10).read
        view = UploadView()
        view.chunk_size = 2
        with tempfile.TemporaryFile() as temporary:
            problem = view.receive(request, upload, temporary)
            self.assertEqual(problem, (413, "The file is larger than 3 bytes."))
            self.assertLessEqual(temporary.tell(), 3)

    def test_length_required(self):
        upload = self.prepare(("a.txt", b"abc"))["uploads"][0]
        token = upload["headers"]["Authorization"].split(" ", 1)[1]
        response = self.client.generic(
            "PUT",
            upload["url"],
            b"abc",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            CONTENT_LENGTH="",
        )
        self.assertEqual(response.status_code, 411)
        self.assertEqual(Upload.objects.get().failure, "")
        self.assertEqual(self.put(upload, b"abc").status_code, 201)

    def test_expired(self):
        prepared = self.prepare(("a.txt", b"abc"))
        Upload.objects.update(expires=timezone.now())
        response = self.put(prepared["uploads"][0], b"abc")
        self.assertEqual(response.status_code, 410)

    def test_content(self):
        cases = [
            ("image.png", SVG, None, "The file is image/svg+xml"),
            ("page.md", b"<!DOCTYPE html><script>x</script>", None, "is text/html"),
            ("photo.png", b"\x00\x01binary", None, "application/octet-stream"),
            ("photo.jpg", PNG, ["image/jpeg"], "The file is image/png"),
            ("icon.svg", SVG, ["image/*"], "The file is image/svg+xml"),
            ("page.html", b"<p onclick=x>hi</p>", None, "The file is text/html"),
            ("evil.html", PNG, None, "image/png, but its name ends in .html"),
            ("app.js", b"alert(1)", ["text/*"], "The file is text/javascript"),
            ("notes.md", PNG, None, "image/png, but its name ends in .md"),
        ]
        for path, content, types, message in cases:
            with self.subTest(path):
                arguments = {"content_types": types} if types else {}
                upload = self.prepare((path, content), **arguments)["uploads"][0]
                response = self.put(upload, content)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])
        upload = self.prepare(("icon.svg", SVG), content_types=["image/svg+xml"])
        self.assertEqual(self.put(upload["uploads"][0], SVG).status_code, 201)
        upload = self.prepare(("p.html", b"<p>hi</p>"), content_types=["text/html"])
        self.assertEqual(self.put(upload["uploads"][0], b"<p>hi</p>").status_code, 201)
        for path, content in (("README", b"read me"), ("photo.raw", PNG)):
            upload = self.prepare((path, content))["uploads"][0]
            self.assertEqual(self.put(upload, content).status_code, 201, path)

    def test_content_types_must_be_a_list(self):
        ctx = CallContext(server=uploads_mcp, user=self.mcp_user)
        for value in ("image/png", ["image/png", ""], [None]):
            with self.subTest(value), self.assertRaises(TypeError):
                prepare_uploads(ctx, [entry("a.png", PNG)], content_types=value)

    def test_detect(self):
        self.assertEqual(detect(b"\xff\xd8\xff\xe0", False, "a"), "image/jpeg")
        self.assertEqual(detect(b"GIF89a..", False, "a"), "image/gif")
        self.assertEqual(
            detect(b"RIFF\x00\x00\x00\x00WEBPVP8", False, "a"), "image/webp"
        )
        self.assertEqual(detect(b'{"a": 1}', True, "x.JSON"), "application/json")
        self.assertEqual(detect(b"a,b", True, "x.csv"), "text/csv")
        self.assertEqual(detect(b"plain", True, "x"), "text/plain")
        self.assertEqual(detect(b"# <script> in prose", True, "x.md"), "text/markdown")

    def test_validator(self):
        def validator(file, upload):
            if file.read().startswith(b"bad"):
                raise ValidationError("Not a good file.")

        with uploads_setting(VALIDATOR=validator):
            upload = self.prepare(("a.txt", b"bad"))["uploads"][0]
            response = self.put(upload, b"bad")
        self.assertEqual(response.json(), {"error": "Not a good file."})

    def test_key_stored_hashed_and_never_logged(self):
        upload = self.prepare(("a.txt", b"abc"))["uploads"][0]
        key = upload["headers"]["Authorization"].split(" ", 1)[1]
        row = Upload.objects.get()
        self.assertEqual(row.key_hash, hashlib.sha256(key.encode()).hexdigest())
        values = [str(value) for value in Upload.objects.values().get().values()]
        self.assertNotIn(key, values)
        self.assertNotIn(key, upload["url"])
        with self.assertLogs("xprez_mcp", "INFO") as logs:
            self.put(upload, b"abc")
            self.put(upload, b"abc")
            self.put(upload, b"abc", token="mcpup_guess")
        output = "\n".join(logs.output)
        self.assertNotIn(key, output)
        self.assertNotIn(key[6:12], output)
        self.assertIn(f"upload={row.pk}", output)


class CleanupTests(UploadTestCase):
    def test_cleanup(self):
        now = timezone.now()
        prepared = self.prepare(
            ("fresh.txt", b"a"),
            ("old.txt", b"b"),
            ("used.txt", b"c"),
            ("late.txt", b"d"),
        )
        for upload, content in zip(prepared["uploads"], (b"a", b"b", b"c")):
            self.put(upload, content)
        Upload.objects.filter(path="old.txt").update(
            received_at=now - timedelta(days=2)
        )
        Upload.objects.filter(path="used.txt").update(consumed_at=now)
        Upload.objects.filter(path="late.txt").update(expires=now)
        orphan = upload_storage.save(conf.PREFIX + "orphan", ContentFile(b"x"))
        os.utime(upload_storage.path(orphan), (0, 0))
        young = upload_storage.save(conf.PREFIX + "young", ContentFile(b"x"))
        out = io.StringIO()
        call_command("mcp_cleanup", "--only", "xprez_mcp_uploads", stdout=out)
        self.assertEqual(
            out.getvalue(), "xprez_mcp_uploads: 3 uploads, 3 files deleted.\n"
        )
        self.assertEqual(
            list(Upload.objects.values_list("path", flat=True)), ["fresh.txt"]
        )
        staged = self.staged()
        self.assertEqual(len(staged), 2)
        self.assertIn(young.split("/")[-1], staged)

    def test_storage_errors_keep_rows(self):
        prepared = self.prepare(("a.txt", b"a"))
        self.put(prepared["uploads"][0], b"a")
        Upload.objects.update(consumed_at=timezone.now())
        with mock.patch.object(
            type(upload_storage), "delete", side_effect=OSError("down")
        ):
            with self.assertRaisesMessage(cleanup.CleanupError, "OSError('down')"):
                cleanup.cleanup(timezone.now())
        self.assertTrue(Upload.objects.exists())


class CheckTests(SimpleTestCase):
    def test_storage_required(self):
        for value in (None, "django.core.files.storage.FileSystemStorage"):
            with self.subTest(value):
                with override_settings(XPREZ_MCP={"UPLOADS": {"STORAGE": value}}):
                    errors = checks.check_uploads()
                self.assertEqual(
                    [error.id for error in errors], ["xprez_mcp_uploads.E001"]
                )
        self.assertEqual(checks.check_uploads(), [])

    def test_resolve_uploads_leaves_other_objects(self):
        counts = defaultdict(int)
        arguments = {"counts": counts, "items": [1, (2, 3)]}
        self.assertIs(resolve_uploads(None, arguments), arguments)
        self.assertIs(arguments["counts"], counts)
