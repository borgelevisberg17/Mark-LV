"""Advanced editing for existing DOCX, XLSX, PPTX and structured EPUB files.

Existing files are preserved by default. Set in_place=true only when the caller
explicitly authorizes replacing the source file.
"""
from __future__ import annotations

import html
import os
import re
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from core.path_policy import PathPolicyError, resolve_user_path, user_accessible_roots
from actions._office_common import safe_filename, safe_output_directory, unique_output_path

_SCALAR_TYPES = (str, int, float, bool, type(None))
_MAX_INPUT_ITEMS = 10000


def _source_path(raw: str, suffixes: set[str]) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("file_path is required.")
    source = resolve_user_path(raw.strip(), roots=user_accessible_roots(), must_exist=True)
    if not source.is_file() or source.suffix.lower() not in suffixes:
        raise ValueError(f"Expected an existing file with extension: {', '.join(sorted(suffixes))}.")
    if source.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("Source file exceeds the 100 MiB editing limit.")
    return source


def _destination(source: Path, params: dict, extension: str) -> Path:
    if params.get("in_place") is True:
        return source
    requested_dir = str(params.get("output_dir", "") or "")
    directory = safe_output_directory(requested_dir) if requested_dir else source.parent
    filename = str(params.get("filename", "") or "")
    default = f"{source.stem}-edited"
    safe = safe_filename(filename, default, extension)
    return unique_output_path(directory, safe)


def _save_package(source: Path, destination: Path, writer) -> str:
    temporary = destination.with_name(f".{destination.stem}-{uuid.uuid4().hex}.tmp{destination.suffix}")
    try:
        writer(temporary)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ValueError("The editor produced an empty output file.")
        # Reopen ZIP-based Office/EPUB packages before replacing a target.
        if temporary.suffix.lower() in {".docx", ".xlsx", ".pptx", ".epub"}:
            with zipfile.ZipFile(temporary) as archive:
                bad = archive.testzip()
                if bad:
                    raise zipfile.BadZipFile(f"Corrupt package member: {bad}")
        if destination == source:
            os.replace(temporary, destination)
        else:
            os.replace(temporary, destination)
        return str(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_paragraph_runs(paragraph, old: str, new: str) -> bool:
    """Replace within one run to preserve all surrounding run formatting."""
    changed = False
    for run in paragraph.runs:
        if old in run.text:
            run.text = run.text.replace(old, new)
            changed = True
    return changed


def _docx_edit(params: dict) -> str:
    from docx import Document
    from docx.enum.text import WD_BREAK
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches

    source = _source_path(str(params.get("file_path", "") or ""), {".docx"})
    operation = str(params["operation"])
    destination = _destination(source, params, ".docx")
    if destination == source and params.get("in_place") is not True:
        raise ValueError("Refusing to overwrite the source document.")
    doc = Document(source)
    changes = 0

    if operation == "replace_text":
        old = params.get("old_text")
        new = params.get("new_text", "")
        if not isinstance(old, str) or not old:
            raise ValueError("old_text must be a non-empty string.")
        if not isinstance(new, str):
            raise ValueError("new_text must be a string.")
        for paragraph in doc.paragraphs:
            changes += int(_replace_paragraph_runs(paragraph, old, new))
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        changes += int(_replace_paragraph_runs(paragraph, old, new))
        if changes == 0:
            return f"No matching text was found inside a single formatted text run. Source unchanged: {source}"
        summary = f"Replaced matching text in {changes} paragraph/run locations."

    elif operation == "append_content":
        blocks = params.get("blocks")
        if not isinstance(blocks, list) or not blocks or len(blocks) > 500:
            raise ValueError("blocks must be a list of 1–500 block objects.")
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("text", ""), str):
                raise ValueError("Each block must contain string text and optional type/level.")
            kind = str(block.get("type", "paragraph")).lower()
            text = block.get("text", "")
            if kind == "heading":
                level = block.get("level", 1)
                if not isinstance(level, int) or level < 1 or level > 9:
                    raise ValueError("Heading level must be an integer from 1 to 9.")
                doc.add_heading(text, level=level)
            elif kind == "title":
                doc.add_heading(text, level=0)
            elif kind == "page_break":
                doc.add_page_break()
            elif kind == "paragraph":
                doc.add_paragraph(text)
            elif kind == "bullet":
                doc.add_paragraph(text, style="List Bullet")
            elif kind == "number":
                doc.add_paragraph(text, style="List Number")
            else:
                raise ValueError(f"Unsupported DOCX block type: {kind}")
            changes += 1
        summary = f"Appended {changes} document blocks."

    elif operation == "add_toc":
        heading = str(params.get("heading", "Índice")).strip() or "Índice"
        anchor = doc.paragraphs[0] if doc.paragraphs else doc.add_paragraph()
        heading_paragraph = anchor.insert_paragraph_before(heading)
        heading_paragraph.style = "Heading 1"
        paragraph = anchor.insert_paragraph_before()
        begin_run = OxmlElement("w:r")
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        begin.set(qn("w:dirty"), "true")
        begin_run.append(begin)
        instruction_run = OxmlElement("w:r")
        instruction = OxmlElement("w:instrText")
        instruction.set(qn("xml:space"), "preserve")
        instruction.text = ' TOC \\o "1-3" \\h \\z \\u '
        instruction_run.append(instruction)
        separate_run = OxmlElement("w:r")
        separate = OxmlElement("w:fldChar")
        separate.set(qn("w:fldCharType"), "separate")
        separate_run.append(separate)
        placeholder_run = OxmlElement("w:r")
        placeholder_text = OxmlElement("w:t")
        placeholder_text.text = "Abra no Word e atualize o campo para gerar o índice."
        placeholder_run.append(placeholder_text)
        end_run = OxmlElement("w:r")
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        end_run.append(end)
        paragraph._p.append(begin_run)
        paragraph._p.append(instruction_run)
        paragraph._p.append(separate_run)
        paragraph._p.append(placeholder_run)
        paragraph._p.append(end_run)
        summary = "Inserted a Word TOC field. Word must update the field to calculate page numbers."

    elif operation == "add_references":
        references = params.get("references")
        if not isinstance(references, list) or not references or len(references) > 200:
            raise ValueError("references must be a list of 1–200 reference strings.")
        if any(not isinstance(ref, str) or not ref.strip() for ref in references):
            raise ValueError("Every reference must be a non-empty string.")
        doc.add_page_break()
        doc.add_heading(str(params.get("heading", "Referências bibliográficas")), level=1)
        for ref in references:
            doc.add_paragraph(ref.strip(), style="List Number")
        changes = len(references)
        summary = f"Added {changes} bibliography entries."

    elif operation == "insert_cover":
        image_path = _source_path(str(params.get("image_path", "") or ""), {".png", ".jpg", ".jpeg"})
        if image_path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Cover image exceeds the 20 MiB limit.")
        # Insert the cover at the beginning and keep existing body content after it.
        first = doc.paragraphs[0] if doc.paragraphs else doc.add_paragraph()
        cover = first.insert_paragraph_before()
        cover.alignment = 1
        cover.add_run().add_picture(str(image_path), width=Inches(5.8))
        cover.add_run().add_break(WD_BREAK.PAGE)
        title = str(params.get("cover_title", "")).strip()
        if title:
            title_paragraph = first.insert_paragraph_before(title)
            title_paragraph.style = "Title"
        summary = f"Inserted cover image {image_path.name} at the beginning."

    else:
        raise ValueError(f"Unsupported DOCX operation: {operation}")

    result_path = _save_package(source, destination, lambda temp: doc.save(temp))
    # Validate the saved document is parseable before reporting success.
    check = Document(result_path)
    if not check.paragraphs:
        raise ValueError("DOCX validation failed: no readable paragraphs were found.")
    return f"Edited DOCX: {result_path}\n{summary}\nValidation: reopened successfully; {len(check.paragraphs)} paragraphs."


def _xlsx_edit(params: dict) -> str:
    from openpyxl import load_workbook
    source = _source_path(str(params.get("file_path", "") or ""), {".xlsx", ".xlsm"})
    operation = str(params["operation"])
    destination = _destination(source, params, source.suffix.lower())
    keep_vba = source.suffix.lower() == ".xlsm"
    workbook = load_workbook(source, keep_vba=keep_vba, data_only=False)
    try:
        sheet_name = str(params.get("sheet_name", "") or workbook.active.title)
        if sheet_name not in workbook.sheetnames:
            raise ValueError(f"Worksheet not found: {sheet_name}")
        sheet = workbook[sheet_name]
        if operation == "set_cells":
            cells = params.get("cells")
            if not isinstance(cells, dict) or not cells or len(cells) > 1000:
                raise ValueError("cells must be a non-empty object of A1-style addresses to values (max 1,000).")
            for address, value in cells.items():
                if not isinstance(address, str) or not re.fullmatch(r"[A-Za-z]{1,3}[1-9][0-9]{0,6}", address):
                    raise ValueError(f"Invalid A1 cell address: {address}")
                if not isinstance(value, _SCALAR_TYPES):
                    raise ValueError(f"Cell {address} must contain a scalar value.")
                cell = sheet[address.upper()]
                cell.value = value
                if isinstance(value, str):
                    cell.data_type = "s"
            summary = f"Updated {len(cells)} cells in '{sheet_name}'."
        elif operation == "append_rows":
            rows = params.get("rows")
            if not isinstance(rows, list) or not rows or len(rows) > 10000:
                raise ValueError("rows must be a non-empty list of row arrays (max 10,000).")
            if any(not isinstance(row, list) or len(row) > 500 for row in rows):
                raise ValueError("Each row must be an array of at most 500 cells.")
            if any(not isinstance(value, _SCALAR_TYPES) for row in rows for value in row):
                raise ValueError("Spreadsheet cells must be scalar values.")
            for row in rows:
                sheet.append(row)
                for cell in sheet[sheet.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
            summary = f"Appended {len(rows)} rows to '{sheet_name}'."
        elif operation == "add_sheet":
            name = str(params.get("new_sheet_name", "")).strip()
            if not name or len(name) > 31 or any(char in name for char in r'[]:*?/\\'):
                raise ValueError("new_sheet_name must be 1–31 characters and cannot contain []:*?/\\.")
            if name in workbook.sheetnames:
                raise ValueError(f"Worksheet already exists: {name}")
            workbook.create_sheet(name)
            summary = f"Created worksheet '{name}'."
        else:
            raise ValueError(f"Unsupported XLSX operation: {operation}")
        def writer(temp):
            workbook.save(temp)
        result_path = _save_package(source, destination, writer)
    finally:
        workbook.close()
    check = load_workbook(result_path, read_only=True, data_only=False)
    try:
        if sheet_name not in check.sheetnames:
            raise ValueError("XLSX validation failed: worksheet missing after save.")
    finally:
        check.close()
    return f"Edited workbook: {result_path}\n{summary}\nValidation: workbook reopened successfully."


def _pptx_edit(params: dict) -> str:
    from pptx import Presentation
    source = _source_path(str(params.get("file_path", "") or ""), {".pptx"})
    operation = str(params["operation"])
    destination = _destination(source, params, ".pptx")
    presentation = Presentation(source)
    if operation == "replace_shape_text":
        slide_index = params.get("slide_index")
        shape_index = params.get("shape_index")
        text = params.get("text")
        if not isinstance(slide_index, int) or not 0 <= slide_index < len(presentation.slides):
            raise ValueError("slide_index must identify an existing zero-based slide.")
        shapes = presentation.slides[slide_index].shapes
        if not isinstance(shape_index, int) or not 0 <= shape_index < len(shapes):
            raise ValueError("shape_index must identify an existing zero-based shape on that slide.")
        if not isinstance(text, str):
            raise ValueError("text must be a string.")
        shape = shapes[shape_index]
        if not shape.has_text_frame:
            raise ValueError("The selected shape does not contain editable text.")
        paragraph = shape.text_frame.paragraphs[0]
        if paragraph.runs:
            paragraph.runs[0].text = text
            for run in paragraph.runs[1:]:
                run.text = ""
        else:
            paragraph.add_run().text = text
        summary = f"Updated text in slide {slide_index + 1}, shape {shape_index + 1}."
    elif operation == "append_slides":
        slides = params.get("slides")
        if not isinstance(slides, list) or not slides or len(slides) > 50:
            raise ValueError("slides must be a list of 1–50 slide objects.")
        if len(presentation.slides) + len(slides) > 150:
            raise ValueError("A presentation is limited to 150 slides after editing.")
        from pptx.util import Pt
        for item in slides:
            if not isinstance(item, dict) or not isinstance(item.get("title", ""), str):
                raise ValueError("Each slide must be an object with a string title.")
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = item.get("title", "")
            body = item.get("body", "")
            bullets = item.get("bullets", [])
            if not isinstance(body, str) or not isinstance(bullets, list) or any(not isinstance(x, str) for x in bullets):
                raise ValueError("Slide body must be text and bullets must be a list of strings.")
            frame = slide.placeholders[1].text_frame
            frame.clear()
            if body:
                frame.paragraphs[0].text = body
                frame.paragraphs[0].font.size = Pt(18)
            for bullet in bullets:
                paragraph = frame.add_paragraph()
                paragraph.text = bullet
                paragraph.level = 0
                paragraph.font.size = Pt(18)
        summary = f"Appended {len(slides)} slides."
    else:
        raise ValueError(f"Unsupported PPTX operation: {operation}")
    result_path = _save_package(source, destination, lambda temp: presentation.save(temp))
    check = Presentation(result_path)
    if len(check.slides) < 1:
        raise ValueError("PPTX validation failed: no slides found.")
    return f"Edited presentation: {result_path}\n{summary}\nValidation: reopened successfully; {len(check.slides)} slides."


def _create_epub(params: dict) -> str:
    title = str(params.get("title", "")).strip()
    chapters = params.get("chapters")
    if not title:
        raise ValueError("title is required.")
    if not isinstance(chapters, list) or not chapters or len(chapters) > 100:
        raise ValueError("chapters must be a list of 1–100 chapter objects.")
    if any(not isinstance(ch, dict) or not isinstance(ch.get("title"), str) or not isinstance(ch.get("content"), str) for ch in chapters):
        raise ValueError("Each chapter requires string title and content.")
    directory = safe_output_directory(str(params.get("output_dir", "") or ""))
    output = unique_output_path(directory, safe_filename(str(params.get("filename", "") or ""), title, ".epub"))
    cover_path = None
    raw_cover = str(params.get("cover_image_path", "") or "").strip()
    if raw_cover:
        cover_path = _source_path(raw_cover, {".png", ".jpg", ".jpeg"})
        if cover_path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("EPUB cover exceeds the 20 MiB limit.")
    identifier = "urn:uuid:" + str(uuid.uuid4())
    manifest = []
    spine = []
    nav_items = []
    entries = {
        "META-INF/container.xml": ('<?xml version="1.0" encoding="UTF-8"?>'
          '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
          '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'),
    }
    for index, chapter in enumerate(chapters, 1):
        filename = f"chapter-{index:03d}.xhtml"
        chapter_title = chapter["title"].strip() or f"Chapter {index}"
        blocks = []
        for paragraph in chapter["content"].split("\n\n"):
            if paragraph.strip():
                blocks.append("<p>" + html.escape(paragraph.strip()).replace("\n", "<br/>") + "</p>")
        xhtml = ('<?xml version="1.0" encoding="utf-8"?>'
          '<html xmlns="http://www.w3.org/1999/xhtml" lang="pt"><head><title>'
          + html.escape(chapter_title) + '</title><meta charset="utf-8"/></head><body><h1>'
          + html.escape(chapter_title) + '</h1>' + "".join(blocks) + '</body></html>')
        entries[f"OEBPS/{filename}"] = xhtml
        manifest.append(f'<item id="chapter{index}" href="{filename}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="chapter{index}"/>')
        nav_items.append(f'<li><a href="{filename}">{html.escape(chapter_title)}</a></li>')
    cover_manifest = ""
    cover_meta = ""
    cover_nav = ""
    if cover_path:
        ext = ".jpg" if cover_path.suffix.lower() in {".jpg", ".jpeg"} else ".png"
        media = "image/jpeg" if ext == ".jpg" else "image/png"
        entries[f"OEBPS/cover{ext}"] = cover_path.read_bytes()
        manifest.append(f'<item id="cover-image" href="cover{ext}" media-type="{media}" properties="cover-image"/>')
        cover_manifest = f'<item id="cover-page" href="cover.xhtml" media-type="application/xhtml+xml"/>'
        cover_meta = '<meta name="cover" content="cover-image"/>'
        cover_nav = ('<?xml version="1.0" encoding="utf-8"?>'
          '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title></head><body><img src="cover'
          + ext + '" alt="Book cover"/></body></html>')
        entries["OEBPS/cover.xhtml"] = cover_nav
        spine.insert(0, '<itemref idref="cover-page" linear="no"/>')
    entries["OEBPS/nav.xhtml"] = ('<?xml version="1.0" encoding="utf-8"?>'
      '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
      '<head><title>Contents</title></head><body><nav epub:type="toc"><h1>Contents</h1><ol>'
      + "".join(nav_items) + '</ol></nav></body></html>')
    manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    opf = ('<?xml version="1.0" encoding="UTF-8"?>'
      '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">'
      '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">'
      + identifier + '</dc:identifier><dc:title>' + html.escape(title)
      + '</dc:title><dc:language>pt</dc:language><dc:creator>Mark-LV</dc:creator>'
      + cover_meta + '</metadata><manifest>' + "".join(manifest) + cover_manifest
      + '</manifest><spine>' + "".join(spine) + '</spine></package>')
    entries["OEBPS/content.opf"] = opf
    temporary = output.with_name(f".{output.stem}-{uuid.uuid4().hex}.tmp.epub")
    try:
        with zipfile.ZipFile(temporary, "w") as book:
            book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            for name, value in entries.items():
                book.writestr(name, value, compress_type=zipfile.ZIP_DEFLATED)
        with zipfile.ZipFile(temporary) as book:
            if book.namelist()[0] != "mimetype" or book.testzip():
                raise zipfile.BadZipFile("Generated EPUB failed package validation.")
            ET.fromstring(book.read("OEBPS/content.opf"))
            ET.fromstring(book.read("OEBPS/nav.xhtml"))
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return f"Created structured EPUB: {output}\nChapters: {len(chapters)}\nCover: {'included' if cover_path else 'not provided'}\nValidation: package and navigation XML reopened successfully."


def office_editor(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "")).strip().lower()
    try:
        if operation in {"replace_text", "append_content", "add_toc", "add_references", "insert_cover"}:
            return _docx_edit(params)
        if operation in {"set_cells", "append_rows", "add_sheet"}:
            return _xlsx_edit(params)
        if operation in {"replace_shape_text", "append_slides"}:
            return _pptx_edit(params)
        if operation == "create_epub":
            return _create_epub(params)
        return ("Unsupported operation. Use replace_text, append_content, add_toc, add_references, "
                "insert_cover, set_cells, append_rows, add_sheet, replace_shape_text, append_slides, or create_epub.")
    except ImportError as exc:
        return f"Required editing library is unavailable: {exc}"
    except (OSError, ValueError, TypeError, KeyError, PathPolicyError, zipfile.BadZipFile) as exc:
        return f"Office editing failed safely: {exc}"
    except Exception as exc:
        return f"Office editing failed: {type(exc).__name__}: {exc}"


TOOL = {
    "name": "office_editor",
    "description": (
        "Advanced editing for existing Word DOCX, Excel XLSX/XLSM, and PowerPoint PPTX files, "
        "plus structured multi-chapter EPUB publishing. Operations: replace_text (within individual DOCX text runs), "
        "append_content (paragraphs/headings/bullets/numbers/page breaks), add_toc (inserts a Word TOC field; user must update it in Word), "
        "add_references, insert_cover, set_cells, append_rows, add_sheet, replace_shape_text, append_slides, create_epub. "
        "Existing source files are preserved by default and an edited copy is created. Only set in_place=true when the user explicitly asks to overwrite the source. "
        "DOCX replacements preserve run formatting but do not match text split across multiple differently formatted runs. "
        "Use exact local file_path values. Files are reopened and validated before success is reported."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "description": "replace_text | append_content | add_toc | add_references | insert_cover | set_cells | append_rows | add_sheet | replace_shape_text | append_slides | create_epub"},
            "file_path": {"type": "STRING", "description": "Existing DOCX/XLSX/XLSM/PPTX source file path"},
            "in_place": {"type": "BOOLEAN", "description": "Default false: save a new edited copy and preserve source. Set true only with explicit user authorization to overwrite."},
            "output_dir": {"type": "STRING", "description": "Optional output directory within the permitted user roots"},
            "filename": {"type": "STRING", "description": "Optional output filename only"},
            "old_text": {"type": "STRING", "description": "replace_text: text to replace inside a single DOCX run"},
            "new_text": {"type": "STRING", "description": "replace_text: replacement text"},
            "blocks": {"type": "ARRAY", "items": {"type": "OBJECT"}, "description": "append_content: objects with type paragraph|heading|title|page_break|bullet|number, text, and optional heading level"},
            "heading": {"type": "STRING", "description": "Optional heading for table of contents or bibliography"},
            "references": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "add_references: bibliography entries"},
            "image_path": {"type": "STRING", "description": "insert_cover: local PNG or JPEG cover image path"},
            "cover_title": {"type": "STRING", "description": "insert_cover: optional title to place on the cover"},
            "sheet_name": {"type": "STRING", "description": "XLSX: target sheet name; defaults to active sheet"},
            "cells": {"type": "OBJECT", "description": "set_cells: mapping of A1 cell addresses to scalar values"},
            "rows": {"type": "ARRAY", "items": {"type": "ARRAY", "items": {"type": "STRING"}}, "description": "append_rows: row arrays to append"},
            "new_sheet_name": {"type": "STRING", "description": "add_sheet: new worksheet name"},
            "slide_index": {"type": "INTEGER", "description": "replace_shape_text: zero-based slide index"},
            "shape_index": {"type": "INTEGER", "description": "replace_shape_text: zero-based shape index"},
            "text": {"type": "STRING", "description": "replace_shape_text: replacement text"},
            "slides": {"type": "ARRAY", "items": {"type": "OBJECT"}, "description": "append_slides: slide objects with title, optional body and bullets"},
            "title": {"type": "STRING", "description": "create_epub: book title"},
            "chapters": {"type": "ARRAY", "items": {"type": "OBJECT"}, "description": "create_epub: chapter objects with title and content strings"},
            "cover_image_path": {"type": "STRING", "description": "create_epub: optional PNG/JPEG cover image path"}
        },
        "required": ["operation"]
    },
    "handler": office_editor
}
