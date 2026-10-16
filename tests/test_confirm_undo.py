import threading
import unittest

from core import confirm, undo


class ConfirmationAndUndoTests(unittest.TestCase):
    def tearDown(self):
        confirm.resolve(False)
        confirm.bind(None, None)
        undo.clear()

    def test_irreversible_action_refuses_without_ui(self):
        confirm.bind(None, None)
        ran = []
        result = confirm.request("shutdown", "Shut down", "Power off", lambda: ran.append(True))
        self.assertIn("interface is not available", result)
        self.assertEqual(ran, [])

    def test_confirmation_request_does_not_run_action_immediately(self):
        shown = []
        hidden = []
        confirm.bind(lambda title, detail: shown.append((title, detail)),
                     lambda: hidden.append(True))
        ran = []
        result = confirm.request("restart", "Restart", "Restart computer",
                                 lambda: ran.append(True) or "done")
        self.assertIn("CONFIRMATION_PENDING", result)
        self.assertEqual(shown, [("Restart", "Restart computer")])
        self.assertEqual(ran, [])
        confirm.resolve(False)
        self.assertEqual(ran, [])
        self.assertTrue(hidden)

    def test_second_confirmation_cannot_replace_pending_action(self):
        shown = []
        confirm.bind(lambda title, detail: shown.append(title), lambda: None)
        first_ran = []
        second_ran = []
        first = confirm.request("first", "First action", "First detail",
                                lambda: first_ran.append(True) or "done")
        second = confirm.request("second", "Second action", "Second detail",
                                 lambda: second_ran.append(True) or "done")
        self.assertIn("CONFIRMATION_PENDING", first)
        self.assertIn("already a confirmation waiting", second)
        self.assertEqual(shown, ["First action"])
        confirm.resolve(False)
        self.assertEqual(first_ran, [])
        self.assertEqual(second_ran, [])

    def test_undo_reverses_latest_registered_operation(self):
        state = {"value": 1}
        state["value"] = 2
        undo.push_undo("set value to 2", lambda: state.update(value=1) or "restored")
        self.assertEqual(undo.peek(), "set value to 2")
        result = undo.undo_last()
        self.assertEqual(state["value"], 1)
        self.assertIn("Undone:", result)
        self.assertFalse(undo.can_undo())

    def test_undo_empty_stack_is_safe(self):
        undo.clear()
        self.assertIn("nothing to undo", undo.undo_last().lower())


if __name__ == "__main__":
    unittest.main()
