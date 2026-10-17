"""Code-helper writes and execution are gated by explicit UI approval."""
import unittest
from unittest.mock import patch

from actions.code_helper import code_helper


class CodeHelperConsentTests(unittest.TestCase):
    def test_write_only_runs_after_execution_gate_invokes_callback(self):
        captured = {}

        def request_execution(**kwargs):
            captured.update(kwargs)
            return "[CONFIRMATION_PENDING]"

        with patch("core.execution.request_execution", side_effect=request_execution), \
             patch("actions.code_helper._write_action") as write:
            result = code_helper({"action": "write", "description": "Make a small utility"})

        self.assertIn("CONFIRMATION_PENDING", result)
        self.assertEqual(captured["title"], "Approve coding action: write")
        write.assert_not_called()

    def test_explain_remains_read_only_and_does_not_request_approval(self):
        with patch("actions.code_helper._explain_action", return_value="Explanation") as explain, \
             patch("core.execution.request_execution") as approval:
            result = code_helper({"action": "explain", "code": "print(1)"})

        self.assertEqual(result, "Explanation")
        explain.assert_called_once()
        approval.assert_not_called()


if __name__ == "__main__":
    unittest.main()
