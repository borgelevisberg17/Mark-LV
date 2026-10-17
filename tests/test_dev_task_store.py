"""Persistence, recovery, and safe-boundary control for development tasks."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import dev_agent
from core import dev_task_store as tasks


class DevelopmentTaskStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._workspace_temp = tempfile.TemporaryDirectory(
            prefix=".task-test-", dir=Path(__file__).resolve().parents[1]
        )
        cls._previous_tempdir = tempfile.tempdir
        tempfile.tempdir = cls._workspace_temp.name

    @classmethod
    def tearDownClass(cls):
        tempfile.tempdir = cls._previous_tempdir
        cls._workspace_temp.cleanup()

    def _new(self):
        root = Path(tempfile.gettempdir()) / ("dev-task-" + next(tempfile._get_candidate_names()))
        root.mkdir()
        project = root / "project"
        project.mkdir()
        store = root / "store"
        return project, store

    def test_atomic_task_checkpoint_retries_transient_windows_replace_lock(self):
        project, store = self._new()
        target = store / "tasks" / "record.json"
        real_replace = tasks.os.replace
        attempts = 0

        def flaky_replace(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("transient file scanner lock")
            return real_replace(source, destination)

        with patch.object(tasks.os, "replace", side_effect=flaky_replace), \
             patch.object(tasks.time, "sleep"):
            tasks._atomic_json(target, {"status": "running"})
        self.assertEqual(attempts, 2)
        self.assertIn('"status": "running"', target.read_text(encoding="utf-8"))

    def test_checkpoint_roundtrip_is_bound_to_project_and_hash(self):
        project, store = self._new()
        task = tasks.create_task(project, store, goal="Build", language="python", project_name="app")
        tasks.update_task(task["task_id"], store, plan={"files": [{"path": "main.py"}]})
        tasks.save_generated_file(task["task_id"], store, "main.py", "print('ok')")
        self.assertEqual(tasks.load_generated_files(task["task_id"], store, project),
                         {"main.py": "print('ok')"})
        artifact = store / "task_artifacts" / task["task_id"] / "main.py"
        artifact.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "integrity"):
            tasks.load_generated_files(task["task_id"], store, project)
        other = project.parent / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "different project"):
            tasks.load_generated_files(task["task_id"], store, other)

    def test_restart_marks_running_task_interrupted_without_replay(self):
        project, store = self._new()
        task = tasks.create_task(project, store, goal="Build", language="python", project_name="app")
        task_path = str(store.resolve())
        tasks._RECOVERED.discard(task_path)
        recovered = tasks.get_task(task["task_id"], store)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["phase"], "recovery_required")
        self.assertEqual(recovered["generated"], {})

    def test_restart_after_approved_side_effect_requires_reconciliation(self):
        project, store = self._new()
        task = tasks.create_task(project, store, goal="Build", language="python", project_name="app")
        tasks.update_task(task["task_id"], store, phase="applying", status="running")
        tasks._RECOVERED.discard(str(store.resolve()))
        recovered = tasks.get_task(task["task_id"], store)
        self.assertEqual(recovered["status"], "unknown_after_restart")
        with self.assertRaisesRegex(ValueError, "outcome is uncertain"):
            tasks.control_task(task["task_id"], store, "resume")

    def test_pause_and_resume_are_honored_at_safe_checkpoint(self):
        project, store = self._new()
        task = tasks.create_task(project, store, goal="Build", language="python", project_name="app")
        tasks.control_task(task["task_id"], store, "pause")
        finished = threading.Event()

        def worker():
            tasks.checkpoint(task["task_id"], store, phase="generating", message="next file", poll_seconds=0.1)
            finished.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if tasks.get_task(task["task_id"], store)["status"] == "paused":
                break
            time.sleep(0.02)
        self.assertEqual(tasks.get_task(task["task_id"], store)["status"], "paused")
        tasks.control_task(task["task_id"], store, "resume")
        thread.join(timeout=2)
        self.assertTrue(finished.is_set())
        self.assertEqual(tasks.get_task(task["task_id"], store)["status"], "running")

    def test_cancel_is_applied_at_next_checkpoint(self):
        project, store = self._new()
        task = tasks.create_task(project, store, goal="Build", language="python", project_name="app")
        tasks.control_task(task["task_id"], store, "cancel")
        with self.assertRaises(tasks.TaskCancelled):
            tasks.checkpoint(task["task_id"], store, phase="generating", message="next file")
        self.assertEqual(tasks.get_task(task["task_id"], store)["status"], "cancelled")

    def test_development_action_exposes_task_list_and_status(self):
        root = Path(tempfile.gettempdir()) / ("dev-task-action-" + next(tempfile._get_candidate_names()))
        root.mkdir()
        projects = root / "Projects"
        projects.mkdir()
        project = projects / "app"
        project.mkdir()
        task = tasks.create_task(project, projects / ".dev_agent_context",
                                 goal="Build app", language="python", project_name="app")
        with patch.object(dev_agent, "PROJECTS_DIR", projects):
            status = dev_agent.dev_agent({"mode": "task_status", "task_id": task["task_id"]})
            task_list = dev_agent.dev_agent({"mode": "task_list"})
        self.assertIn("running / planning", status)
        self.assertIn(task["task_id"], task_list)

    def test_resume_mode_restarts_only_a_recoverable_checkpoint(self):
        root = Path(tempfile.gettempdir()) / ("dev-task-resume-" + next(tempfile._get_candidate_names()))
        root.mkdir()
        projects = root / "Projects"
        projects.mkdir()
        project = projects / "app"
        project.mkdir()
        store = projects / ".dev_agent_context"
        task = tasks.create_task(project, store, goal="Build app", language="python", project_name="app")
        tasks.update_task(task["task_id"], store, status="interrupted", phase="generation_incomplete")
        with patch.object(dev_agent, "PROJECTS_DIR", projects), \
             patch.object(dev_agent, "_build_project", return_value="resumed") as resume:
            result = dev_agent.dev_agent({"mode": "resume", "task_id": task["task_id"]})
        self.assertEqual(result, "resumed")
        self.assertEqual(resume.call_args.kwargs["task_id"], task["task_id"])


if __name__ == "__main__":
    unittest.main()
