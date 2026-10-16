"""Local image resize, format conversion, compression and metadata tools."""
from __future__ import annotations

from core.path_policy import PathPolicyError, resolve_user_path, user_accessible_roots
from actions._office_common import safe_filename, safe_output_directory, unique_output_path


def image_tools(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    raw_path = str(params.get("file_path", "") or "").strip()
    if not raw_path:
        return "file_path is required."
    try:
        source = resolve_user_path(raw_path, roots=user_accessible_roots(), must_exist=True)
        if not source.is_file():
            return "file_path must point to a file."
        from PIL import Image, UnidentifiedImageError
        with Image.open(source) as image:
            image.verify()
        with Image.open(source) as image:
            operation = str(params.get("operation", "info")).strip().lower()
            if operation == "info":
                return f"Image: {source}\nFormat: {image.format}\nDimensions: {image.width} × {image.height}\nMode: {image.mode}\nBytes: {source.stat().st_size}"
            if operation not in {"resize", "convert", "compress"}:
                return "Unsupported operation. Use info, resize, convert, or compress."
            directory = safe_output_directory(str(params.get("output_dir", "") or str(source.parent)))
            fmt = str(params.get("format", "png" if operation == "resize" else "jpg")).strip().lower().lstrip(".")
            fmt_map = {"jpg": ("JPEG", ".jpg"), "jpeg": ("JPEG", ".jpg"), "png": ("PNG", ".png"), "webp": ("WEBP", ".webp"), "bmp": ("BMP", ".bmp"), "tiff": ("TIFF", ".tiff")}
            if operation == "resize":
                width = params.get("width")
                height = params.get("height")
                scale = params.get("scale")
                if scale is not None:
                    scale = float(scale)
                    if not 0.01 <= scale <= 10:
                        return "scale must be between 0.01 and 10."
                    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
                elif width is not None or height is not None:
                    width = int(width) if width is not None else max(1, round(image.width * int(height) / image.height))
                    height = int(height) if height is not None else max(1, round(image.height * int(width) / image.width))
                    if not (1 <= width <= 20000 and 1 <= height <= 20000):
                        return "Image dimensions must be between 1 and 20,000 pixels."
                    size = (width, height)
                else:
                    return "Provide width, height, or scale."
                result = image.resize(size, Image.Resampling.LANCZOS)
                fmt = str(params.get("format", image.format or "PNG")).lower().lstrip(".")
                out_format, ext = fmt_map.get(fmt, (None, None))
                if not out_format:
                    return "Unsupported output format."
                filename = safe_filename(str(params.get("filename", "") or ""), source.stem + "-resized", ext)
                output = unique_output_path(directory, filename)
                if out_format == "JPEG" and result.mode not in ("RGB", "L"):
                    result = result.convert("RGB")
                result.save(output, format=out_format)
                return f"Resized image to {size[0]} × {size[1]} and saved: {output}"
            if operation == "convert":
                out_format, ext = fmt_map.get(fmt, (None, None))
                if not out_format:
                    return "Unsupported output format. Use jpg, png, webp, bmp, or tiff."
                result = image.copy()
                if out_format == "JPEG" and result.mode not in ("RGB", "L"):
                    result = result.convert("RGB")
                output = unique_output_path(directory, safe_filename(str(params.get("filename", "") or ""), source.stem + "-converted", ext))
                result.save(output, format=out_format)
                return f"Converted image saved: {output}"
            quality = int(params.get("quality", 80))
            if not 1 <= quality <= 95:
                return "quality must be between 1 and 95."
            result = image.convert("RGB")
            output = unique_output_path(directory, safe_filename(str(params.get("filename", "") or ""), source.stem + "-compressed", ".jpg"))
            result.save(output, "JPEG", quality=quality, optimize=True)
            return f"Compressed image saved: {output}\nOriginal bytes: {source.stat().st_size}; output bytes: {output.stat().st_size}"
    except ImportError:
        return "Pillow is not installed. Install it with: pip install Pillow"
    except (OSError, ValueError, TypeError, PathPolicyError) as exc:
        return f"Image operation failed safely: {exc}"
    except Exception as exc:
        return f"Image operation failed: {exc}"


TOOL = {
    "name": "image_tools",
    "description": "Edit local images: inspect metadata, resize, convert among JPG/PNG/WEBP/BMP/TIFF, and compress to JPEG. The original image is preserved and outputs use new filenames.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "description": "info | resize | convert | compress"},
            "file_path": {"type": "STRING", "description": "Existing image path"},
            "output_dir": {"type": "STRING", "description": "Optional output folder inside the current user's home directory or OS temporary directory"},
            "filename": {"type": "STRING", "description": "Optional output filename only"},
            "format": {"type": "STRING", "description": "jpg | png | webp | bmp | tiff"},
            "width": {"type": "INTEGER", "description": "Target width for resize"},
            "height": {"type": "INTEGER", "description": "Target height for resize"},
            "scale": {"type": "NUMBER", "description": "Scale factor between 0.01 and 10"},
            "quality": {"type": "INTEGER", "description": "JPEG compression quality, 1-95"}
        },
        "required": ["operation", "file_path"]
    },
    "handler": image_tools
}
