"""Shared path validation for user-facing file actions.

Paths are canonicalized before use and must remain inside explicitly allowed roots.
This is a containment check, not a replacement for OS permissions or race-free
file-descriptor-based operations.
"""
from __future__ import annotations

import tempfile
from pathlib import Path, PureWindowsPath
from typing import Iterable


class PathPolicyError(ValueError):
    """Raised when a requested path escapes its authorized root."""


def user_accessible_roots() -> tuple[Path, ...]:
    """Conservative roots for files the app processes: home and OS temp."""
    roots = [Path.home(), Path(tempfile.gettempdir())]
    result: list[Path] = []
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        if resolved not in result:
            result.append(resolved)
    return tuple(result)


def _resolved_roots(roots: Iterable[str | Path] | None) -> tuple[Path, ...]:
    candidates = user_accessible_roots() if roots is None else tuple(Path(p) for p in roots)
    resolved: list[Path] = []
    for root in candidates:
        try:
            item = root.expanduser().resolve()
        except OSError:
            continue
        if item not in resolved:
            resolved.append(item)
    return tuple(resolved)


def is_within_roots(path: str | Path, roots: Iterable[str | Path] | None = None) -> bool:
    try:
        target = Path(path).expanduser().resolve()
        for root in _resolved_roots(roots):
            if target == root or target.is_relative_to(root):
                return True
    except (OSError, RuntimeError, ValueError):
        return False
    return False


def resolve_user_path(
    raw_path: str | Path,
    *,
    base: str | Path | None = None,
    roots: Iterable[str | Path] | None = None,
    must_exist: bool = False,
) -> Path:
    """Resolve a user-supplied path and reject paths outside allowed roots."""
    raw_text = str(raw_path)
    if not raw_text.strip() or "\x00" in raw_text:
        raise PathPolicyError("Path is empty or contains an invalid NUL character.")

    candidate = Path(raw_text).expanduser()
    if base is not None and not candidate.is_absolute():
        candidate = Path(base).expanduser() / candidate

    try:
        resolved = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathPolicyError(f"Cannot resolve path safely: {raw_text}") from exc

    if not is_within_roots(resolved, roots):
        raise PathPolicyError("Access denied: path is outside the permitted directories.")
    if must_exist and not resolved.exists():
        raise PathPolicyError("Path does not exist.")
    return resolved


def resolve_child_path(
    root: str | Path,
    child: str | Path,
    *,
    roots: Iterable[str | Path] | None = None,
    must_exist: bool = False,
) -> Path:
    """Resolve a relative child beneath root; reject absolute paths and traversal."""
    child_text = str(child)
    if not child_text.strip() or "\x00" in child_text:
        raise PathPolicyError("A valid relative child path is required.")
    windows_path = PureWindowsPath(child_text)
    child_path = Path(child_text.replace("\\", "/"))
    if child_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise PathPolicyError("Absolute child paths are not allowed.")
    if any(part == ".." for part in child_path.parts):
        raise PathPolicyError("Path traversal ('..') is not allowed.")
    if not child_path.parts or any(part in ("", ".") for part in child_path.parts):
        raise PathPolicyError("A valid relative child path is required.")

    allowed = _resolved_roots(roots)
    resolved_root = resolve_user_path(root, roots=allowed)
    resolved = resolve_user_path(child_path, base=resolved_root, roots=allowed, must_exist=must_exist)
    if resolved != resolved_root and not resolved.is_relative_to(resolved_root):
        raise PathPolicyError("Access denied: target escapes its parent directory.")
    return resolved
