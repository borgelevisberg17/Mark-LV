"""Small, durable engineering context records, isolated per project path.

Only project metadata is stored here: never source contents, credentials, or
environment variables. The hashed canonical path prevents similarly named
projects from sharing context.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

_LOCK = RLock()
MAX_HISTORY = 50


def project_identity(project_dir: str | Path) -> tuple[str, str]:
    # normcase folds case on Windows while preserving case-sensitive roots on POSIX.
    canonical = os.path.normcase(str(Path(project_dir).expanduser().resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24], canonical


def _context_path(project_dir: str | Path, store_dir: str | Path) -> tuple[Path, str, str]:
    identity, canonical = project_identity(project_dir)
    root = Path(store_dir).expanduser().resolve()
    return root / f"{identity}.json", identity, canonical


def load_project_context(project_dir: str | Path, store_dir: str | Path) -> dict:
    path, identity, canonical = _context_path(project_dir, store_dir)
    with _LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"project_id": identity, "project_path": canonical,
                    "goals": [], "architecture": "", "stack": [],
                    "acceptance_criteria": [], "decisions": [], "history": []}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Project context could not be read safely: {exc}") from exc
    if data.get("project_id") != identity or data.get("project_path") != canonical:
        raise ValueError("Project context identity mismatch; refusing to reuse another project's history.")
    return data


def save_project_context(project_dir: str | Path, store_dir: str | Path, *,
                         goal: str, language: str, architecture: str,
                         acceptance_criteria: list[str], verification_command: str,
                         status: str, summary: str = "") -> dict:
    path, identity, canonical = _context_path(project_dir, store_dir)
    with _LOCK:
        try:
            data = load_project_context(project_dir, store_dir)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Project context is unavailable: {exc}") from exc
        event = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "goal": goal[:1000], "status": status[:40],
            "summary": summary[:1000], "verification_command": verification_command[:500],
        }
        data.update({
            "project_id": identity, "project_path": canonical,
            "goals": [goal[:1000]], "architecture": architecture[:2000],
            "stack": [language[:80]],
            "acceptance_criteria": [str(item)[:300] for item in acceptance_criteria[:20]],
        })
        data["history"] = (data.get("history", []) + [event])[-MAX_HISTORY:]
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".dev-context-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return data
