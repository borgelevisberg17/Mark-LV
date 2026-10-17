"""UI GitHub connection, repository browsing, and clone-consent tests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import github_connection


class GitHubConnectionTests(unittest.TestCase):
    def test_settings_expose_connect_clone_and_disconnect_without_a_token_field(self):
        schema = github_connection.settings_schema()
        labels = [item["label"] for item in schema["actions"]]
        keys = [field["key"] for field in schema["fields"]]
        self.assertIn("CONNECT / REFRESH REPOSITORIES", labels)
        self.assertIn("CLONE SELECTED REPOSITORY", labels)
        self.assertIn("DISCONNECT GITHUB", labels)
        self.assertEqual(keys, ["repository", "clone_folder"])
        self.assertTrue(all("token" not in key.lower() for key in keys))

    def test_list_repositories_returns_metadata_only(self):
        payload = [{"nameWithOwner": "octo/example", "isPrivate": True,
                    "updatedAt": "2026-10-17T00:00:00Z"}]
        with patch.object(github_connection, "connection_status", return_value=(True, "Connected as octo.")), \
             patch.object(github_connection, "_run_gh",
                          return_value="Process exited with code 0.\n" + json.dumps(payload)) as run:
            ok, status, repos = github_connection.list_repositories()
        self.assertTrue(ok)
        self.assertIn("octo", status)
        self.assertEqual(repos[0]["nameWithOwner"], "octo/example")
        self.assertIn("--json", run.call_args.args[0])

    def test_clone_shows_exact_confirmation_and_does_not_write_first(self):
        with tempfile.TemporaryDirectory(prefix="marklv-github-", ignore_cleanup_errors=True,
                                         dir=Path(__file__).resolve().parents[1]) as temp:
            parent = Path(temp) / "repos"
            values = {"repository": "octo/example", "clone_folder": str(parent)}
            with patch.object(github_connection, "connection_status", return_value=(True, "Connected as octo.")), \
                 patch("core.github_connection.request_execution", return_value="[CONFIRMATION_PENDING]") as request:
                ok, message = github_connection.clone_from_settings(values)
            self.assertTrue(ok)
            self.assertIn("CONFIRMATION_PENDING", message)
            self.assertFalse(parent.exists())
            detail = request.call_args.kwargs["detail"]
            self.assertIn("octo/example", detail)
            self.assertIn(str(parent / "example"), detail)

    def test_invalid_repository_and_out_of_home_destination_are_rejected(self):
        with patch.object(github_connection, "connection_status", return_value=(True, "Connected")), \
             patch("core.github_connection.request_execution") as request:
            invalid = github_connection.clone_from_settings({"repository": "octo/..", "clone_folder": str(Path.home())})
            outside = github_connection.clone_from_settings({"repository": "octo/example", "clone_folder": str(Path.home().parent)})
        self.assertFalse(invalid[0])
        self.assertIn("owner/name", invalid[1])
        self.assertFalse(outside[0])
        request.assert_not_called()

    def test_disconnect_is_confirmation_gated(self):
        with patch.object(github_connection, "connection_status", return_value=(True, "Connected as octo.")), \
             patch("core.github_connection.request_execution", return_value="[CONFIRMATION_PENDING]") as request:
            ok, message = github_connection.disconnect()
        self.assertTrue(ok)
        self.assertIn("CONFIRMATION_PENDING", message)
        self.assertIn("affects other local projects", request.call_args.kwargs["detail"])


if __name__ == "__main__":
    unittest.main()
