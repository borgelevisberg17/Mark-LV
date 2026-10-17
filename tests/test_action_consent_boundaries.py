"""Approval boundaries for side-effecting built-in actions."""
import unittest
from unittest.mock import patch


class ActionConsentBoundaryTests(unittest.TestCase):
    def test_file_mutation_is_queued_without_exposing_content_or_running(self):
        from actions import file_controller

        with patch("core.execution.request_execution", return_value="pending") as request:
            result = file_controller.file_controller({
                "action": "write", "path": "documents", "name": "notes.txt",
                "content": "private text",
            })

        self.assertEqual(result, "pending")
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["category"], "filesystem")
        self.assertIn("filesystem.write", kwargs["permissions"])
        self.assertIn("content omitted", kwargs["detail"])
        self.assertNotIn("private text", kwargs["detail"])
        self.assertTrue(callable(kwargs["operation"]))

    def test_read_only_file_action_does_not_request_approval(self):
        from actions import file_controller

        with patch("core.execution.request_execution") as request, \
             patch.object(file_controller, "_file_controller_execute", return_value="listed"):
            result = file_controller.file_controller({"action": "list", "path": "desktop"})

        self.assertEqual(result, "listed")
        request.assert_not_called()

    def test_game_install_is_queued_and_same_payload_is_fingerprinted(self):
        from actions import game_updater

        with patch("core.execution.request_execution", return_value="pending") as request:
            result = game_updater.game_updater({
                "action": "install", "platform": "steam", "game_name": "Example",
            })

        self.assertEqual(result, "pending")
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["category"], "general")
        self.assertIn("game: Example", kwargs["detail"])
        self.assertEqual(len(kwargs["fingerprint"]), 64)
        self.assertTrue(callable(kwargs["operation"]))

    def test_game_schedule_rejects_out_of_range_time_before_side_effect(self):
        from actions import game_updater

        with patch("core.execution.request_execution") as request:
            result = game_updater.game_updater({"action": "schedule", "hour": 24})

        self.assertIn("hour must be 0–23", result)
        request.assert_not_called()

    def test_game_list_remains_read_only_and_immediate(self):
        from actions import game_updater

        with patch("core.execution.request_execution") as request, \
             patch.object(game_updater, "_game_updater_execute", return_value="listed"):
            result = game_updater.game_updater({"action": "list", "platform": "steam"})

        self.assertEqual(result, "listed")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
