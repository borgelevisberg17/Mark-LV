"""Tests for visual generation extraction and local Office insertion."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from actions.visual_studio import _extract_image, _insert_docx, _insert_pptx, visual_studio


class VisualStudioUnitTests(unittest.TestCase):
    def test_extract_image_ignores_text_and_returns_image_bytes(self):
        response = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
            SimpleNamespace(text="Here is your image."),
            SimpleNamespace(inline_data=SimpleNamespace(data=b"png-bytes", mime_type="image/png")),
        ]))])
        self.assertEqual(_extract_image(response), (b"png-bytes", "image/png"))

    def test_extract_image_fails_when_model_returns_no_image(self):
        response = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
            SimpleNamespace(text="I cannot generate that image.")
        ]))])
        with self.assertRaisesRegex(ValueError, "no usable image"):
            _extract_image(response)

    def test_invalid_operation_is_rejected_without_network(self):
        self.assertIn("Unsupported operation", visual_studio({"operation": "delete"}))


@unittest.skipUnless(
    all(importlib.util.find_spec(name) for name in ("PIL", "docx", "pptx")),
    "Office artifact libraries are not installed",
)
class VisualStudioOfficeInsertionTests(unittest.TestCase):
    def test_insert_docx_creates_cover_copy_and_preserves_source(self):
        from PIL import Image
        from docx import Document

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image_path = root / "cover.png"
            Image.new("RGB", (120, 160), "white").save(image_path)
            source = root / "book.docx"
            document = Document()
            document.add_paragraph("Original content")
            document.save(source)

            output = _insert_docx(image_path, str(source))
            self.assertTrue(Path(output).is_file())
            self.assertTrue(source.is_file())
            reopened = Document(output)
            self.assertGreaterEqual(len(reopened.inline_shapes), 1)
            self.assertIn("Original content", "\n".join(p.text for p in reopened.paragraphs))

    def test_insert_pptx_appends_visual_slide_and_preserves_source(self):
        from PIL import Image
        from pptx import Presentation

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image_path = root / "poster.png"
            Image.new("RGB", (160, 90), "white").save(image_path)
            source = root / "deck.pptx"
            presentation = Presentation()
            presentation.slides.add_slide(presentation.slide_layouts[6])
            presentation.save(source)

            output = _insert_pptx(image_path, str(source))
            self.assertTrue(Path(output).is_file())
            self.assertTrue(source.is_file())
            reopened = Presentation(output)
            self.assertEqual(len(reopened.slides), 2)
            self.assertTrue(any(shape.shape_type == 13 for shape in reopened.slides[-1].shapes))


if __name__ == "__main__":
    unittest.main()
