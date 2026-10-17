"""Tests for live control of the document already open in Microsoft Word."""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import office_editor


class _FakeWordDocument:
    def __init__(self, path):
        self.FullName = str(path)
        self.Name = path.name
        self.ReadOnly = False

    def SaveAs2(self, FileName):
        target = Path(FileName)
        target.write_text("updated document", encoding="utf-8")
        self.FullName = str(target)
        self.Name = target.name


class _FakeWordApplication:
    def __init__(self, document):
        self.ActiveDocument = document


class ActiveWordDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.docs = self.root / "Documents"
        self.docs.mkdir()
        self.source = self.docs / "Draft.docx"
        self.source.write_text("original document", encoding="utf-8")
        self.document = _FakeWordDocument(self.source)
        self.client = types.ModuleType("win32com.client")
        self.client.GetActiveObject = lambda name: _FakeWordApplication(self.document)
        self.win32com = types.ModuleType("win32com")
        self.win32com.client = self.client
        self.modules_patch = patch.dict(
            sys.modules,
            {"win32com": self.win32com, "win32com.client": self.client},
        )
        self.modules_patch.start()
        self.addCleanup(self.modules_patch.stop)
        self.addCleanup(self.temp.cleanup)

    def test_rename_uses_active_word_instance_and_keeps_target_open(self):
        with patch("platform.system", return_value="Windows"), patch(
            "actions.office_editor.user_accessible_roots", return_value=(self.root,)
        ):
            result = office_editor.office_editor({
                "operation": "active_word_rename_document",
                "filename": "Final draft",
            })

        target = self.docs / "Final draft.docx"
        self.assertIn(str(target), result)
        self.assertTrue(target.is_file())
        self.assertFalse(self.source.exists())
        self.assertEqual(str(target), self.document.FullName)
        self.assertNotIn("closed", result.lower())

    def test_rename_refuses_to_overwrite_existing_target(self):
        target = self.docs / "Final draft.docx"
        target.write_text("keep this file", encoding="utf-8")
        with patch("platform.system", return_value="Windows"), patch(
            "actions.office_editor.user_accessible_roots", return_value=(self.root,)
        ):
            result = office_editor.office_editor({
                "operation": "active_word_rename_document",
                "filename": "Final draft",
            })
        self.assertIn("Refusing to overwrite", result)
        self.assertEqual("keep this file", target.read_text(encoding="utf-8"))
        self.assertTrue(self.source.exists())

    def test_live_word_action_is_explicitly_registered(self):
        self.assertIn(
            "active_word_rename_document",
            office_editor.TOOL["parameters"]["properties"]["operation"]["description"],
        )
        self.assertIn("active_word_replace_text", office_editor.TOOL["description"])


if __name__ == "__main__":
    unittest.main()
