"""Document conversion for formats supported by installed local libraries."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.path_policy import PathPolicyError, resolve_user_path, user_accessible_roots
from actions._office_common import safe_filename, safe_output_directory, unique_output_path

_MAX_OCR_PDF_BYTES = 50 * 1024 * 1024
_MAX_OCR_PAGES = 100


def _resolve_pdf_output(source: Path, params: dict) -> Path:
    directory = safe_output_directory(str(params.get("output_dir", "") or str(source.parent)))
    filename = safe_filename(str(params.get("filename", "") or ""), source.stem, ".pdf")
    return unique_output_path(directory, filename)


def _convert_docx_with_libreoffice(source: Path, destination: Path) -> tuple[bool, str]:
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if not executable:
        return False, "LibreOffice executable not found on PATH."
    # Use a fresh output directory so the converter cannot overwrite an existing file.
    with tempfile.TemporaryDirectory(prefix="marklv-docx-pdf-") as temp_dir:
        try:
            completed = subprocess.run(
                [executable, "--headless", "--convert-to", "pdf", "--outdir", temp_dir, str(source)],
                capture_output=True, text=True, timeout=120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"LibreOffice conversion failed: {exc}"
        generated = Path(temp_dir) / (source.stem + ".pdf")
        if completed.returncode != 0 or not generated.is_file() or generated.stat().st_size < 100:
            detail = (completed.stderr or completed.stdout or "no diagnostic output").strip()[:500]
            return False, f"LibreOffice did not produce a valid PDF ({detail})."
        shutil.move(str(generated), str(destination))
        return True, "LibreOffice"


def _convert_docx_with_word(source: Path, destination: Path) -> tuple[bool, str]:
    """Optional Windows Microsoft Word fallback; Word is automated read-only."""
    if __import__("os").name != "nt":
        return False, "Microsoft Word COM conversion is available only on Windows."
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return False, "Windows Word automation is unavailable (install pywin32 and Microsoft Word)."

    word = document = None
    pythoncom.CoInitialize()
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        document = word.Documents.Open(str(source.resolve()), ReadOnly=True, AddToRecentFiles=False)
        document.SaveAs2(str(destination.resolve()), FileFormat=17)  # wdFormatPDF
        document.Close(False)
        document = None
        if not destination.is_file() or destination.stat().st_size < 100:
            destination.unlink(missing_ok=True)
            return False, "Microsoft Word did not produce a valid PDF."
        return True, "Microsoft Word"
    except Exception as exc:
        destination.unlink(missing_ok=True)
        return False, f"Microsoft Word conversion failed: {type(exc).__name__}: {str(exc)[:300]}"
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def _docx_to_pdf(params: dict, raw_path: str) -> str:
    if not raw_path:
        return "file_path is required for docx_to_pdf."
    source = resolve_user_path(raw_path, roots=user_accessible_roots(), must_exist=True)
    if not source.is_file() or source.suffix.lower() != ".docx":
        return "Please provide an existing .docx file."
    directory = safe_output_directory(str(params.get("output_dir", "") or str(source.parent)))
    output = unique_output_path(
        directory,
        safe_filename(str(params.get("filename", "") or ""), source.stem, ".pdf"),
    )
    success, engine = _convert_docx_with_libreoffice(source, output)
    if not success:
        success, engine = _convert_docx_with_word(source, output)
    if not success:
        return (
            "DOCX-to-PDF conversion is unavailable. Install LibreOffice and add soffice to PATH, "
            "or use Windows with Microsoft Word and pywin32. Details: " + engine
        )
    if not output.is_file() or output.stat().st_size < 100:
        output.unlink(missing_ok=True)
        return "PDF validation failed; incomplete output removed."
    return f"Converted DOCX to PDF with {engine}: {output}\nBytes: {output.stat().st_size}"


def _ocr_pdf(params: dict, raw_path: str) -> str:
    if not raw_path:
        return "file_path is required for ocr_pdf."
    source = resolve_user_path(raw_path, roots=user_accessible_roots(), must_exist=True)
    if not source.is_file() or source.suffix.lower() != ".pdf":
        return "Please provide an existing PDF file."
    if source.stat().st_size > _MAX_OCR_PDF_BYTES:
        return "OCR input exceeds the 50 MiB safety limit."
    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        return (
            "OCR dependencies are missing. Install them with: pip install pymupdf pytesseract Pillow. "
            "The Tesseract OCR engine must also be installed separately and available on PATH. "
            f"Missing module: {exc.name or exc}"
        )

    language = str(params.get("language", "eng+por") or "eng+por").strip()
    if not re.fullmatch(r"[A-Za-z0-9_+.-]{2,32}", language):
        return "language must contain only language codes such as eng, por, or eng+por."
    try:
        max_pages = int(params.get("max_pages", 20))
    except (ValueError, TypeError):
        return "max_pages must be an integer between 1 and 100."
    if not 1 <= max_pages <= _MAX_OCR_PAGES:
        return "max_pages must be between 1 and 100."

    chunks = []
    try:
        with fitz.open(source) as pdf:
            if pdf.is_encrypted:
                return "This PDF is encrypted; unlock it before OCR."
            if pdf.page_count == 0:
                return "The PDF has no pages."
            if pdf.page_count > max_pages:
                return f"PDF has {pdf.page_count} pages, exceeding max_pages={max_pages}. Increase max_pages up to 100 or split the PDF."
            for index, page in enumerate(pdf):
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                try:
                    text = pytesseract.image_to_string(image, lang=language, timeout=60)
                finally:
                    image.close()
                chunks.append(f"[Page {index + 1}]\n{text.strip()}")
    except pytesseract.TesseractNotFoundError:
        return "Tesseract OCR engine was not found. Install Tesseract OCR and ensure its executable is on PATH."
    except RuntimeError as exc:
        return f"OCR failed or timed out: {exc}"
    except Exception as exc:
        return f"OCR failed: {type(exc).__name__}: {str(exc)[:500]}"

    extracted = "\n\n".join(chunks).strip()
    if not extracted:
        return "OCR completed but no text was detected."
    if params.get("save", True):
        directory = safe_output_directory(str(params.get("output_dir", "") or str(source.parent)))
        output = unique_output_path(
            directory,
            safe_filename(str(params.get("filename", "") or ""), source.stem + "-ocr", ".txt"),
        )
        output.write_text(extracted, encoding="utf-8")
        return f"OCR extracted text from {len(chunks)} pages to: {output}\nCharacters: {len(extracted)}\n\nPreview:\n{extracted[:4000]}"
    return extracted[:12000]


def document_conversion(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "")).strip().lower()
    raw_path = str(params.get("file_path", "") or "").strip()
    try:
        if operation == "docx_to_pdf":
            return _docx_to_pdf(params, raw_path)

        if operation == "ocr_pdf":
            return _ocr_pdf(params, raw_path)

        if operation == "text_to_pdf":
            text = params.get("content", "")
            if not isinstance(text, str) or not text.strip():
                return "content must contain the text to export."
            try:
                from reportlab.lib.pagesizes import A4
                from reportlab.lib.styles import getSampleStyleSheet
                from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
                from reportlab.lib.units import mm
            except ImportError:
                return "PDF export requires ReportLab. Install it with: pip install reportlab"
            title = str(params.get("title", "Document")).strip() or "Document"
            directory = safe_output_directory(str(params.get("output_dir", "") or ""))
            output = unique_output_path(directory, safe_filename(str(params.get("filename", "") or ""), title, ".pdf"))
            styles = getSampleStyleSheet()
            story = [Paragraph(title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), styles["Title"]), Spacer(1, 6 * mm)]
            for block in text.split("\n\n"):
                safe = block.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
                if safe.strip():
                    story.append(Paragraph(safe, styles["BodyText"]))
                    story.append(Spacer(1, 3 * mm))
            SimpleDocTemplate(str(output), pagesize=A4, title=title).build(story)
            if not output.is_file() or output.stat().st_size < 100:
                output.unlink(missing_ok=True)
                return "PDF validation failed; incomplete output removed."
            return f"Created PDF: {output}\nBytes: {output.stat().st_size}"

        if operation == "extract_pdf_text":
            if not raw_path:
                return "file_path is required."
            source = resolve_user_path(raw_path, roots=user_accessible_roots(), must_exist=True)
            if not source.is_file() or source.suffix.lower() != ".pdf":
                return "Please provide an existing PDF file."
            extracted = ""
            try:
                import pdfplumber
                with pdfplumber.open(source) as pdf:
                    extracted = "\n\n".join(page.extract_text() or "" for page in pdf.pages)
            except ImportError:
                try:
                    from PyPDF2 import PdfReader
                    with source.open("rb") as stream:
                        extracted = "\n\n".join(page.extract_text() or "" for page in PdfReader(stream).pages)
                except ImportError:
                    return "PDF text extraction requires pdfplumber or PyPDF2."
            if not extracted.strip():
                return "No selectable text was extracted. This PDF may be scanned; use operation=ocr_pdf with Tesseract installed."
            if params.get("save", True):
                directory = safe_output_directory(str(params.get("output_dir", "") or str(source.parent)))
                output = unique_output_path(directory, safe_filename(str(params.get("filename", "") or ""), source.stem + "-text", ".txt"))
                output.write_text(extracted, encoding="utf-8")
                return f"Extracted {len(extracted)} characters to: {output}\n\nPreview:\n{extracted[:4000]}"
            return extracted[:12000]

        return "Unsupported operation. Use docx_to_pdf, text_to_pdf, extract_pdf_text, or ocr_pdf."
    except ImportError as exc:
        return f"Required conversion library unavailable: {exc}"
    except (OSError, ValueError, TypeError, PathPolicyError) as exc:
        return f"Conversion failed safely: {exc}"
    except Exception as exc:
        return f"Conversion failed: {type(exc).__name__}: {str(exc)[:700]}"


TOOL = {
    "name": "document_conversion",
    "description": "Convert DOCX to PDF using LibreOffice or Windows Microsoft Word, export plain text to PDF, extract selectable PDF text, and OCR scanned PDFs using PyMuPDF, pytesseract, Pillow and the separately installed Tesseract engine.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "description": "docx_to_pdf | text_to_pdf | extract_pdf_text | ocr_pdf"},
            "title": {"type": "STRING", "description": "PDF title"},
            "content": {"type": "STRING", "description": "Text to export as PDF"},
            "file_path": {"type": "STRING", "description": "Existing DOCX or PDF path, depending on operation"},
            "filename": {"type": "STRING", "description": "Optional output filename only"},
            "output_dir": {"type": "STRING", "description": "Optional output folder inside the current user's home directory or OS temporary directory"},
            "save": {"type": "BOOLEAN", "description": "Whether to save extracted/OCR text as a new TXT file"},
            "language": {"type": "STRING", "description": "Tesseract OCR language codes, for example eng, por, or eng+por"},
            "max_pages": {"type": "INTEGER", "description": "Maximum PDF pages to OCR; defaults to 20 and cannot exceed 100"}
        },
        "required": ["operation"]
    },
    "handler": document_conversion
}
