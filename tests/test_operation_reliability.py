"""Reliability regressions for operation state, idempotency, and undo recovery."""
import tempfile
import unittest
from pathlib import Path

from core.operation_registry import OperationRegistry
from core.execution import ExecutionPolicyError, _validate_timeout


class OperationRegistryTests(unittest.TestCase):
    def test_same_idempotency_key_returns_existing_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = OperationRegistry(Path(temp) / "audit.jsonl")
            first, created = registry.create("external-effect:abc", "Publish release", "network", ("network",))
            self.assertTrue(created)
            duplicate, created_again = registry.create("external-effect:abc", "Publish release", "network", ("network",))
            self.assertFalse(created_again)
            self.assertEqual(first["operation_id"], duplicate["operation_id"])

    def test_failed_operation_is_not_replayed_automatically(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = OperationRegistry(Path(temp) / "audit.jsonl")
            first, _ = registry.create("send:invoice-1", "Send invoice", "network", ("network",))
            registry.transition(first["operation_id"], "running")
            registry.transition(first["operation_id"], "failed", "Response lost; outcome uncertain")
            duplicate, created = registry.create("send:invoice-1", "Send invoice", "network", ("network",))
            self.assertFalse(created)
            self.assertEqual(first["operation_id"], duplicate["operation_id"])
            self.assertEqual("failed", duplicate["status"])

    def test_restart_marks_inflight_operation_uncertain_and_does_not_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "audit.jsonl"
            first_registry = OperationRegistry(path)
            first, _ = first_registry.create("deploy:prod-v2", "Deploy", "project", ("process",))
            first_registry.transition(first["operation_id"], "running")
            restarted = OperationRegistry(path)
            record = restarted.get(first["operation_id"])
            self.assertEqual("unknown_after_restart", record["status"])
            duplicate, created = restarted.create("deploy:prod-v2", "Deploy", "project", ("process",))
            self.assertFalse(created)
            self.assertEqual(first["operation_id"], duplicate["operation_id"])

    def test_audit_tracks_terminal_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "audit.jsonl"
            registry = OperationRegistry(path)
            record, _ = registry.create("x", "Example", "general", ("filesystem.read",))
            registry.transition(record["operation_id"], "running")
            completed = registry.transition(record["operation_id"], "completed", "ok")
            self.assertIsNotNone(completed["finished_at"])
            self.assertIsNotNone(completed["duration_ms"])
            log = path.read_text(encoding="utf-8")
            self.assertIn('"status":"completed"', log)
            self.assertIn('"permissions":["filesystem.read"]', log)


class UndoRecoveryTests(unittest.TestCase):
    def test_failed_undo_is_retained_and_can_be_retried(self):
        from core import undo

        undo.clear()
        calls = {"count": 0}

        def reverse():
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("temporary filesystem error")
            return "restored"

        undo.push_undo("move report.txt", reverse, description="Move report.txt from A to B")
        first = undo.undo_last()
        self.assertIn("record was retained", first)
        self.assertIn("FAILED:", undo.history()[0])
        second = undo.undo_last()
        self.assertIn("Undone: move report.txt", second)
        self.assertEqual([], undo.history())

    def test_concurrent_undo_does_not_start_same_entry_twice(self):
        from core import undo

        undo.clear()
        import threading
        import time
        entered = threading.Event()
        release = threading.Event()
        calls = {"count": 0}

        def reverse():
            calls["count"] += 1
            entered.set()
            release.wait(1)
            return "restored"

        undo.push_undo("long operation", reverse)
        thread = threading.Thread(target=undo.undo_last)
        thread.start()
        self.assertTrue(entered.wait(1))
        duplicate = undo.undo_last()
        self.assertIn("already running", duplicate)
        release.set()
        thread.join(1)
        self.assertEqual(1, calls["count"])
        undo.clear()


class CategoryTimeoutTests(unittest.TestCase):
    def test_timeouts_are_bounded_by_operation_category(self):
        self.assertEqual(60, _validate_timeout(60, "desktop"))
        self.assertEqual(300, _validate_timeout(300, "project"))
        with self.assertRaises(ExecutionPolicyError):
            _validate_timeout(61, "desktop")
        with self.assertRaises(ExecutionPolicyError):
            _validate_timeout(301, "project")


if __name__ == "__main__":
    unittest.main()
