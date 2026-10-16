"""Advanced editing for existing DOCX, XLSX, PPTX and structured EPUB files.

Existing files are preserved by default. Set in_place=true only when the caller
explicitly authorizes replacing the source file.
"""
from __future__ import annotations

import html
import os
from copy import copy
import re
import shutil
import subprocess
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
    """Replace across run boundaries while retaining unaffected formatting."""
    runs = list(paragraph.runs)
    texts = [run.text for run in runs]
    combined = "".join(texts)
    if old not in combined:
        return False
    offsets, cursor = [], 0
    for value in texts:
        offsets.append((cursor, cursor + len(value)))
        cursor += len(value)
    matches, cursor = [], 0
    while True:
        position = combined.find(old, cursor)
        if position < 0:
            break
        matches.append((position, position + len(old)))
        cursor = position + len(old)
    for match_start, match_end in reversed(matches):
        first_index = next((i for i, (start, end) in enumerate(offsets)
                            if start <= match_start < end), None)
        last_index = next((i for i, (start, end) in enumerate(offsets)
                           if start < match_end <= end), None)
        if first_index is None or last_index is None:
            continue
        first_local = match_start - offsets[first_index][0]
        last_local_end = match_end - offsets[last_index][0]
        if first_index == last_index:
            value = runs[first_index].text
            runs[first_index].text = value[:first_local] + new + value[last_local_end:]
        else:
            first_value, last_value = runs[first_index].text, runs[last_index].text
            runs[first_index].text = first_value[:first_local] + new
            for i in range(first_index + 1, last_index):
                runs[i].text = ""
            runs[last_index].text = last_value[last_local_end:]
    return True


def _iter_docx_paragraphs(container, seen=None):
    """Yield paragraphs from document parts and nested tables without duplicates."""
    if seen is None:
        seen = set()
    for paragraph in getattr(container, "paragraphs", ()):
        identity = id(paragraph._p)
        if identity not in seen:
            seen.add(identity)
            yield paragraph
    for table in getattr(container, "tables", ()):
        for row in table.rows:
            for cell in row.cells:
                yield from _iter_docx_paragraphs(cell, seen)


def _assert_xlsx_supported_features(source: Path, params: dict) -> None:
    """Fail closed for package features openpyxl may silently drop on save."""
    risky_prefixes = (
        "xl/charts/", "xl/drawings/", "xl/pivotTables/", "xl/pivotCache/",
        "xl/slicers/", "xl/externalLinks/", "xl/embeddings/", "xl/activeX/",
        "xl/threadedComments/", "xl/persons/", "customXml/",
    )
    try:
        with zipfile.ZipFile(source) as package:
            names = package.namelist()
            if len(names) > 10000 or sum(item.file_size for item in package.infolist()) > 500 * 1024 * 1024:
                raise ValueError("Workbook package exceeds the safe inspection limits.")
            unsupported = sorted({prefix.rstrip("/") for name in names for prefix in risky_prefixes
                                  if name.startswith(prefix)})
    except zipfile.BadZipFile as exc:
        raise ValueError("Workbook is not a valid XLSX/XLSM package.") from exc
    if unsupported and params.get("allow_unsupported_features") is not True:
        features = ", ".join(unsupported[:6])
        raise ValueError(
            "Editing stopped to protect workbook features that openpyxl may not preserve "
            f"({features}). Use native Excel editing, or explicitly set allow_unsupported_features=true "
            "only after accepting possible feature loss."
        )


def _validate_epub_package(path: Path) -> None:
    """Validate EPUB ZIP structure, XML well-formedness, manifest/spine and local links."""
    with zipfile.ZipFile(path) as book:
        names = book.namelist()
        if not names or names[0] != "mimetype":
            raise zipfile.BadZipFile("EPUB mimetype must be the first package entry.")
        info = book.getinfo("mimetype")
        if info.compress_type != zipfile.ZIP_STORED or book.read("mimetype") != b"application/epub+zip":
            raise zipfile.BadZipFile("EPUB mimetype must be present and uncompressed.")
        if book.testzip():
            raise zipfile.BadZipFile("Generated EPUB contains a corrupt ZIP member.")
        parsed = {name: ET.fromstring(book.read(name)) for name in names
                  if name.endswith((".xml", ".opf", ".xhtml"))}
        if "META-INF/container.xml" not in parsed or "OEBPS/content.opf" not in parsed:
            raise ValueError("EPUB container or package document is missing.")
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        opf = parsed["OEBPS/content.opf"]
        manifest = {item.attrib.get("id"): item.attrib.get("href")
                    for item in opf.findall("./opf:manifest/opf:item", ns)}
        if not manifest or any(not key or not href for key, href in manifest.items()):
            raise ValueError("EPUB manifest contains an item without an id or href.")
        for href in manifest.values():
            if str(Path("OEBPS") / href).replace("\\", "/") not in names:
                raise ValueError(f"EPUB manifest points to a missing resource: {href}")
        spine = opf.findall("./opf:spine/opf:itemref", ns)
        if not spine or any(item.attrib.get("idref") not in manifest for item in spine):
            raise ValueError("EPUB spine references a missing manifest item.")
        for name, root in parsed.items():
            if not name.endswith((".xhtml", ".opf")):
                continue
            base = Path(name).parent
            for element in root.iter():
                for attr in ("href", "src"):
                    value = element.attrib.get(attr, "")
                    if not value or value.startswith(("#", "http:", "https:", "mailto:", "data:")):
                        continue
                    local = value.split("#", 1)[0].split("?", 1)[0]
                    if local and str(base / local).replace("\\", "/") not in names:
                        raise ValueError(f"EPUB link points to a missing resource: {name} -> {value}")


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
        paragraphs = list(_iter_docx_paragraphs(doc))
        for section in doc.sections:
            for part_name in ("header", "footer", "first_page_header", "first_page_footer",
                              "even_page_header", "even_page_footer"):
                part = getattr(section, part_name, None)
                if part is not None:
                    paragraphs.extend(_iter_docx_paragraphs(part))
        seen = set()
        for paragraph in paragraphs:
            identity = id(paragraph._p)
            if identity in seen:
                continue
            seen.add(identity)
            changes += int(_replace_paragraph_runs(paragraph, old, new))
        if changes == 0:
            return f"No matching text was found in editable DOCX paragraphs. Source unchanged: {source}"
        summary = f"Replaced matching text in {changes} paragraphs, including supported table/header/footer content."

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
    _assert_xlsx_supported_features(source, params)
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
        elif operation == "set_formulas":
            formulas = params.get("formulas")
            if not isinstance(formulas, dict) or not formulas or len(formulas) > 1000:
                raise ValueError("formulas must be a non-empty object of A1 addresses to formulas (max 1,000).")
            for address, formula in formulas.items():
                if not isinstance(address, str) or not re.fullmatch(r"[A-Za-z]{1,3}[1-9][0-9]{0,6}", address):
                    raise ValueError(f"Invalid A1 cell address: {address}")
                if not isinstance(formula, str) or not formula.startswith("=") or len(formula) > 8192:
                    raise ValueError(f"Formula for {address} must be a string beginning with '=' and at most 8,192 characters.")
                sheet[address.upper()] = formula
            summary = f"Set {len(formulas)} formulas in '{sheet_name}'. Excel will calculate formula results when opened."
        elif operation == "format_cells":
            formats = params.get("formats")
            if not isinstance(formats, dict) or not formats or len(formats) > 1000:
                raise ValueError("formats must be a non-empty object of A1 addresses to style objects (max 1,000).")
            allowed = {"bold", "italic", "font_size", "font_name", "font_color", "fill_color",
                       "number_format", "horizontal_alignment", "wrap_text"}
            alignments = {"general", "left", "center", "right", "fill", "justify", "centerContinuous", "distributed"}
            from openpyxl.styles import Alignment, Font, PatternFill
            for address, style in formats.items():
                if not isinstance(address, str) or not re.fullmatch(r"[A-Za-z]{1,3}[1-9][0-9]{0,6}", address):
                    raise ValueError(f"Invalid A1 cell address: {address}")
                if not isinstance(style, dict) or not style or set(style) - allowed:
                    raise ValueError(f"Formatting for {address} must use supported style properties only: {', '.join(sorted(allowed))}.")
                cell = sheet[address.upper()]
                font = copy(cell.font)
                if "bold" in style:
                    if not isinstance(style["bold"], bool):
                        raise ValueError(f"bold for {address} must be boolean.")
                    font.bold = style["bold"]
                if "italic" in style:
                    if not isinstance(style["italic"], bool):
                        raise ValueError(f"italic for {address} must be boolean.")
                    font.italic = style["italic"]
                if "font_size" in style:
                    size = style["font_size"]
                    if isinstance(size, bool) or not isinstance(size, (int, float)) or not 1 <= size <= 72:
                        raise ValueError(f"font_size for {address} must be between 1 and 72.")
                    font.sz = size
                if "font_name" in style:
                    name = style["font_name"]
                    if not isinstance(name, str) or not name.strip() or len(name) > 64:
                        raise ValueError(f"font_name for {address} must be a non-empty string of at most 64 characters.")
                    font.name = name
                if "font_color" in style:
                    color = style["font_color"]
                    if not isinstance(color, str) or not re.fullmatch(r"(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})", color):
                        raise ValueError(f"font_color for {address} must be a 6- or 8-digit hexadecimal RGB/ARGB value.")
                    font.color = color if len(color) == 8 else "FF" + color
                cell.font = font
                if "fill_color" in style:
                    color = style["fill_color"]
                    if not isinstance(color, str) or not re.fullmatch(r"(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})", color):
                        raise ValueError(f"fill_color for {address} must be a 6- or 8-digit hexadecimal RGB/ARGB value.")
                    cell.fill = PatternFill(fill_type="solid", fgColor=color if len(color) == 8 else "FF" + color)
                if "number_format" in style:
                    number_format = style["number_format"]
                    if not isinstance(number_format, str) or not number_format or len(number_format) > 64:
                        raise ValueError(f"number_format for {address} must be a non-empty string of at most 64 characters.")
                    cell.number_format = number_format
                if "horizontal_alignment" in style:
                    alignment = style["horizontal_alignment"]
                    if alignment not in alignments:
                        raise ValueError(f"Unsupported horizontal_alignment for {address}: {alignment}")
                    cell.alignment = copy(cell.alignment)
                    cell.alignment = Alignment(
                        horizontal=alignment, vertical=cell.alignment.vertical,
                        text_rotation=cell.alignment.textRotation, wrap_text=cell.alignment.wrap_text,
                        shrink_to_fit=cell.alignment.shrink_to_fit, indent=cell.alignment.indent,
                    )
                if "wrap_text" in style:
                    if not isinstance(style["wrap_text"], bool):
                        raise ValueError(f"wrap_text for {address} must be boolean.")
                    cell.alignment = copy(cell.alignment)
                    cell.alignment = Alignment(
                        horizontal=cell.alignment.horizontal, vertical=cell.alignment.vertical,
                        text_rotation=cell.alignment.textRotation, wrap_text=style["wrap_text"],
                        shrink_to_fit=cell.alignment.shrink_to_fit, indent=cell.alignment.indent,
                    )
            summary = f"Applied formatting to {len(formats)} cells in '{sheet_name}'."
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
        elif operation == "rename_sheet":
            new_name = str(params.get("new_sheet_name", "")).strip()
            if not new_name or len(new_name) > 31 or any(char in new_name for char in r'[]:*?/\\'):
                raise ValueError("new_sheet_name must be 1–31 characters and cannot contain []:*?/\\.")
            if new_name in workbook.sheetnames and new_name != sheet_name:
                raise ValueError(f"Worksheet already exists: {new_name}")
            workbook[sheet_name].title = new_name
            sheet_name = new_name
            summary = f"Renamed worksheet to '{new_name}'."
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
        paragraphs = list(shape.text_frame.paragraphs)
        paragraph = paragraphs[0]
        if paragraph.runs:
            paragraph.runs[0].text = text
            for run in paragraph.runs[1:]:
                run.text = ""
        else:
            paragraph.add_run().text = text
        for extra in paragraphs[1:]:
            extra._p.getparent().remove(extra._p)
        summary = f"Replaced all text in slide {slide_index + 1}, shape {shape_index + 1}; first-paragraph formatting retained."
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


def _run_epubcheck(path: Path) -> str:
    """Run official EPUBCheck when available; never invoke it through a shell."""
    executable = shutil.which("epubcheck")
    if not executable:
        return "EPUBCheck not installed; structural validation only"
    try:
        completed = subprocess.run(
            [executable, str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("EPUBCheck exceeded the 120-second validation timeout.") from exc
    output = "\\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode != 0:
        detail = output[-2000:] if output else "No diagnostic output was returned."
        raise ValueError(f"EPUBCheck rejected the publication (exit {completed.returncode}): {detail}")
    return "EPUBCheck passed" + (f": {output[-500:]}" if output else "")


def _create_epub(params: dict) -> str:
    title = str(params.get("title", "")).strip()
    author = str(params.get("author", "Mark-LV")).strip() or "Mark-LV"
    language = str(params.get("language", "pt")).strip() or "pt"
    description = str(params.get("description", "")).strip()
    chapters = params.get("chapters")
    if not title:
        raise ValueError("title is required.")
    for label, value in (("title", title), ("author", author), ("language", language), ("description", description)):
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
            raise ValueError(f"{label} contains characters not allowed in XML.")
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
        content = chapter["content"]
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", chapter_title + content):
            raise ValueError(f"Chapter {index} contains characters not allowed in XML.")
        blocks = []
        for paragraph in content.split("\n\n"):
            if paragraph.strip():
                blocks.append("<p>" + html.escape(paragraph.strip()).replace("\n", "<br/>") + "</p>")
        xhtml = ('<?xml version="1.0" encoding="utf-8"?>'
          '<html xmlns="http://www.w3.org/1999/xhtml" lang="' + html.escape(language) + '"><head><title>'
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
      + '</dc:title><dc:language>' + html.escape(language) + '</dc:language><dc:creator>'
      + html.escape(author) + '</dc:creator>'
      + ('<dc:description>' + html.escape(description) + '</dc:description>' if description else '')
      + cover_meta + '</metadata><manifest>' + "".join(manifest) + cover_manifest
      + '</manifest><spine>' + "".join(spine) + '</spine></package>')
    entries["OEBPS/content.opf"] = opf
    temporary = output.with_name(f".{output.stem}-{uuid.uuid4().hex}.tmp.epub")
    try:
        with zipfile.ZipFile(temporary, "w") as book:
            book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            for name, value in entries.items():
                book.writestr(name, value, compress_type=zipfile.ZIP_DEFLATED)
        _validate_epub_package(temporary)
        epubcheck_status = _run_epubcheck(temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return f"Created structured EPUB: {output}\nChapters: {len(chapters)}\nCover: {'included' if cover_path else 'not provided'}\nValidation: package/XML/internal links passed; {epubcheck_status}."


def office_editor(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "")).strip().lower()
    try:
        if operation in {"replace_text", "append_content", "add_toc", "add_references", "insert_cover"}:
            return _docx_edit(params)
        if operation in {"set_cells", "set_formulas", "format_cells", "append_rows", "add_sheet", "rename_sheet"}:
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
        "plus structured multi-chapter EPUB publishing with metadata and structural validation. Operations: replace_text across DOCX runs, "
        "append_content (paragraphs/headings/bullets/numbers/page breaks), add_toc (inserts a Word TOC field; user must update it in Word), "
        "add_references, insert_cover, set_cells, set_formulas, format_cells, append_rows, add_sheet, rename_sheet, replace_shape_text, append_slides, create_epub. "
        "Existing source files are preserved by default and an edited copy is created. Only set in_place=true when the user explicitly asks to overwrite the source. "
        "DOCX replacements preserve unaffected run formatting and search supported body, table, header and footer paragraphs. "
        "XLSX/XLSM supports literal cell edits, explicit formulas, formatting, row append and sheet create/rename; it refuses charts, drawings, pivots, external links and embedded parts by default because openpyxl may drop them. "
        "PPTX replacement clears stale paragraphs and retains first-paragraph formatting. EPUB output validates XML, manifest/spine and local links, and runs official EPUBCheck automatically when installed. "
        "Use exact local file_path values; source files are copied by default."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "description": "replace_text | append_content | add_toc | add_references | insert_cover | set_cells | set_formulas | format_cells | append_rows | add_sheet | rename_sheet | replace_shape_text | append_slides | create_epub"},
            "file_path": {"type": "STRING", "description": "Existing DOCX/XLSX/XLSM/PPTX source file path"},
            "in_place": {"type": "BOOLEAN", "description": "Default false: save a new edited copy and preserve source. Set true only with explicit user authorization to overwrite."},
            "allow_unsupported_features": {"type": "BOOLEAN", "description": "XLSX/XLSM only: accept possible loss of chart, drawing, pivot, external-link or embedded features. Never set without explicit user authorization to accept that risk."},
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
            "cells": {"type": "OBJECT", "description": "set_cells: mapping of A1 cell addresses to scalar values; strings remain literal text, not formulas"},
            "formulas": {"type": "OBJECT", "description": "set_formulas: mapping of A1 cell addresses to Excel formulas beginning with '='"},
            "formats": {"type": "OBJECT", "description": "format_cells: mapping of A1 cell addresses to style objects using bold, italic, font_size, font_name, font_color, fill_color, number_format, horizontal_alignment, wrap_text"},
            "rows": {"type": "ARRAY", "items": {"type": "ARRAY", "items": {"type": "STRING"}}, "description": "append_rows: row arrays to append"},
            "new_sheet_name": {"type": "STRING", "description": "add_sheet: new worksheet name"},
            "slide_index": {"type": "INTEGER", "description": "replace_shape_text: zero-based slide index"},
            "shape_index": {"type": "INTEGER", "description": "replace_shape_text: zero-based shape index"},
            "text": {"type": "STRING", "description": "replace_shape_text: replacement text"},
            "slides": {"type": "ARRAY", "items": {"type": "OBJECT"}, "description": "append_slides: slide objects with title, optional body and bullets"},
            "title": {"type": "STRING", "description": "create_epub: book title"},
            "chapters": {"type": "ARRAY", "items": {"type": "OBJECT"}, "description": "create_epub: chapter objects with title and content strings"},
            "cover_image_path": {"type": "STRING", "description": "create_epub: optional PNG/JPEG cover image path"},
            "author": {"type": "STRING", "description": "create_epub: author metadata; defaults to Mark-LV"},
            "language": {"type": "STRING", "description": "create_epub: BCP 47 language tag; defaults to pt"},
            "description": {"type": "STRING", "description": "create_epub: optional book description metadata"}
        },
        "required": ["operation"]
    },
    "handler": office_editor
}
