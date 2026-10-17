"""Durable, project-bound checkpoints for development-agent build tasks.

Task metadata and generated checkpoint files stay in the local application
store. Persisted running tasks become interrupted on process restart and are
never replayed automatically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.dev_project_context import project_identity
from core.path_policy import PathPolicyError, resolve_child_path, resolve_user_path

_LOCK = threading.RLock()
_RECOVERED: set[str] = set()
_TASK_ID = re.compile(r"^[a-f0-9]{32}$")
MAX_TASKS = 200
MAX_CHECKPOINT_BYTES = 10 * 1024 * 1024
MAX_TASK_EVENTS = 200
_TERMINAL = {"completed", "failed", "cancelled"}


class TaskCancelled(Exception):
    """Raised at a safe boundary after an explicit task cancellation."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_event(record: dict[str, Any], event_type: str, message: str | None = None) -> None:
    """Append a bounded, user-readable state transition to the durable task timeline."""
    events = record.get("events")
    if not isinstance(events, list):
        events = []
    last_sequence = 0
    if events and isinstance(events[-1], dict):
        try:
            last_sequence = int(events[-1].get("sequence", 0))
        except (TypeError, ValueError):
            last_sequence = len(events)
    event = {
        "sequence": last_sequence + 1,
        "at": _now(), "type": str(event_type)[:40],
        "phase": str(record.get("phase", ""))[:80],
        "status": str(record.get("status", ""))[:40],
        "message": str(message if message is not None else record.get("message", ""))[:500],
    }
    if events and isinstance(events[-1], dict):
        previous = events[-1]
        if all(previous.get(key) == event.get(key) for key in ("type", "phase", "status", "message")):
            return
    record["events"] = (events + [event])[-MAX_TASK_EVENTS:]


def _paths(store_dir: str | Path, task_id: str) -> tuple[Path, Path]:
    if not _TASK_ID.fullmatch(str(task_id or "")):
        raise ValueError("Invalid development task ID.")
    root = Path(store_dir).expanduser().resolve()
    return root / "tasks" / f"{task_id}.json", root / "task_artifacts" / task_id


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".task-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows Defender/indexers can briefly hold the newly flushed temp
        # file. Retry transient sharing/permission locks without weakening the
        # atomic replace or falling back to a non-atomic write.
        for attempt in range(5):
            try:
                os.replace(temp_name, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.03 * (2 ** attempt))
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Development task record could not be read safely: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Development task record is malformed.")
    return data


def _recover_once(store_dir: str | Path) -> None:
    store = str(Path(store_dir).expanduser().resolve())
    with _LOCK:
        if store in _RECOVERED:
            return
        task_dir = Path(store) / "tasks"
        if task_dir.is_dir():
            for path in task_dir.glob("*.json"):
                try:
                    record = _read(path)
                    if record.get("status") in {"running", "awaiting_approval", "paused", "queued"}:
                        uncertain = record.get("phase") in {"applying", "verifying", "dependency_install"}
                        record.update({
                            "status": "unknown_after_restart" if uncertain else "interrupted",
                            "phase": "reconciliation_required" if uncertain else "recovery_required",
                            "message": ("The application stopped after an approved side effect may have started. Inspect the project and operation journal; automatic resume is blocked."
                                        if uncertain else "The application stopped during a checkpointed preparation phase. No project side effect was replayed; inspect its checkpoint before resuming."),
                            "updated_at": _now(),
                        })
                        _append_event(record, "recovery")
                        _atomic_json(path, record)
                except (OSError, ValueError):
                    continue
        _RECOVERED.add(store)


def create_task(project_dir: str | Path, store_dir: str | Path, *,
                goal: str, language: str, project_name: str) -> dict[str, Any]:
    _recover_once(store_dir)
    identity, canonical = project_identity(project_dir)
    task_id = uuid.uuid4().hex
    path, artifact_root = _paths(store_dir, task_id)
    record = {
        "task_id": task_id, "project_id": identity, "project_path": canonical,
        "project_name": str(project_name)[:100], "goal": str(goal)[:2000],
        "language": str(language)[:80], "status": "running", "phase": "planning",
        "control": "continue", "message": "Planning project architecture.",
        "todo": [{"id": "plan", "label": "Plan architecture and acceptance criteria", "state": "in_progress"},
                 {"id": "generate", "label": "Generate and checkpoint source files", "state": "pending"},
                 {"id": "review", "label": "Review exact changes and approve writes", "state": "pending"},
                 {"id": "verify", "label": "Verify in an isolated environment", "state": "pending"}],
        "plan": None, "generated": {}, "created_at": _now(), "updated_at": _now(),
        "events": [],
    }
    _append_event(record, "created")
    with _LOCK:
        records = sorted((Path(store_dir) / "tasks").glob("*.json"), key=lambda item: item.stat().st_mtime)
        while len(records) >= MAX_TASKS:
            old = records.pop(0)
            try:
                previous = _read(old)
                if previous.get("status") in _TERMINAL:
                    old.unlink(missing_ok=True)
                    shutil_path = Path(store_dir) / "task_artifacts" / old.stem
                    if shutil_path.exists():
                        shutil.rmtree(shutil_path, ignore_errors=True)
            except (OSError, ValueError):
                old.unlink(missing_ok=True)
        artifact_root.mkdir(parents=True, exist_ok=True)
        _atomic_json(path, record)
    return dict(record)


def get_task(task_id: str, store_dir: str | Path, *,
             project_dir: str | Path | None = None) -> dict[str, Any] | None:
    _recover_once(store_dir)
    path, _ = _paths(store_dir, task_id)
    with _LOCK:
        if not path.is_file():
            return None
        record = _read(path)
        if project_dir is not None:
            identity, canonical = project_identity(project_dir)
            if record.get("project_id") != identity or record.get("project_path") != canonical:
                raise ValueError("Task belongs to a different project; refusing to expose or reuse its checkpoint.")
        return record


def list_tasks(store_dir: str | Path, *, project_dir: str | Path | None = None,
               limit: int = 20) -> list[dict[str, Any]]:
    _recover_once(store_dir)
    identity = project_identity(project_dir)[0] if project_dir is not None else None
    task_dir = Path(store_dir).expanduser().resolve() / "tasks"
    results = []
    if not task_dir.is_dir():
        return results
    for path in sorted(task_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            record = _read(path)
            if identity is None or record.get("project_id") == identity:
                results.append(record)
            if len(results) >= max(1, min(100, int(limit))):
                break
        except (OSError, ValueError):
            continue
    return results


def update_task(task_id: str, store_dir: str | Path, *, phase: str | None = None,
                status: str | None = None, message: str | None = None,
                todo: list[dict[str, str]] | None = None,
                plan: dict[str, Any] | None = None,
                operation_id: str | None = None) -> dict[str, Any]:
    _recover_once(store_dir)
    path, _ = _paths(store_dir, task_id)
    with _LOCK:
        record = _read(path)
        previous_state = (record.get("phase"), record.get("status"), record.get("message"))
        if record.get("status") in _TERMINAL:
            return record
        if phase is not None:
            record["phase"] = str(phase)[:80]
        if status is not None:
            record["status"] = str(status)[:40]
        if message is not None:
            record["message"] = str(message)[:1000]
        if todo is not None:
            record["todo"] = todo[:100]
        if plan is not None:
            record["plan"] = plan
        if operation_id is not None:
            record["operation_id"] = str(operation_id)[:64]
        terminal = record.get("status") in _TERMINAL
        if terminal:
            record["generated"] = {}
            record["plan"] = None
        next_state = (record.get("phase"), record.get("status"), record.get("message"))
        if next_state != previous_state:
            _append_event(record, "state_change")
        record["updated_at"] = _now()
        _atomic_json(path, record)
        if terminal:
            _, artifact_root = _paths(store_dir, task_id)
            shutil.rmtree(artifact_root, ignore_errors=True)
        return record


def checkpoint(task_id: str, store_dir: str | Path, *, phase: str,
               message: str, poll_seconds: float = 0.5) -> dict[str, Any]:
    """Persist a safe boundary and honor pause/cancel requests between operations."""
    path, _ = _paths(store_dir, task_id)
    while True:
        with _LOCK:
            record = _read(path)
            control = record.get("control", "continue")
            if control == "cancel":
                record.update({"status": "cancelled", "phase": phase,
                               "message": "Cancelled at a safe checkpoint; completed effects were not replayed.",
                               "generated": {}, "plan": None,
                               "updated_at": _now()})
                _append_event(record, "cancelled")
                _atomic_json(path, record)
                _, artifact_root = _paths(store_dir, task_id)
                shutil.rmtree(artifact_root, ignore_errors=True)
                raise TaskCancelled(record["message"])
            if control == "pause":
                record.update({"status": "paused", "phase": phase,
                               "message": "Paused at a safe checkpoint. Resume this task to continue.",
                               "updated_at": _now()})
                _append_event(record, "paused")
                _atomic_json(path, record)
            else:
                record.update({"status": "running", "phase": phase,
                               "message": str(message)[:1000], "updated_at": _now()})
                _append_event(record, "checkpoint")
                _atomic_json(path, record)
                return record
        time.sleep(max(0.1, min(2.0, poll_seconds)))


def control_task(task_id: str, store_dir: str | Path, action: str) -> dict[str, Any]:
    _recover_once(store_dir)
    path, _ = _paths(store_dir, task_id)
    action = str(action).strip().lower()
    if action not in {"pause", "resume", "cancel"}:
        raise ValueError("Task action must be pause, resume, or cancel.")
    with _LOCK:
        record = _read(path)
        status = record.get("status")
        if status in _TERMINAL:
            raise ValueError(f"Task is already {status} and cannot be controlled.")
        if status == "unknown_after_restart":
            raise ValueError("Task outcome is uncertain after restart; reconcile project files and operation status before starting new work.")
        if action in {"pause", "cancel"} and record.get("phase") in {"applying", "verifying", "dependency_install"}:
            raise ValueError("The task is in an approved side-effect phase and cannot be interrupted safely; wait for its result, then inspect operation status.")
        if action == "pause":
            if status not in {"running", "awaiting_approval"}:
                raise ValueError(f"Only a running or approval-waiting task can pause (current state: {status}).")
            record["control"] = "pause"
            record["message"] = "Pause requested; the task will stop at its next safe checkpoint."
        elif action == "resume":
            if status == "interrupted":
                record["status"] = "queued"
                record["control"] = "continue"
                record["message"] = "Recovery requested. Call resume with this task ID to continue from its last verified checkpoint."
            elif status == "paused":
                record["control"] = "continue"
                record["status"] = "running"
                record["message"] = "Resume requested; the worker will continue at its next safe checkpoint."
            elif status == "awaiting_approval" and record.get("control") == "pause":
                record["control"] = "continue"
                record["message"] = "Pause request cleared; the task remains at its approval checkpoint."
            else:
                raise ValueError(f"Task cannot resume from state {status}.")
        else:
            record["control"] = "cancel"
            record["message"] = "Cancellation requested; the task will stop at its next safe checkpoint."
            if status in {"interrupted", "queued", "awaiting_approval"}:
                record.update({"status": "cancelled", "phase": "cancelled",
                               "message": "Checkpointed task cancelled without replaying any effects.",
                               "generated": {}, "plan": None})
        record["updated_at"] = _now()
        _append_event(record, f"control_{action}")
        _atomic_json(path, record)
        if record.get("status") == "cancelled":
            _, artifact_root = _paths(store_dir, task_id)
            shutil.rmtree(artifact_root, ignore_errors=True)
        return record


def save_generated_file(task_id: str, store_dir: str | Path, relative_path: str,
                       content: str) -> dict[str, Any]:
    path, artifact_root = _paths(store_dir, task_id)
    encoded = str(content).encode("utf-8")
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise ValueError("Generated checkpoint file exceeds the safe size limit.")
    with _LOCK:
        record = _read(path)
        safe_root = resolve_user_path(artifact_root, roots=[Path.home()])
        target = resolve_child_path(safe_root, relative_path, roots=[Path.home()])
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".generated-", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        record.setdefault("generated", {})[relative_path] = {
            "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded),
        }
        _append_event(record, "file_checkpoint", f"Saved generated file: {relative_path}")
        record["updated_at"] = _now()
        _atomic_json(path, record)
        return record


def load_generated_files(task_id: str, store_dir: str | Path,
                         project_dir: str | Path) -> dict[str, str]:
    record = get_task(task_id, store_dir, project_dir=project_dir)
    if not record:
        raise ValueError("Development task was not found.")
    _, artifact_root = _paths(store_dir, task_id)
    safe_root = resolve_user_path(artifact_root, roots=[Path.home()], must_exist=True)
    result: dict[str, str] = {}
    for relative, metadata in record.get("generated", {}).items():
        target = resolve_child_path(safe_root, relative, roots=[Path.home()], must_exist=True)
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"Task checkpoint is not a regular file: {relative}")
        data = target.read_bytes()
        if len(data) != metadata.get("bytes") or hashlib.sha256(data).hexdigest() != metadata.get("sha256"):
            raise ValueError(f"Task checkpoint integrity check failed: {relative}")
        result[relative] = data.decode("utf-8")
    return result
