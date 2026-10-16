"""Create editable Word documents and basic EPUB ebooks locally."""
from __future__ import annotations

import html
import os
import platform
import subprocess
import uuid
import zipfile
from pathlib import Path

from core.path_policy import PathPolicyError, resolve_user_path, user_accessible_roots
from actions._office_common import safe_filename, safe_output_directory, unique_output_path

_OPENABLE_EXTENSIONS = {
    ".docx", ".doc", ".rtf", ".txt", ".odt", ".pdf", ".epub",
    ".xlsx", ".xls", ".csv", ".pptx", ".ppt", ".odp", ".ods",
}
_WORD_EXTENSIONS = {".docx", ".doc", ".rtf", ".txt"}


def _find_document_path(file_path: str, filename: str) -> Path:
    """Resolve an explicit safe path or find a named file in common user folders."""
    if file_path.strip():
        source = resolve_user_path(
            file_path.strip(), roots=user_accessible_roots(), must_exist=True
        )
        if not source.is_file():
            raise FileNotFoundError(f"Not a regular file: {source}")
        return source

    name = filename.strip()
    if not name:
        raise ValueError("Provide file_path or filename to open a document.")
    if Path(name).name != name or "/" in name or "\\" in name or "\x00" in name:
        raise PathPolicyError("Provide only the filename, not a path or directory.")

    home = Path.home()
    folders = [
        home / "Documents" / "MarkLV",
        home / "Documents",
        home / "Desktop",
        home / "Downloads",
        home / "OneDrive" / "Documents",
    ]
    requested = Path(name)
    candidates = []
    for folder in folders:
        try:
            safe_folder = resolve_user_path(folder, roots=user_accessible_roots(), must_exist=True)
            if not safe_folder.is_dir():
                continue
            # Only inspect direct children; do not recursively scan the user's files.
            for item in safe_folder.iterdir():
                if not item.is_file() or item.suffix.lower() not in _OPENABLE_EXTENSIONS:
                    continue
                exact_name = item.name.casefold() == requested.name.casefold()
                same_stem = (
                    not requested.suffix
                    and item.stem.casefold() == requested.stem.casefold()
                )
                if exact_name or same_stem:
                    candidates.append(item)
        except (OSError, PathPolicyError):
            continue

    if not candidates:
        raise FileNotFoundError(
            f"Could not find '{name}' in Documents/MarkLV, Documents, Desktop, "
            "Downloads, or OneDrive/Documents. Pass the full file_path if it is elsewhere."
        )
    # Prefer an exact name, then the most recently modified matching file.
    candidates.sort(
        key=lambda item: (
            item.name.casefold() == requested.name.casefold(),
            item.stat().st_mtime,
        ),
        reverse=True,
    )
    return resolve_user_path(
        candidates[0], roots=user_accessible_roots(), must_exist=True
    )


def _open_document_path(source: Path, app_name: str = "") -> str:
    """Open a validated document through its OS association, without UI key simulation."""
    requested_app = app_name.strip().casefold()
    suffix = source.suffix.casefold()
    if requested_app in {"word", "microsoft word", "winword"} and suffix not in _WORD_EXTENSIONS:
        if suffix == ".epub":
            return (
                f"Cannot open {source.name} in Microsoft Word because Word does not natively "
                "support EPUB. The EPUB file is intact; open it with an EPUB reader, or create "
                "an editable .docx version with operation=create_docx."
            )
        return (
            f"Microsoft Word is not the recommended handler for {suffix or 'this file type'}. "
            "Use the default application or request a supported Word document."
        )

    system = platform.system()
    if system == "Windows":
        os.startfile(str(source))  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.Popen(
            ["open", str(source)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    elif system == "Linux":
        subprocess.Popen(
            ["xdg-open", str(source)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        return f"Opening files is not supported on operating system '{system}'."
    return f"Opened document: {source}\nApplication: {requested_app or 'default application'}"


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
        if operation == "open_document":
            source = _find_document_path(
                str(params.get("file_path", "") or ""),
                str(params.get("filename", "") or ""),
            )
            return _open_document_path(
                source, str(params.get("app_name", "") or "")
            )

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

        return "Unsupported operation. Use create_docx, create_epub, open_document, or inspect_docx."
    except ImportError as exc:
        return f"Required document library is unavailable: {exc}. Install python-docx to create or inspect DOCX files."
    except (OSError, ValueError, PathPolicyError, zipfile.BadZipFile) as exc:
        return f"Document operation failed safely: {exc}"
    except Exception as exc:
        return f"Document operation failed: {exc}"


TOOL = {
    "name": "office_documents",
    "description": "Create editable Word DOCX documents and basic EPUB ebooks, open a previously created document directly from its verified path, or inspect a DOCX. For a request to open a file, call operation=open_document with the exact file_path returned when it was created, or filename if it is in Documents/MarkLV, Documents, Desktop, Downloads, or OneDrive/Documents. Do NOT open Word and simulate Ctrl+O when you can open the exact file. Use app_name=word only for .docx/.doc/.rtf/.txt; EPUB is not natively supported by Word and should open with the default EPUB reader. Use create_docx for editable Word content and create_epub for a reader-ready ebook. Outputs are new files; existing files are never overwritten.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "description": "create_docx | create_epub | open_document | inspect_docx"},
            "title": {"type": "STRING", "description": "Document or ebook title"},
            "content": {"type": "STRING", "description": "Body text. Separate paragraphs with blank lines. For create_docx, a list of paragraph strings is also accepted by the handler."},
            "filename": {"type": "STRING", "description": "Optional filename only, without a directory path"},
            "output_dir": {"type": "STRING", "description": "Optional output folder inside the current user's home directory or OS temporary directory"},
            "file_path": {"type": "STRING", "description": "Existing local file path for open_document or inspect_docx. Prefer the exact path returned when creating the file."},
            "app_name": {"type": "STRING", "description": "Optional application preference for open_document, e.g. word. Leave empty to use the operating system's default application."}
        },
        "required": ["operation"]
    },
    "handler": office_documents
}
