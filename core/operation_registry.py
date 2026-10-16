"""Operation lifecycle, idempotency, and append-only audit events.

The audit log intentionally stores metadata, not command output, secrets, or user
payloads. A persisted RUNNING operation after restart is treated as uncertain and
must not be replayed automatically: the side effect may already have happened.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUSES = {
    "awaiting_confirmation", "running", "completed", "failed",
    "cancelled", "expired", "unknown_after_restart",
}
TERMINAL = {"completed", "failed", "cancelled", "expired", "unknown_after_restart"}
MAX_RECORDS = 512
MAX_AUDIT_BYTES = 2 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def stable_key(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


class OperationRegistry:
    """Thread-safe in-memory operation state with a small durable audit journal."""

    def __init__(self, audit_path: Path | None = None):
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._by_key: dict[str, str] = {}
        self._audit_path = audit_path or (Path.home() / ".marklv" / "operation_audit.jsonl")
        self._load_journal()

    def _load_journal(self) -> None:
        try:
            if not self._audit_path.is_file():
                return
            if self._audit_path.stat().st_size > MAX_AUDIT_BYTES:
                # The log is rotated on writes; do not parse an unexpectedly huge file.
                return
            for line in self._audit_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                    operation_id = event.get("operation_id")
                    key_hash = event.get("key_hash")
                    if not operation_id or not key_hash:
                        continue
                    rec = self._records.setdefault(operation_id, {
                        "operation_id": operation_id, "key_hash": key_hash,
                        "title": event.get("title", "Operation"),
                        "category": event.get("category", "general"),
                        "permissions": event.get("permissions", []),
                        "status": "unknown_after_restart",
                        "created_at": event.get("timestamp"),
                        "started_at": None, "finished_at": None,
                        "duration_ms": None, "result": "Outcome may be uncertain after restart.",
                    })
                    rec.update({k: event[k] for k in (
                        "status", "started_at", "finished_at", "duration_ms", "result"
                    ) if k in event})
                    self._by_key[key_hash] = operation_id
                except (ValueError, TypeError):
                    continue
            for rec in self._records.values():
                if rec.get("status") == "running":
                    rec["status"] = "unknown_after_restart"
                    rec["result"] = "Process restarted while this operation was running; verify its effects before retrying."
                elif rec.get("status") == "awaiting_confirmation":
                    rec["status"] = "expired"
                    rec["result"] = "Confirmation was pending when the app restarted; it was not replayed."
        except OSError:
            # Auditing failure must not crash startup; write attempts will be logged in memory.
            return

    def _write_event(self, record: dict[str, Any], event: str) -> None:
        payload = {
            "timestamp": utc_now(), "event": event,
            "operation_id": record["operation_id"],
            "key_hash": record["key_hash"],
            "title": str(record.get("title", "Operation"))[:160],
            "category": str(record.get("category", "general"))[:40],
            "status": record["status"],
            "permissions": list(record.get("permissions", []))[:20],
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "duration_ms": record.get("duration_ms"),
            "result": str(record.get("result", ""))[:500],
        }
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            if self._audit_path.exists() and self._audit_path.stat().st_size > MAX_AUDIT_BYTES:
                rotated = self._audit_path.with_suffix(".jsonl.1")
                try:
                    if rotated.exists():
                        rotated.unlink()
                    self._audit_path.replace(rotated)
                except OSError:
                    pass
            fd = os.open(str(self._audit_path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            # In-memory state remains authoritative for this process.
            pass

    def create(self, idempotency_key: str, title: str, category: str = "general",
               permissions: tuple[str, ...] | list[str] = ()) -> tuple[dict[str, Any], bool]:
        key_hash = stable_key(idempotency_key)
        with self._lock:
            existing_id = self._by_key.get(key_hash)
            if existing_id:
                existing = self._records.get(existing_id)
                if existing and existing.get("status") not in {"cancelled", "expired"}:
                    return dict(existing), False
            now = time.monotonic()
            record = {
                "operation_id": uuid.uuid4().hex,
                "key_hash": key_hash,
                "title": str(title)[:160],
                "category": str(category)[:40],
                "permissions": sorted({str(p)[:48] for p in permissions})[:20],
                "status": "awaiting_confirmation",
                "created_at": utc_now(),
                "started_at": None, "finished_at": None,
                "duration_ms": None, "result": "",
                "_started_mono": now,
            }
            self._records[record["operation_id"]] = record
            self._by_key[key_hash] = record["operation_id"]
            while len(self._records) > MAX_RECORDS:
                oldest = next(iter(self._records))
                old = self._records.pop(oldest)
                if self._by_key.get(old.get("key_hash")) == oldest:
                    self._by_key.pop(old.get("key_hash"), None)
            self._write_event(record, "created")
            return dict(record), True

    def transition(self, operation_id: str, status: str, result: str = "") -> dict[str, Any] | None:
        if status not in STATUSES:
            raise ValueError(f"Unsupported operation status: {status}")
        with self._lock:
            record = self._records.get(operation_id)
            if record is None:
                return None
            previous = record["status"]
            if previous in TERMINAL:
                return dict(record)
            now = time.monotonic()
            if status == "running":
                record["started_at"] = utc_now()
                record["_started_mono"] = now
            if status in TERMINAL:
                record["finished_at"] = utc_now()
                started = record.get("_started_mono")
                if started is not None:
                    record["duration_ms"] = max(0, round((now - started) * 1000))
            record["status"] = status
            record["result"] = str(result or "")[:500]
            self._write_event(record, status)
            return dict(record)

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._records.get(operation_id)
            return dict(rec) if rec else None

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in list(self._records.values())[-max(1, min(100, limit)):][::-1]]


registry = OperationRegistry()


def normalize_result(value: Any, *, operation_id: str, status: str = "completed") -> dict[str, Any]:
    """Normalize legacy string results and exceptions to a stable result shape."""
    if status not in STATUSES:
        status = "failed"
    if isinstance(value, dict):
        message = str(value.get("message", value.get("result", "")))
        status = value.get("status", status)
    else:
        message = str(value or "")
    return {"operation_id": operation_id, "status": status, "message": message}
