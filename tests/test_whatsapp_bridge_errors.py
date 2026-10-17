"""Regression tests for actionable WhatsApp Edge Bridge errors."""
import json
import sys
import types
import unittest
from unittest.mock import patch

from actions.whatsapp import _call, whatsapp


class WhatsAppBridgeErrorTests(unittest.TestCase):
    def test_plain_bridge_error_is_preserved(self):
        bridge = types.ModuleType("actions.browser_control")
        bridge._try_existing_edge = lambda *_args, **_kwargs: (
            "Open WhatsApp Web in the active Edge tab first."
        )
        with patch.dict(sys.modules, {"actions.browser_control": bridge}):
            with self.assertRaisesRegex(RuntimeError, "Open WhatsApp Web"):
                _call({"operation": "read_messages", "contact": "Moisés"})

    def test_disconnected_status_has_actionable_message(self):
        with patch("actions.whatsapp._call", return_value={
            "connected": False,
            "title": "WhatsApp",
        }):
            result = json.loads(whatsapp({"operation": "status"}))
        self.assertFalse(result["connected"])
        self.assertIn("Complete sign-in", result["message"])


if __name__ == "__main__":
    unittest.main()
