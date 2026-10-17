"""Tests for explicit-consent DevOps workflows."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import devops


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True,
                   capture_output=True, shell=False)
    subprocess.run(["git", "config", "user.email", "marklv-tests@example.invalid"], cwd=root,
                   check=True, capture_output=True, shell=False)
    subprocess.run(["git", "config", "user.name", "Mark-LV Tests"], cwd=root,
                   check=True, capture_output=True, shell=False)
    (root / "app.py").write_text("print('ready')\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "app.py"], cwd=root, check=True,
                   capture_output=True, shell=False)
    subprocess.run(["git", "commit", "-m", "chore: initial"], cwd=root, check=True,
                   capture_output=True, shell=False)
    return root


class DevOpsActionTests(unittest.TestCase):
    def test_action_is_discovered_and_explicit_only_with_no_auto_action(self):
        self.assertEqual(devops.TOOL["name"], "devops")
        description = devops.TOOL["description"].lower()
        self.assertIn("only when the user explicitly asks", description)
        self.assertNotIn("auto", devops.TOOL["parameters"]["properties"]["action"]["description"].lower())

    def test_action_loader_registers_devops_tool(self):
        from core.action_loader import discover_actions

        actions_dir = Path(__file__).resolve().parents[1] / "actions"
        registry = discover_actions(actions_dir, logger=lambda _message: None)
        self.assertTrue(registry.has("devops"))

    def test_requested_repository_listing_does_not_require_a_local_clone(self):
        repos = [{"nameWithOwner": "octo/app", "isPrivate": True}]
        with patch("core.github_connection.list_repositories",
                   return_value=(True, "Connected as octo.", repos)):
            result = devops.devops({"action": "repositories"})
        self.assertIn("octo/app", result)
        self.assertIn("Repository metadata only", result)

    def test_audit_is_read_only_and_reports_git_state(self):
        with tempfile.TemporaryDirectory(prefix="marklv-devops-", ignore_cleanup_errors=True) as tmp:
            repo = make_repo(Path(tmp) / "repo")
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
            result = devops.devops({"action": "audit", "repo_path": str(repo)})
            self.assertIn("Branch: main", result)
            self.assertIn("app.py", result)
            self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "print('changed')\n")
            self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                            capture_output=True, text=True, shell=False).returncode, 0)

    def test_ci_setup_shows_approval_before_writing_and_pins_actions(self):
        with tempfile.TemporaryDirectory(prefix="marklv-devops-", ignore_cleanup_errors=True) as tmp:
            repo = make_repo(Path(tmp) / "repo")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_smoke.py").write_text("def test_smoke(): pass\n", encoding="utf-8")
            with patch("actions.devops.request_execution", return_value="pending") as request:
                result = devops.devops({"action": "setup_ci", "repo_path": str(repo), "language": "python"})
            self.assertEqual(result, "pending")
            path = repo / ".github" / "workflows" / "ci.yml"
            self.assertFalse(path.exists())
            detail = request.call_args.kwargs["detail"]
            self.assertIn("contents: read", detail)
            request.call_args.kwargs["operation"]()
            workflow = path.read_text(encoding="utf-8")
            self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", workflow)
            self.assertIn("actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97", workflow)
            self.assertIn("persist-credentials: false", workflow)

    def test_node_ci_requires_lockfile(self):
        with tempfile.TemporaryDirectory(prefix="marklv-devops-", ignore_cleanup_errors=True) as tmp:
            repo = make_repo(Path(tmp) / "repo")
            (repo / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8")
            with patch("actions.devops.request_execution") as request:
                result = devops.devops({"action": "setup_ci", "repo_path": str(repo), "language": "node"})
            self.assertIn("package-lock.json", result)
            request.assert_not_called()

    def test_verify_fails_closed_when_docker_is_unavailable(self):
        with tempfile.TemporaryDirectory(prefix="marklv-devops-", ignore_cleanup_errors=True) as tmp:
            repo = make_repo(Path(tmp) / "repo")
            with patch("actions.devops.docker_sandbox_status", return_value=(False, "daemon stopped")), \
                 patch("actions.devops.request_execution") as request:
                result = devops.devops({"action": "verify", "repo_path": str(repo),
                                        "command": "python -m unittest discover", "language": "python"})
            self.assertIn("nothing was executed", result)
            request.assert_not_called()

    def test_branch_creation_requires_clean_tree_and_confirmation(self):
        with tempfile.TemporaryDirectory(prefix="marklv-devops-", ignore_cleanup_errors=True) as tmp:
            repo = make_repo(Path(tmp) / "repo")
            with patch("actions.devops.request_execution", return_value="pending") as request:
                result = devops.devops({"action": "branch", "branch": "feature/login",
                                        "repo_path": str(repo)})
            self.assertIn("pending", result)
            self.assertFalse(subprocess.run(["git", "show-ref", "--verify", "refs/heads/feature/login"],
                                            cwd=repo, capture_output=True, shell=False).returncode == 0)
            request.call_args.kwargs["operation"]()
            self.assertEqual(subprocess.run(["git", "branch", "--show-current"], cwd=repo,
                                            capture_output=True, text=True, shell=False).stdout.strip(),
                             "feature/login")

    def test_commit_only_commits_the_explicit_file_selection(self):
        with tempfile.TemporaryDirectory(prefix="marklv-devops-", ignore_cleanup_errors=True) as tmp:
            repo = make_repo(Path(tmp) / "repo")
            subprocess.run(["git", "switch", "-c", "feature/change"], cwd=repo, check=True,
                           capture_output=True, shell=False)
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
            (repo / "notes.txt").write_text("keep out of this commit\n", encoding="utf-8")
            with patch("actions.devops.request_execution", return_value="pending") as request:
                result = devops.devops({"action": "commit", "repo_path": str(repo),
                                        "files": ["app.py"], "message": "fix: update greeting"})
            self.assertIn("pending", result)
            self.assertIn("Other changed files remain unstaged", request.call_args.kwargs["detail"])
            request.call_args.kwargs["operation"]()
            committed = subprocess.run(["git", "show", "--pretty=format:", "--name-only", "HEAD"],
                                       cwd=repo, capture_output=True, text=True, shell=False).stdout
            self.assertIn("app.py", committed)
            self.assertNotIn("notes.txt", committed)
            self.assertIn("notes.txt", subprocess.run(["git", "status", "--short"], cwd=repo,
                                                       capture_output=True, text=True, shell=False).stdout)

    def test_release_refuses_dirty_tree_and_invalid_semver(self):
        with tempfile.TemporaryDirectory(prefix="marklv-devops-", ignore_cleanup_errors=True) as tmp:
            repo = make_repo(Path(tmp) / "repo")
            (repo / "app.py").write_text("dirty\n", encoding="utf-8")
            with patch("actions.devops.request_execution") as request:
                dirty = devops.devops({"action": "release", "repo_path": str(repo), "version": "1.2.0"})
                invalid = devops.devops({"action": "release", "repo_path": str(repo), "version": "latest"})
            self.assertIn("clean working tree", dirty)
            self.assertIn("SemVer", invalid)
            request.assert_not_called()

    def test_commit_on_primary_branch_is_blocked(self):
        with tempfile.TemporaryDirectory(prefix="marklv-devops-", ignore_cleanup_errors=True) as tmp:
            repo = make_repo(Path(tmp) / "repo")
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
            with patch("actions.devops.request_execution") as request:
                result = devops.devops({"action": "commit", "repo_path": str(repo),
                                        "files": ["app.py"], "message": "fix: change"})
            self.assertIn("direct commits to the primary branch are blocked", result)
            request.assert_not_called()

    def test_unsafe_branch_and_path_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="marklv-devops-", ignore_cleanup_errors=True) as tmp:
            repo = make_repo(Path(tmp) / "repo")
            self.assertIn("Branch must follow", devops.devops({"action": "branch", "branch": "../main",
                                                               "repo_path": str(repo)}))
            with patch("actions.devops.request_execution") as request:
                result = devops.devops({"action": "commit", "repo_path": str(repo),
                                        "files": ["../secrets.txt"], "message": "fix: test"})
            self.assertIn("Unsafe repository path", result)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
