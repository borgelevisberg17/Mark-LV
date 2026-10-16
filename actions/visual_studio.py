"""Generate/edit visual assets with Gemini and optionally insert them into Office files."""
from __future__ import annotations

import json
import os
import sys
import uuid
import zipfile
from pathlib import Path

from actions._office_common import safe_filename, safe_output_directory, unique_output_path
from core.path_policy import PathPolicyError, resolve_user_path, user_accessible_roots

_MAX_PROMPT = 12000
_MAX_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_OUTPUT_BYTES = 30 * 1024 * 1024
_MODEL_DEFAULT = "gemini-2.5-flash-image"
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def _api_key() -> str:
    """Read the same local API-key configuration used by the main Gemini client."""
    base_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    config_path = base_dir / "config" / "api_keys.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        key = str(data.get("gemini_api_key", "")).strip()
    except (OSError, ValueError, TypeError):
        key = ""
    if not key:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise ValueError("Gemini API key is not configured. Add gemini_api_key to config/api_keys.json.")
    return key


def _image_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("image_path is required for image editing.")
    path = resolve_user_path(raw.strip(), roots=user_accessible_roots(), must_exist=True)
    if not path.is_file() or path.suffix.lower() not in _IMAGE_EXTENSIONS:
        raise ValueError("image_path must point to an existing PNG, JPEG or WEBP image.")
    if path.stat().st_size > _MAX_SOURCE_BYTES:
        raise ValueError("Source image exceeds the 20 MiB limit.")
    from PIL import Image
    with Image.open(path) as image:
        image.verify()
    return path


def _extract_image(response) -> tuple[bytes, str]:
    """Extract only actual image parts; never treat model text as an image result."""
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            mime = str(getattr(inline, "mime_type", "") or "image/png").lower() if inline else ""
            if data and mime in {"image/png", "image/jpeg", "image/webp"}:
                if isinstance(data, str):
                    import base64
                    data = base64.b64decode(data, validate=True)
                if len(data) <= _MAX_OUTPUT_BYTES:
                    return bytes(data), mime
    raise ValueError("The image model returned no usable image. Try a clearer prompt or another model.")


def _generate(params: dict) -> tuple[Path, str]:
    prompt = str(params.get("prompt", "") or "").strip()
    if not prompt:
        raise ValueError("prompt is required.")
    if len(prompt) > _MAX_PROMPT:
        raise ValueError("prompt must not exceed 12,000 characters.")
    operation = str(params.get("operation", "generate")).strip().lower()
    if operation not in {"generate", "edit"}:
        raise ValueError("operation must be generate or edit.")
    source = _image_path(str(params.get("image_path", "") or "")) if operation == "edit" else None

    # Keep the existing application key store as the source of credentials.
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=_api_key())
    contents = [prompt]
    if source:
        source_mime = {
            ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp",
        }[source.suffix.lower()]
        contents.append(types.Part.from_bytes(data=source.read_bytes(), mime_type=source_mime))
    model = str(params.get("model", "") or _MODEL_DEFAULT).strip()
    if not model or len(model) > 100 or any(ch.isspace() for ch in model):
        raise ValueError("model must be a valid model identifier.")
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )
    image_bytes, mime = _extract_image(response)
    extension = { "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp" }[mime]
    directory_raw = str(params.get("output_dir", "") or "")
    directory = safe_output_directory(directory_raw) if directory_raw else safe_output_directory()
    default_name = str(params.get("filename", "") or ("visual-edit" if source else "generated-visual"))
    filename = safe_filename(default_name, "generated-visual", extension)
    # Always align the extension with the actual MIME type returned by the model.
    filename = str(Path(filename).with_suffix(extension))
    output = unique_output_path(directory, filename)
    temporary = output.with_name(f".{output.stem}-{uuid.uuid4().hex}.tmp{extension}")
    try:
        temporary.write_bytes(image_bytes)
        from PIL import Image
        with Image.open(temporary) as image:
            image.verify()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, output)
    return output, mime


def _insert_docx(image_path: Path, raw_target: str) -> Path:
    from docx import Document
    from docx.enum.text import WD_BREAK
    from docx.shared import Inches
    target = resolve_user_path(raw_target, roots=user_accessible_roots(), must_exist=True)
    if not target.is_file() or target.suffix.lower() != ".docx":
        raise ValueError("DOCX insertion requires an existing .docx file.")
    if target.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("Target document exceeds the 100 MiB limit.")
    output = unique_output_path(target.parent, safe_filename(target.stem + "-visual", target.stem + "-visual", ".docx"))
    document = Document(target)
    paragraph = document.add_paragraph()
    paragraph.alignment = 1
    paragraph.add_run().add_picture(str(image_path), width=Inches(5.8))
    paragraph.add_run().add_break(WD_BREAK.PAGE)
    body = document._element.body
    body.insert(0, paragraph._p)
    temporary = output.with_name(f".{output.stem}-{uuid.uuid4().hex}.tmp.docx")
    try:
        document.save(temporary)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip():
                raise ValueError("Generated Word document failed package validation.")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _insert_pptx(image_path: Path, raw_target: str, slide_index=None) -> Path:
    from pptx import Presentation
    from pptx.util import Inches
    target = resolve_user_path(raw_target, roots=user_accessible_roots(), must_exist=True)
    if not target.is_file() or target.suffix.lower() != ".pptx":
        raise ValueError("PowerPoint insertion requires an existing .pptx file.")
    if target.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("Target presentation exceeds the 100 MiB limit.")
    output = unique_output_path(target.parent, safe_filename(target.stem + "-visual", target.stem + "-visual", ".pptx"))
    presentation = Presentation(target)
    from PIL import Image
    with Image.open(image_path) as image:
        image_width, image_height = image.size
    if slide_index is None:
        # Append a dedicated visual slide; fit without distorting the source image.
        layout = next((item for item in presentation.slide_layouts if len(item.placeholders) == 0), presentation.slide_layouts[-1])
        slide = presentation.slides.add_slide(layout)
        bound_left = bound_top = Inches(0)
        bound_width, bound_height = presentation.slide_width, presentation.slide_height
    else:
        index = int(slide_index)
        if index < 0 or index >= len(presentation.slides):
            raise ValueError(f"slide_index must be between 0 and {len(presentation.slides) - 1}.")
        slide = presentation.slides[index]
        bound_left = bound_top = Inches(1)
        bound_width = presentation.slide_width - Inches(2)
        bound_height = presentation.slide_height - Inches(2)
    scale = min(bound_width / image_width, bound_height / image_height)
    width, height = int(image_width * scale), int(image_height * scale)
    left = bound_left + int((bound_width - width) / 2)
    top = bound_top + int((bound_height - height) / 2)
    slide.shapes.add_picture(str(image_path), left, top, width=width, height=height)
    temporary = output.with_name(f".{output.stem}-{uuid.uuid4().hex}.tmp.pptx")
    try:
        presentation.save(temporary)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip():
                raise ValueError("Generated PowerPoint failed package validation.")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def visual_studio(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "generate") or "generate").strip().lower()
    try:
        if operation in {"generate", "edit"}:
            image_path, mime = _generate(params)
            details = f"Visual saved: {image_path}\nFormat: {mime}"
            target = str(params.get("target_file_path", "") or "").strip()
            if target:
                suffix = Path(target).suffix.lower()
                if suffix == ".docx":
                    inserted = _insert_docx(image_path, target)
                elif suffix == ".pptx":
                    inserted = _insert_pptx(image_path, target, params.get("slide_index"))
                else:
                    return details + "\nAutomatic insertion supports existing .docx and .pptx files; image remains saved separately."
                details += f"\nInserted into a new copy: {inserted}"
            return details
        if operation == "insert":
            image_path = _image_path(str(params.get("image_path", "") or ""))
            target = str(params.get("target_file_path", "") or "").strip()
            if not target:
                raise ValueError("target_file_path is required for insert.")
            suffix = Path(target).suffix.lower()
            if suffix == ".docx":
                inserted = _insert_docx(image_path, target)
            elif suffix == ".pptx":
                inserted = _insert_pptx(image_path, target, params.get("slide_index"))
            else:
                raise ValueError("Insert target must be an existing .docx or .pptx file.")
            return f"Image inserted into a new copy: {inserted}\nSource image and original document/presentation were preserved."
        return "Unsupported operation. Use generate, edit, or insert."
    except ImportError as exc:
        return f"Visual Studio dependency unavailable: {exc}. Check google-genai and Pillow installation."
    except (OSError, ValueError, TypeError, KeyError, PathPolicyError, zipfile.BadZipFile) as exc:
        return f"Visual Studio failed safely: {exc}"
    except Exception as exc:
        # Avoid echoing request headers, credentials or full SDK internals into speech.
        return f"Visual generation failed: {type(exc).__name__}. Check network access, API quota and model availability."


TOOL = {
    "name": "visual_studio",
    "description": (
        "Generate or edit images with the configured Gemini image model and optionally insert the result into a new copy of an existing Word or PowerPoint file. "
        "Operations: generate (prompt to image), edit (prompt plus existing image_path), insert (place existing image into a DOCX cover page or add it to a PPTX slide). "
        "Supports covers, illustrations, posters, backgrounds and visual assets. Uses the app's config/api_keys.json Gemini key; never ask the user to paste the key. "
        "Existing files are preserved; DOCX gets a cover page, PPTX gets a full-bleed new slide by default or an inset image on slide_index. "
        "Use exact local paths and report output paths only after success."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "description": "generate | edit | insert"},
            "prompt": {"type": "STRING", "description": "Detailed visual brief; required for generate/edit"},
            "image_path": {"type": "STRING", "description": "Existing PNG/JPEG/WEBP source for edit or insert"},
            "target_file_path": {"type": "STRING", "description": "Optional existing DOCX/PPTX to receive the generated image automatically"},
            "slide_index": {"type": "INTEGER", "description": "Optional zero-based existing PowerPoint slide index; omit to append a full-bleed visual slide"},
            "output_dir": {"type": "STRING", "description": "Optional output directory inside permitted user roots"},
            "filename": {"type": "STRING", "description": "Optional output filename, without directories"},
            "model": {"type": "STRING", "description": "Optional Gemini image model identifier; defaults to gemini-2.5-flash-image"}
        },
        "required": ["operation"]
    },
    "handler": visual_studio
}
