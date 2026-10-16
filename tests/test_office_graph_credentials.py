"""Regression tests for Microsoft Graph credential storage limits and recovery."""
import json
import unittest
from unittest.mock import patch

from actions import office_graph


class FakeWindowsCredentialStore:
    """Emulate WinCred's 2,560-byte blob ceiling using UTF-16LE secrets."""

    MAX_BLOB_BYTES = 2560

    def __init__(self):
        self.values = {}
        self.fail_target_suffix = None

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, secret):
        if len(secret.encode("utf-16-le")) > self.MAX_BLOB_BYTES:
            raise OSError(1783, "CredWrite: O stub recebeu dados incorretos")
        if self.fail_target_suffix and username.endswith(self.fail_target_suffix):
            self.fail_target_suffix = None
            raise OSError(1783, "simulated WinCred write failure")
        self.values[(service, username)] = secret

    def delete_password(self, service, username):
        key = (service, username)
        if key not in self.values:
            raise KeyError(username)
        del self.values[key]


class MicrosoftCredentialStorageTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeWindowsCredentialStore()
        self.patch_keyring = patch.object(office_graph, "_keyring", return_value=self.store)
        self.patch_keyring.start()
        self.addCleanup(self.patch_keyring.stop)

    def test_large_token_cache_round_trips_under_windows_blob_limit(self):
        cache = {
            "access_token": "A" * 4200,
            "refresh_token": "R" * 6200,
            "expires_at": 1234567890,
            "scope": "User.Read Files.ReadWrite Calendars.ReadWrite",
            "scopes": ["user.read", "files.readwrite", "calendars.readwrite"],
            "upload_sessions": {
                "upload-" + str(i): {
                    "source_path": "C:/Users/test/Documents/report-" + str(i) + ".docx",
                    "remote_path": "Documents/report-" + str(i) + ".docx",
                    "upload_url": "https://upload.example/" + ("x" * 300),
                    "size": i * 1024,
                    "mtime_ns": i * 100,
                    "nextExpectedRanges": ["0-"],
                }
                for i in range(8)
            },
        }

        office_graph._save_cache(cache)

        self.assertEqual(office_graph._load_cache(), cache)
        for (service, username), secret in self.store.values.items():
            if service == office_graph._CREDENTIAL_SERVICE:
                self.assertLessEqual(
                    len(secret.encode("utf-16-le")),
                    self.store.MAX_BLOB_BYTES,
                    msg=f"credential {username!r} exceeds WinCred's blob limit",
                )
        manifest = json.loads(
            self.store.get_password(
                office_graph._CREDENTIAL_SERVICE,
                office_graph._CREDENTIAL_USERNAME,
            )
        )
        self.assertEqual(manifest["_marklv_chunked"], 1)
        self.assertGreater(manifest["chunks"], 1)

    def test_legacy_single_entry_cache_remains_readable(self):
        legacy = {"access_token": "old-access", "refresh_token": "old-refresh", "scopes": ["user.read"]}
        self.store.set_password(
            office_graph._CREDENTIAL_SERVICE,
            office_graph._CREDENTIAL_USERNAME,
            json.dumps(legacy, separators=(",", ":")),
        )
        self.assertEqual(office_graph._load_cache(), legacy)

    def test_failed_new_generation_preserves_committed_cache(self):
        previous = {"access_token": "previous", "refresh_token": "previous-refresh"}
        office_graph._save_cache(previous)
        committed_manifest = self.store.get_password(
            office_graph._CREDENTIAL_SERVICE,
            office_graph._CREDENTIAL_USERNAME,
        )

        # Fail on the second chunk of a different generation.
        self.store.fail_target_suffix = ":1"
        candidate = {"access_token": "N" * 3000, "refresh_token": "new-refresh"}
        with self.assertRaisesRegex(RuntimeError, "securely save"):
            office_graph._save_cache(candidate)

        self.assertEqual(
            self.store.get_password(
                office_graph._CREDENTIAL_SERVICE,
                office_graph._CREDENTIAL_USERNAME,
            ),
            committed_manifest,
        )
        self.assertEqual(office_graph._load_cache(), previous)


    def test_token_storage_failure_ends_consumed_device_flow_without_plaintext_fallback(self):
        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "access_token": "secret-access-token",
                    "refresh_token": "secret-refresh-token",
                    "expires_in": 3600,
                    "scope": "User.Read offline_access",
                }

        class Requests:
            @staticmethod
            def post(*args, **kwargs):
                return Response()

        office_graph._PENDING_FLOW = {
            "device_code": "consumed-device-code",
            "interval": 5,
            "expires_at": 9999999999,
            "scopes": ["User.Read", "offline_access"],
        }
        with patch.object(office_graph, "_requests", return_value=Requests):
            with patch.object(office_graph, "_client_id", return_value="test-client-id"):
                with patch.object(
                    office_graph,
                    "_save_cache",
                    side_effect=OSError(1783, "CredWrite rejected credential blob"),
                ) as save_cache:
                    result = office_graph._poll_login()

        self.assertIn("could not securely save the tokens", result)
        self.assertIn("No plaintext fallback was used", result)
        self.assertNotIn("secret-access-token", result)
        self.assertIsNone(office_graph._PENDING_FLOW)
        save_cache.assert_called_once()

    def test_chunk_size_is_below_conservative_utf16_byte_ceiling(self):
        self.assertLessEqual(
            office_graph._CREDENTIAL_CHUNK_CHARS * 2,
            office_graph._CREDENTIAL_CHUNK_MAX_BYTES,
        )
        self.assertLess(office_graph._CREDENTIAL_CHUNK_MAX_BYTES, 2560)


if __name__ == "__main__":
    unittest.main()
