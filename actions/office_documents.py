"""Create editable Word documents and basic EPUB ebooks locally."""
from __future__ import annotations

import html
import uuid
import zipfile
from pathlib import Path

from core.path_policy import PathPolicyError
from actions._office_common import safe_filename, safe_output_directory, unique_output_path


def office_documents(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "create_docx")).strip().lower()
    title = str(params.get("title", "Untitled document")).strip() or "Untitled document"
    content = params.get("content", "")
    output_dir = str(params.get("output_dir", "") or "")
    filename = str(params.get("filename", "") or "")

    if not isinstance(content, (str, list)):
        return "The content must be text or a list of paragraph strings."
    if isinstance(content, list) and any(not isinstance(item, str) for item in content):
        return "Every content item must be a string."

    try:
        directory = safe_output_directory(output_dir)
        if operation == "create_docx":
            from docx import Document
            from docx.shared import Inches, Pt

            out_name = safe_filename(filename, title, ".docx")
            output = unique_output_path(directory, out_name)
            doc = Document()
            section = doc.sections[0]
            section.top_margin = Inches(0.8)
            section.bottom_margin = Inches(0.8)
            section.left_margin = Inches(0.9)
            section.right_margin = Inches(0.9)
            doc.core_properties.title = title
            doc.core_properties.subject = "Created by Mark-LV Office Productivity Agent"
            doc.add_heading(title, level=0)
            paragraphs = content if isinstance(content, list) else content.split("\n")
            for line in paragraphs:
                if line.strip():
                    doc.add_paragraph(line.strip())
            for style_name in ("Normal",):
                if style_name in doc.styles:
                    doc.styles[style_name].font.name = "Aptos"
                    doc.styles[style_name].font.size = Pt(11)
            doc.save(output)
            # Verify that the resulting package can be reopened.
            check = Document(output)
            if not check.paragraphs or check.paragraphs[0].text != title:
                output.unlink(missing_ok=True)
                return "Document validation failed; the incomplete output was removed."
            return f"Created editable DOCX: {output}\nParagraphs: {len(check.paragraphs)}"

        if operation == "create_epub":
            body = content if isinstance(content, str) else "\n\n".join(content)
            out_name = safe_filename(filename, title, ".epub")
            output = unique_output_path(directory, out_name)
            identifier = "urn:uuid:" + str(uuid.uuid4())
            xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" lang="pt"><head>'
                '<title>' + html.escape(title) + '</title></head><body>'
                '<h1>' + html.escape(title) + '</h1>'
                + "".join("<p>" + html.escape(p.strip()) + "</p>" for p in body.split("\n\n") if p.strip())
                + "</body></html>"
            )
            container = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                '</rootfiles></container>'
            )
            opf = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<dc:identifier id="book-id">' + identifier + '</dc:identifier>'
                '<dc:title>' + html.escape(title) + '</dc:title><dc:language>pt</dc:language>'
                '<dc:creator>Mark-LV</dc:creator></metadata>'
                '<manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest>'
                '<spine><itemref idref="chapter"/></spine></package>'
            )
            nav = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                '<head><title>Contents</title></head><body><nav epub:type="toc"><h1>Contents</h1>'
                '<ol><li><a href="chapter.xhtml">' + html.escape(title) + '</a></li></ol>'
                '</nav></body></html>'
            )
            with zipfile.ZipFile(output, "w") as book:
                book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                book.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
                book.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
                book.writestr("OEBPS/chapter.xhtml", xhtml, compress_type=zipfile.ZIP_DEFLATED)
                book.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
            with zipfile.ZipFile(output, "r") as book:
                if book.namelist()[0] != "mimetype" or book.testzip() is not None:
                    output.unlink(missing_ok=True)
                    return "EPUB validation failed; the incomplete output was removed."
            return f"Created EPUB ebook: {output}"

        if operation == "inspect_docx":
            from docx import Document
            raw_path = str(params.get("file_path", "") or "").strip()
            if not raw_path:
                return "file_path is required for inspect_docx."
            from core.path_policy import resolve_user_path, user_accessible_roots
            source = resolve_user_path(raw_path, roots=user_accessible_roots(), must_exist=True)
            if source.suffix.lower() != ".docx" or not source.is_file():
                return "Please provide an existing .docx file."
            doc = Document(source)
            text = "\n".join(p.text for p in doc.paragraphs)
            preview = text[:12000]
            return f"DOCX: {source}\nParagraphs: {len(doc.paragraphs)}\nCharacters: {len(text)}\n\n{preview}"

        return "Unsupported operation. Use create_docx, create_epub, or inspect_docx."
    except ImportError as exc:
        return f"Required document library is unavailable: {exc}. Install python-docx to create or inspect DOCX files."
    except (OSError, ValueError, PathPolicyError, zipfile.BadZipFile) as exc:
        return f"Document operation failed safely: {exc}"
    except Exception as exc:
        return f"Document operation failed: {exc}"


TOOL = {
    "name": "office_documents",
    "description": "Create editable Word DOCX documents and basic EPUB ebooks, or inspect an existing DOCX. Use create_docx for professional editable documents, create_epub for a simple reflowable ebook, and inspect_docx to extract paragraph text. Outputs are new files; existing files are never overwritten.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "description": "create_docx | create_epub | inspect_docx"},
            "title": {"type": "STRING", "description": "Document or ebook title"},
            "content": {"type": "STRING", "description": "Body text. Separate paragraphs with blank lines. For create_docx, a list of paragraph strings is also accepted by the handler."},
            "filename": {"type": "STRING", "description": "Optional filename only, without a directory path"},
            "output_dir": {"type": "STRING", "description": "Optional output folder inside the current user's home directory or OS temporary directory"},
            "file_path": {"type": "STRING", "description": "Existing DOCX path, for inspect_docx only"}
        },
        "required": ["operation"]
    },
    "handler": office_documents
}
