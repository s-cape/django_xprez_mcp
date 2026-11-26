from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase


class FakeConfig:
    def __init__(self, label, counts=None, error=None):
        self.label = label
        self.counts = counts or {}
        self.error = error
        self.calls = []

    def mcp_cleanup(self, now):
        self.calls.append(now)
        if self.error:
            raise self.error
        return self.counts


class NoHook:
    label = "plain"


class CleanupCommandTests(SimpleTestCase):
    def run_cleanup(self, configs, *args):
        out, err = StringIO(), StringIO()
        with mock.patch(
            "xprez_mcp.management.commands.mcp_cleanup.apps.get_app_configs",
            return_value=configs,
        ):
            call_command("mcp_cleanup", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_project_app(self):
        project = FakeConfig("notes", {"drafts": 2, "attachments": 0})
        out, _ = self.run_cleanup([NoHook(), project])
        self.assertEqual(out, "notes: 2 drafts, 0 attachments deleted.\n")
        self.assertEqual(len(project.calls), 1)

    def test_nothing_to_clean(self):
        out, _ = self.run_cleanup([NoHook()])
        self.assertEqual(out, "No installed app has anything to clean up.\n")
        out, _ = self.run_cleanup([FakeConfig("notes")])
        self.assertEqual(out, "notes: nothing deleted.\n")

    def test_failure_does_not_stop_others(self):
        broken = FakeConfig("files", error=OSError("storage down"))
        notes = FakeConfig("notes", {"drafts": 1})
        out, err = StringIO(), StringIO()
        with mock.patch(
            "xprez_mcp.management.commands.mcp_cleanup.apps.get_app_configs",
            return_value=[broken, notes],
        ):
            with self.assertRaisesMessage(CommandError, "Cleanup failed for files."):
                with self.assertLogs("xprez_mcp", "ERROR"):
                    call_command("mcp_cleanup", stdout=out, stderr=err)
        self.assertIn("files: failed: OSError('storage down')", err.getvalue())
        self.assertEqual(out.getvalue(), "notes: 1 drafts deleted.\n")

    def test_only_runs_each_once(self):
        notes, files = FakeConfig("notes"), FakeConfig("files")
        out, _ = self.run_cleanup([notes, files], "--only", "notes", "notes")
        self.assertEqual(out, "notes: nothing deleted.\n")
        self.assertEqual((len(notes.calls), len(files.calls)), (1, 0))
