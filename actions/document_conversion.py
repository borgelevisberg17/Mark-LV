"""Document conversion for formats supported by installed local libraries."""
from __future__ import annotations

from core.path_policy import PathPolicyError, resolve_user_path, user_accessible_roots
from actions._office_common import safe_filename, safe_output_directory, unique_output_path


def document_conversion(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "")).strip().lower()
    raw_path = str(params.get("file_path", "") or "").strip()
    try:
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
                return "No selectable text was extracted. This PDF may be scanned and require OCR."
            if params.get("save", True):
                directory = safe_output_directory(str(params.get("output_dir", "") or str(source.parent)))
                output = unique_output_path(directory, safe_filename(str(params.get("filename", "") or ""), source.stem + "-text", ".txt"))
                output.write_text(extracted, encoding="utf-8")
                return f"Extracted {len(extracted)} characters to: {output}\n\nPreview:\n{extracted[:4000]}"
            return extracted[:12000]

        return "Unsupported operation. Use text_to_pdf or extract_pdf_text. DOCX-to-PDF requires an installed Office/LibreOffice converter and is not yet implemented."
    except ImportError as exc:
        return f"Required conversion library unavailable: {exc}"
    except (OSError, ValueError, TypeError, PathPolicyError) as exc:
        return f"Conversion failed safely: {exc}"
    except Exception as exc:
        return f"Conversion failed: {exc}"


TOOL = {
    "name": "document_conversion",
    "description": "Convert plain text into a PDF or extract selectable text from a PDF. PDF export needs ReportLab; extraction uses pdfplumber or PyPDF2. OCR and DOCX-to-PDF conversion are not claimed unless a separate converter is installed and implemented.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "description": "text_to_pdf | extract_pdf_text"},
            "title": {"type": "STRING", "description": "PDF title"},
            "content": {"type": "STRING", "description": "Text to export as PDF"},
            "file_path": {"type": "STRING", "description": "Existing PDF path for text extraction"},
            "filename": {"type": "STRING", "description": "Optional output filename only"},
            "output_dir": {"type": "STRING", "description": "Optional output folder inside the current user's home directory or OS temporary directory"},
            "save": {"type": "BOOLEAN", "description": "Whether to save extracted text as a new TXT file"}
        },
        "required": ["operation"]
    },
    "handler": document_conversion
}
