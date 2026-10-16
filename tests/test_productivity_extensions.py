"""Regression tests for the Office productivity extension actions."""
from __future__ import annotations

import unittest

from actions.document_conversion import document_conversion
from actions.office_graph import _safe_remote_path, _session_offset


class DocumentConversionExtensionTests(unittest.TestCase):
    def test_docx_conversion_requires_source(self):
        result = document_conversion({"operation": "docx_to_pdf"})
        self.assertIn("file_path is required", result)

    def test_ocr_requires_source(self):
        result = document_conversion({"operation": "ocr_pdf"})
        self.assertIn("file_path is required", result)

    def test_unknown_conversion_operation_is_explicit(self):
        result = document_conversion({"operation": "magic"})
        self.assertIn("Unsupported operation", result)


class ResumableUploadHelperTests(unittest.TestCase):
    def test_upload_session_offset(self):
        self.assertEqual(_session_offset({"nextExpectedRanges": ["5242880-"]}), 5242880)
        self.assertEqual(_session_offset({"nextExpectedRanges": ["0-"]}), 0)
        self.assertEqual(_session_offset({"nextExpectedRanges": ["invalid"]}), 0)
        self.assertEqual(_session_offset({}), 0)

    def test_remote_path_rejects_traversal_and_absolute_paths(self):
        for path in ("../secret.txt", "folder/../secret.txt", "/absolute.txt", r"C:\secret.txt", ""):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    _safe_remote_path(path)

    def test_remote_path_accepts_relative_paths(self):
        self.assertEqual(_safe_remote_path("Reports/October.xlsx"), "Reports/October.xlsx")
        self.assertEqual(_safe_remote_path(r"Reports\October.xlsx"), r"Reports\October.xlsx")


if __name__ == "__main__":
    unittest.main()
