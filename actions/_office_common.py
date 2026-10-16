"""Shared, path-policy-aware helpers for local Office productivity actions.

This module is intentionally prefixed with an underscore so action_loader does not
expose it as a tool.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.path_policy import PathPolicyError, resolve_child_path, resolve_user_path, user_accessible_roots


def safe_output_directory(raw_directory: str = "") -> Path:
    """Create and validate an output directory inside the user's permitted roots."""
    requested = (raw_directory or "").strip()
    default = Path.home() / "Documents" / "MarkLV"
    target = Path(requested).expanduser() if requested else default
    # Resolve against the same explicit roots used by the existing file actions.
    target = resolve_user_path(target, roots=user_accessible_roots())
    target.mkdir(parents=True, exist_ok=True)
    # Resolve again after mkdir to catch symlinked parent components.
    return resolve_user_path(target, roots=user_accessible_roots(), must_exist=True)


def safe_filename(raw_name: str, default_stem: str, extension: str) -> str:
    """Return a plain filename; paths, traversal and unexpected extensions are rejected."""
    name = (raw_name or "").strip()
    if not name:
        name = default_stem
    if "/" in name or "\\" in name or name in {".", ".."} or "\x00" in name:
        raise PathPolicyError("Provide a filename only, without directories or traversal.")
    path = Path(name)
    stem = path.stem if path.suffix else name
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" ._-")
    if not stem:
        stem = default_stem
    ext = extension if extension.startswith(".") else "." + extension
    return stem + ext


def unique_output_path(directory: Path, filename: str) -> Path:
    """Resolve a new output file under directory without silently overwriting."""
    base = Path(filename)
    candidate = resolve_child_path(directory, base.name, roots=user_accessible_roots())
    if not candidate.exists():
        return candidate
    for index in range(2, 10000):
        candidate = resolve_child_path(
            directory, f"{base.stem}-{index}{base.suffix}", roots=user_accessible_roots()
        )
        if not candidate.exists():
            return candidate
    raise FileExistsError("Could not allocate a unique output filename.")
