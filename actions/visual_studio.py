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
_MAX_PIXELS = 100_000_000
_MODEL_DEFAULT = "gemini-2.5-flash-image"
_ALLOWED_ASPECT_RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
_ALLOWED_IMAGE_SIZES = {"1K", "2K", "4K"}
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


def _font(size: int):
    """Choose a dependable local font without requiring font installation."""
    from PIL import ImageFont
    candidates = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return ImageFont.truetype(str(candidate), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _compose_cover_text(image_path: Path, params: dict) -> Path:
    """Add readable, deterministic cover typography while retaining the raw asset."""
    title = str(params.get("cover_title", "") or "").strip()
    subtitle = str(params.get("cover_subtitle", "") or "").strip()
    author = str(params.get("cover_author", "") or "").strip()
    if not any((title, subtitle, author)):
        return image_path
    from PIL import Image, ImageDraw
    with Image.open(image_path) as opened:
        opened.load()
        if opened.width * opened.height > _MAX_PIXELS:
            raise ValueError("Generated image exceeds the safe pixel limit.")
        base = opened.convert("RGBA")
    width, height = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gradient_draw = ImageDraw.Draw(overlay)
    start_y = int(height * 0.52)
    for y in range(start_y, height):
        alpha = int(35 + 175 * ((y - start_y) / max(1, height - start_y)))
        gradient_draw.line((0, y, width, y), fill=(8, 12, 20, alpha))
    composed = Image.alpha_composite(base, overlay)
    draw = ImageDraw.Draw(composed)
    margin = max(28, int(width * 0.075))
    available = max(100, width - margin * 2)
    title_font = _font(max(20, min(84, int(width * 0.075))))
    subtitle_font = _font(max(14, min(42, int(width * 0.035))))
    author_font = _font(max(14, min(36, int(width * 0.028))))

    def wrap(text: str, font, max_width: int):
        lines, current = [], ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines

    blocks = []
    if title:
        blocks.append((title_font, wrap(title, title_font, available), (255, 255, 255, 255), max(4, int(height * 0.012))))
    if subtitle:
        blocks.append((subtitle_font, wrap(subtitle, subtitle_font, available), (235, 239, 245, 255), max(3, int(height * 0.008))))
    if author:
        blocks.append((author_font, wrap(author, author_font, available), (240, 196, 90, 255), max(4, int(height * 0.018))))
    line_gap = max(4, int(height * 0.008))
    measured, total = [], 0
    for font, lines, fill, gap in blocks:
        heights = [draw.textbbox((0, 0), line, font=font)[3] - draw.textbbox((0, 0), line, font=font)[1] for line in lines]
        block_height = sum(heights) + line_gap * max(0, len(lines) - 1)
        measured.append((font, lines, fill, gap, heights))
        total += block_height + gap
    y = max(start_y + int(height * 0.04), height - margin - total)
    for font, lines, fill, gap, heights in measured:
        for line, line_height in zip(lines, heights):
            draw.text((margin, y), line, font=font, fill=fill, stroke_width=max(1, width // 1200), stroke_fill=(0, 0, 0, 180))
            y += line_height + line_gap
        y += gap
    output = unique_output_path(image_path.parent, safe_filename(image_path.stem + "-designed", "designed-cover", image_path.suffix))
    temporary = output.with_name(f".{output.stem}-{uuid.uuid4().hex}.tmp{output.suffix}")
    try:
        if output.suffix.lower() in {".jpg", ".jpeg"}:
            composed.convert("RGB").save(temporary, format="JPEG", quality=94, optimize=True)
        elif output.suffix.lower() == ".webp":
            composed.save(temporary, format="WEBP", quality=94, method=6)
        else:
            composed.save(temporary, format="PNG", optimize=True)
        with Image.open(temporary) as check:
            check.verify()
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _preflight_target(raw_target: str, slide_index=None) -> Path | None:
    """Validate an Office target before consuming image-generation quota."""
    if not raw_target.strip():
        return None
    target = resolve_user_path(raw_target.strip(), roots=user_accessible_roots(), must_exist=True)
    if not target.is_file() or target.suffix.lower() not in {".docx", ".pptx"}:
        raise ValueError("target_file_path must point to an existing .docx or .pptx file.")
    if target.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("Target Office file exceeds the 100 MiB limit.")
    if target.suffix.lower() == ".pptx" and slide_index is not None:
        from pptx import Presentation
        presentation = Presentation(target)
        if isinstance(slide_index, bool) or not isinstance(slide_index, int) or not 0 <= slide_index < len(presentation.slides):
            raise ValueError(f"slide_index must be between 0 and {len(presentation.slides) - 1}.")
    return target


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
    aspect_ratio = str(params.get("aspect_ratio", "") or "").strip()
    image_size = str(params.get("image_size", "") or "").strip().upper()
    if aspect_ratio and aspect_ratio not in _ALLOWED_ASPECT_RATIOS:
        raise ValueError("Unsupported aspect_ratio. Use a documented image ratio.")
    if image_size and image_size not in _ALLOWED_IMAGE_SIZES:
        raise ValueError("image_size must be 1K, 2K or 4K.")
    config_options = {"response_modalities": ["TEXT", "IMAGE"]}
    if aspect_ratio or image_size:
        image_config = {}
        if aspect_ratio:
            image_config["aspect_ratio"] = aspect_ratio
        if image_size:
            image_config["image_size"] = image_size
        config_options["image_config"] = types.ImageConfig(**image_config)
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(**config_options),
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
        with Image.open(temporary) as image:
            if image.width * image.height > _MAX_PIXELS:
                raise ValueError("Generated image exceeds the safe pixel limit.")
            image.load()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, output)
    return _compose_cover_text(output, params), mime


def _insert_docx(image_path: Path, raw_target: str, insertion_mode: str = "cover", caption: str = "") -> Path:
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
    if insertion_mode not in {"cover", "inline"}:
        raise ValueError("docx_insertion_mode must be cover or inline.")
    from PIL import Image
    with Image.open(image_path) as image:
        image_width, image_height = image.size
    max_width, max_height = Inches(5.8), Inches(7.5 if insertion_mode == "cover" else 6.0)
    scale = min(max_width / image_width, max_height / image_height)
    paragraph = document.add_paragraph()
    paragraph.alignment = 1
    paragraph.add_run().add_picture(str(image_path), width=int(image_width * scale), height=int(image_height * scale))
    caption_paragraph = None
    if caption.strip():
        caption_paragraph = document.add_paragraph(caption.strip(), style="Caption")
        caption_paragraph.alignment = 1
    body = document._element.body
    if insertion_mode == "cover":
        page_break = document.add_paragraph()
        page_break.add_run().add_break(WD_BREAK.PAGE)
        body.insert(0, page_break._p)
        if caption_paragraph is not None:
            body.insert(0, caption_paragraph._p)
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


def _insert_pptx(image_path: Path, raw_target: str, slide_index=None, caption: str = "") -> Path:
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
    picture = slide.shapes.add_picture(str(image_path), left, top, width=width, height=height)
    picture.name = "Mark-LV generated visual"
    if caption.strip():
        from pptx.enum.text import PP_ALIGN
        from pptx.util import Pt
        box = slide.shapes.add_textbox(Inches(0.25), presentation.slide_height - Inches(0.48), presentation.slide_width - Inches(0.5), Inches(0.3))
        frame = box.text_frame
        frame.text = caption.strip()
        frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        frame.paragraphs[0].font.size = Pt(12)
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
            target = str(params.get("target_file_path", "") or "").strip()
            if target:
                _preflight_target(target, params.get("slide_index"))
            image_path, mime = _generate(params)
            details = f"Visual saved: {image_path}\nFormat: {mime}"
            target = str(params.get("target_file_path", "") or "").strip()
            if target:
                target_path = _preflight_target(target, params.get("slide_index"))
                caption = str(params.get("caption", "") or "")
                if target_path.suffix.lower() == ".docx":
                    mode = str(params.get("docx_insertion_mode", "cover") or "cover").lower()
                    inserted = _insert_docx(image_path, target, mode, caption)
                else:
                    inserted = _insert_pptx(image_path, target, params.get("slide_index"), caption)
                details += f"\nInserted into a new copy: {inserted}"
            return details
        if operation == "insert":
            image_path = _image_path(str(params.get("image_path", "") or ""))
            target = str(params.get("target_file_path", "") or "").strip()
            if not target:
                raise ValueError("target_file_path is required for insert.")
            target_path = _preflight_target(target, params.get("slide_index"))
            caption = str(params.get("caption", "") or "")
            if target_path.suffix.lower() == ".docx":
                mode = str(params.get("docx_insertion_mode", "cover") or "cover").lower()
                inserted = _insert_docx(image_path, target, mode, caption)
            else:
                inserted = _insert_pptx(image_path, target, params.get("slide_index"), caption)
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
        "Supports covers, illustrations, posters, backgrounds and visual assets. Optional aspect_ratio and image_size tune generation. "
        "cover_title, cover_subtitle and cover_author add readable typography to a designed copy while preserving the raw generated image. "
        "Uses the app's config/api_keys.json Gemini key; never ask the user to paste the key. Existing files are preserved. "
        "DOCX supports docx_insertion_mode=cover|inline; PPTX appends a fitted visual slide by default or inserts into a zero-based slide_index. Optional caption is supported. "
        "The target is validated before generation to avoid wasting quota on invalid Office paths. Use exact local paths and report output paths only after success."
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
            "model": {"type": "STRING", "description": "Optional Gemini image model identifier; defaults to gemini-2.5-flash-image"},
            "aspect_ratio": {"type": "STRING", "description": "Optional ratio: 1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9 or 21:9"},
            "image_size": {"type": "STRING", "description": "Optional image size: 1K, 2K or 4K; model availability varies"},
            "cover_title": {"type": "STRING", "description": "Optional exact title overlaid on a designed copy"},
            "cover_subtitle": {"type": "STRING", "description": "Optional exact subtitle overlaid on a designed copy"},
            "cover_author": {"type": "STRING", "description": "Optional exact author or brand overlaid on a designed copy"},
            "docx_insertion_mode": {"type": "STRING", "description": "cover | inline; defaults to cover"},
            "caption": {"type": "STRING", "description": "Optional caption for Word or PowerPoint"}
        },
        "required": ["operation"]
    },
    "handler": visual_studio
}
