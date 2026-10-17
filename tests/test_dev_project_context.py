"""Persistence and project-isolation tests for development-agent context."""
import tempfile
import unittest
from pathlib import Path

from core.dev_project_context import load_project_context, project_identity, save_project_context


class DevProjectContextTests(unittest.TestCase):
    def test_context_and_history_are_isolated_by_canonical_project(self):
        with tempfile.TemporaryDirectory(
            prefix=".context-test-", dir=Path(__file__).resolve().parents[1]
        ) as temp:
            root = Path(temp)
            store = root / "state"
            project_a = root / "a"
            project_b = root / "b"
            project_a.mkdir()
            project_b.mkdir()
            id_a, _ = project_identity(project_a)
            id_b, _ = project_identity(project_b)
            self.assertNotEqual(id_a, id_b)

            save_project_context(
                project_a, store, goal="Build the dashboard", language="python",
                architecture="modular monolith", acceptance_criteria=["login works"],
                verification_command="python -m unittest", status="verified",
                summary="all tests passed",
            )
            a = load_project_context(project_a, store)
            b = load_project_context(project_b, store)
            self.assertEqual(a["goals"], ["Build the dashboard"])
            self.assertEqual(a["history"][0]["status"], "verified")
            self.assertEqual(b["history"], [])
            self.assertEqual(b["project_id"], id_b)


if __name__ == "__main__":
    unittest.main()
