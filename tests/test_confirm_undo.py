import threading
import unittest
import uuid

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

    def test_voice_confirmation_requires_explicit_phrase(self):
        shown = []
        completed = threading.Event()
        confirm.bind(lambda title, detail: shown.append(title), lambda: None)
        result = confirm.request(
            "voice-explicit-approval-" + uuid.uuid4().hex, "Write file", "Save a new file",
            lambda: completed.set() or "saved",
        )
        self.assertIn("CONFIRMATION_PENDING", result)
        self.assertFalse(confirm.resolve_voice("sim"))
        self.assertFalse(confirm.resolve_voice("pode fazer o jantar"))
        self.assertTrue(confirm.resolve_voice("Jarvis, autorizo, por favor"))
        self.assertTrue(completed.wait(2))
        self.assertEqual(shown, ["Write file"])

    def test_voice_rejection_cancels_pending_action(self):
        ran = []
        confirm.bind(lambda _title, _detail: None, lambda: None)
        confirm.request("voice-explicit-rejection", "Send", "Send message",
                        lambda: ran.append(True) or "sent")
        self.assertTrue(confirm.resolve_voice("não confirmo"))
        self.assertEqual(ran, [])

    def test_operation_dedupes_same_tool_call_but_allows_a_new_call(self):
        base_key = "repeatable-operation-" + uuid.uuid4().hex
        completed = threading.Event()
        shown = []
        confirm.bind(lambda title, _detail: shown.append(title), lambda: None)
        with confirm.invocation_scope("tool-call-1"):
            first = confirm.request(base_key, "Write file", "Save it",
                                    lambda: completed.set() or "saved")
        self.assertIn("CONFIRMATION_PENDING", first)
        confirm.resolve(True)
        self.assertTrue(completed.wait(2))

        with confirm.invocation_scope("tool-call-1"):
            duplicate = confirm.request(base_key, "Write file", "Save it", lambda: "duplicate")
        self.assertIn("status=completed", duplicate)
        with confirm.invocation_scope("tool-call-2"):
            deliberate_repeat = confirm.request(base_key, "Write file", "Save it", lambda: "saved again")
        self.assertIn("CONFIRMATION_PENDING", deliberate_repeat)
        self.assertEqual(shown, ["Write file", "Write file"])
        confirm.resolve(False)

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
