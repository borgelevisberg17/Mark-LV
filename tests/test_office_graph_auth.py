"""Regression tests for Microsoft Graph device-code authentication helpers."""
import unittest
from unittest.mock import Mock, patch

from actions import office_graph


class GraphAuthRegressionTests(unittest.TestCase):
    def test_poll_login_uses_lazy_requests_loader_and_stores_tokens(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "access_token": "access-test",
            "refresh_token": "refresh-test",
            "expires_in": 3600,
            "scope": "User.Read offline_access",
        }
        http = Mock()
        http.post.return_value = response
        flow = {
            "device_code": "device-test",
            "expires_at": 9999999999,
            "interval": 5,
            "scopes": ["User.Read", "offline_access"],
        }
        with (
            patch.object(office_graph, "_PENDING_FLOW", flow),
            patch.object(office_graph, "_client_id", return_value="public-client-id"),
            patch.object(office_graph, "_requests", return_value=http),
            patch.object(office_graph, "_save_cache") as save_cache,
        ):
            result = office_graph._poll_login()

        self.assertIn("account connected", result)
        self.assertEqual("access-test", save_cache.call_args.args[0]["access_token"])
        self.assertEqual("refresh-test", save_cache.call_args.args[0]["refresh_token"])
        self.assertIsNone(office_graph._PENDING_FLOW)
        http.post.assert_called_once()

    def test_expired_access_token_refresh_uses_lazy_requests_loader(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "scope": "User.Read offline_access",
        }
        http = Mock()
        http.post.return_value = response
        cache = {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 0,
            "scopes": ["User.Read"],
        }
        with (
            patch.object(office_graph, "_load_cache", return_value=cache),
            patch.object(office_graph, "_client_id", return_value="public-client-id"),
            patch.object(office_graph, "_requests", return_value=http),
            patch.object(office_graph, "_save_cache") as save_cache,
        ):
            token, error = office_graph._access_token({"User.Read"})

        self.assertEqual("new-access", token)
        self.assertEqual("", error)
        self.assertEqual("new-refresh", save_cache.call_args.args[0]["refresh_token"])
        http.post.assert_called_once()

    def test_remote_path_rejects_absolute_and_traversal_paths(self):
        for value in ("/folder/file.docx", r"\\server\share\file.docx", r"C:\folder\file.docx", "../file.docx", "folder/../file.docx", "."):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    office_graph._safe_remote_path(value)

    def test_remote_path_accepts_relative_path(self):
        self.assertEqual("Reports/October.docx", office_graph._safe_remote_path("Reports/October.docx"))


if __name__ == "__main__":
    unittest.main()
