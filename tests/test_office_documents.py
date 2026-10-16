"""Regression tests for opening locally created Office documents."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from actions import office_documents


def make_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        book.writestr(
            "OEBPS/chapter.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Interseção no Oceano</title></head>'
            '<body><h1>Interseção no Oceano</h1><p>Primeiro capítulo de teste.</p></body></html>',
        )


class OfficeDocumentOpenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docs = self.root / "Documents" / "MarkLV"
        self.docs.mkdir(parents=True)
        self.patch_home = patch.object(Path, "home", return_value=self.root)
        self.patch_home.start()
        self.addCleanup(self.patch_home.stop)
        self.patch_roots = patch("core.path_policy.user_accessible_roots", return_value=(self.root,))
        self.patch_roots.start()
        self.addCleanup(self.patch_roots.stop)
        self.patch_common_roots = patch("actions._office_common.user_accessible_roots", return_value=(self.root,))
        self.patch_common_roots.start()
        self.addCleanup(self.patch_common_roots.stop)
        self.patch_action_roots = patch("actions.office_documents.user_accessible_roots", return_value=(self.root,))
        self.patch_action_roots.start()
        self.addCleanup(self.patch_action_roots.stop)

    def test_opens_exact_existing_file_without_keyboard_simulation(self):
        path = self.docs / "ebook_oceano.epub"
        make_epub(path)
        with patch("actions.office_documents.platform.system", return_value="Windows"):
            with patch("actions.office_documents.os.startfile", create=True) as startfile:
                result = office_documents.office_documents({
                    "operation": "open_document",
                    "file_path": str(path),
                })
        self.assertIn(str(path), result)
        startfile.assert_called_once_with(str(path))

    def test_finds_created_document_by_filename_in_marklv_folder(self):
        path = self.docs / "ebook_oceano.epub"
        make_epub(path)
        with patch("actions.office_documents.platform.system", return_value="Windows"):
            with patch("actions.office_documents.os.startfile", create=True) as startfile:
                result = office_documents.office_documents({
                    "operation": "open_document",
                    "filename": "ebook_oceano.epub",
                })
        self.assertIn(str(path), result)
        startfile.assert_called_once_with(str(path))

    def test_word_request_converts_epub_to_docx_and_opens_the_copy(self):
        try:
            import docx  # noqa: F401
        except ImportError:
            self.skipTest("python-docx is installed in the Office artifact test environment")
        path = self.docs / "ebook_oceano.epub"
        make_epub(path)
        with patch("actions.office_documents.platform.system", return_value="Windows"):
            with patch("actions.office_documents.os.startfile", create=True) as startfile:
                result = office_documents.office_documents({
                    "operation": "open_document",
                    "file_path": str(path),
                    "app_name": "word",
                })
        converted = self.docs / "ebook_oceano.docx"
        self.assertTrue(converted.is_file())
        self.assertTrue(path.is_file(), "The original EPUB must be preserved")
        self.assertIn("Created an editable Word copy", result)
        startfile.assert_called_once_with(str(converted))


if __name__ == "__main__":
    unittest.main()
