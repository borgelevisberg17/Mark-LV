"""Dependency-light contract and path-safety tests for the Office action suite."""
import tempfile
import unittest
from pathlib import Path

from actions._office_common import safe_filename, safe_output_directory, unique_output_path
from core.path_policy import PathPolicyError


class OfficeActionContractTests(unittest.TestCase):
    def test_office_actions_declare_unique_valid_tools(self):
        from core.action_loader import _validate
        from actions import office_documents, office_spreadsheets, office_presentations, image_tools, document_conversion

        modules = [
            office_documents, office_spreadsheets, office_presentations,
            image_tools, document_conversion,
        ]
        names = set()
        for module in modules:
            record = _validate(module, module.__name__.rsplit(".", 1)[-1] + ".py")
            self.assertTrue(record.valid, record.error)
            self.assertTrue(record.name.startswith(("office_", "image_", "document_")))
            self.assertNotIn(record.name, names)
            names.add(record.name)
            self.assertTrue(callable(record.handler))
        self.assertEqual(5, len(names))

    def test_safe_filename_rejects_paths_and_traversal(self):
        for unsafe in ("../report", r"..\report", r"C:\report", "/tmp/report"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(PathPolicyError):
                    safe_filename(unsafe, "report", ".docx")

    def test_output_paths_are_unique_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = safe_output_directory(temp)
            first = unique_output_path(root, "report.docx")
            first.write_text("original", encoding="utf-8")
            second = unique_output_path(root, "report.docx")
            self.assertNotEqual(first, second)
            self.assertEqual("original", first.read_text(encoding="utf-8"))

    def test_handler_returns_clear_error_for_unsupported_operation(self):
        from actions.office_documents import office_documents
        result = office_documents({"operation": "delete_all"})
        self.assertIn("Unsupported operation", result)


if __name__ == "__main__":
    unittest.main()
