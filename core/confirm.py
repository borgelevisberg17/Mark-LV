"""Human-in-the-loop confirmation with idempotent operation tracking."""
from __future__ import annotations

import threading
import time
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Optional

from core.operation_registry import registry

TIMEOUT_SECONDS = 90.0


@dataclass
class _Pending:
    operation_id: str
    key: str
    title: str
    detail: str
    run: Callable[[], str]
    at: float


_pending: Optional[_Pending] = None
_lock = threading.RLock()
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb: Optional[Callable[[str], None]] = None
_invocation_id: ContextVar[str] = ContextVar("marklv_tool_invocation_id", default="")


@contextmanager
def invocation_scope(invocation_id: str):
    """Bind confirmation deduplication to one model tool-call ID."""
    value = str(invocation_id or "")[:200]
    token = _invocation_id.set(value)
    try:
        yield
    finally:
        _invocation_id.reset(token)


def bind(show, hide, log=None) -> None:
    """Wire confirmation controls and audit logging to the desktop UI."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(message: str) -> None:
    if _log_cb:
        try:
            _log_cb(message)
        except Exception:
            pass


def request(key: str, title: str, detail: str, run: Callable[[], str],
            category: str = "general", permissions: tuple[str, ...] | list[str] = ()) -> str:
    """Queue a human confirmation. The interface, not the model, approves it."""
    global _pending
    if _show_cb is None:
        return f"I cannot confirm '{title}' because the interface is not available; nothing was done."
    title = str(title)[:160]
    detail = str(detail)[:12_000]

    invocation_id = _invocation_id.get()
    if invocation_id:
        key = f"{key}:invocation:{invocation_id}"

    now = time.monotonic()
    with _lock:
        if _pending is not None:
            if now - _pending.at <= TIMEOUT_SECONDS:
                if _pending.key == key:
                    rec = registry.get(_pending.operation_id) or {}
                    return (f"[OPERATION_STATUS] status=awaiting_confirmation "
                            f"operation_id={_pending.operation_id}. The same operation is already awaiting approval; do not submit it again.")
                return ("[OPERATION_STATUS] status=awaiting_confirmation. "
                        "There is already a confirmation waiting on screen. Ask the user to resolve that request first.")
            expired = _pending
            _pending = None
            registry.transition(expired.operation_id, "expired", "Confirmation expired before a decision.")
        record, created = registry.create(key, title, category, permissions)
        if not created:
            status = record.get("status", "unknown_after_restart")
            message = record.get("result") or "This operation has already been submitted."
            return (f"[OPERATION_STATUS] status={status} operation_id={record['operation_id']}. "
                    f"{message} Do not repeat it automatically; verify its effects or use a new explicit operation.")
        operation_id = record["operation_id"]
        pending = _Pending(operation_id, key, title, detail, run, now)
        _pending = pending

    try:
        _show_cb(title, detail)
    except Exception as exc:
        with _lock:
            if _pending and _pending.operation_id == operation_id:
                _pending = None
        registry.transition(operation_id, "failed", f"Could not display confirmation: {exc}")
        return f"Could not ask for confirmation: {exc}. Nothing was executed."

    _log(f"SYS: awaiting_confirmation id={operation_id} title={title}")
    return (f"[CONFIRMATION_PENDING] [OPERATION_STATUS] status=awaiting_confirmation operation_id={operation_id}. "
            f"Confirmation is visible on screen for '{title}'. The user can approve it there or say 'Confirmo' / 'Pode executar'; "
            "'Cancela' rejects it. Ask them to choose; do not claim completion.")


def resolve(accepted: bool) -> None:
    """Resolve the visible request once; execute accepted work off the UI thread."""
    global _pending
    with _lock:
        pending = _pending
        if pending is None:
            return
        _pending = None

    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass

    if time.monotonic() - pending.at > TIMEOUT_SECONDS:
        registry.transition(pending.operation_id, "expired", "Confirmation expired before approval.")
        _log(f"SYS: expired id={pending.operation_id} title={pending.title}")
        return
    if not accepted:
        registry.transition(pending.operation_id, "cancelled", "User cancelled before execution.")
        _log(f"SYS: cancelled id={pending.operation_id} title={pending.title}")
        return

    current = registry.get(pending.operation_id)
    if not current or current.get("status") != "awaiting_confirmation":
        _log(f"SYS: duplicate confirmation ignored id={pending.operation_id}")
        return
    registry.transition(pending.operation_id, "running", "Execution started after explicit approval.")

    def worker() -> None:
        started = time.monotonic()
        try:
            value = pending.run()
            result = str(value or "Operation completed.")
            record = registry.transition(pending.operation_id, "completed", result)
            duration = (record or {}).get("duration_ms")
            _log(f"SYS: completed id={pending.operation_id} duration_ms={duration} title={pending.title}: {result[:500]}")
        except Exception as exc:
            # Failure does not imply no side effect; idempotency prevents automatic replay.
            registry.transition(pending.operation_id, "failed", f"{type(exc).__name__}: {exc}")
            _log(f"ERR: failed id={pending.operation_id} duration_ms={round((time.monotonic()-started)*1000)} title={pending.title}: {exc}")

    threading.Thread(target=worker, daemon=True, name=f"confirm-{pending.operation_id[:12]}").start()


def pending_title() -> str:
    """Return the visible pending confirmation title, or an empty string."""
    with _lock:
        if _pending is None:
            return ""
        if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            return ""
        return _pending.title


_VOICE_APPROVALS = {
    "confirmo", "confirmar", "pode confirmar", "pode executar", "autorizo",
    "eu autorizo", "aprovo", "eu aprovo", "sim autorizo", "sim pode executar",
    "i confirm", "i approve", "approve", "confirm", "proceed", "yes approve",
}
_VOICE_REJECTIONS = {
    "cancela", "cancelar", "cancelo", "nao", "nao confirmo", "nao autorizo",
    "recuso", "rejeito", "nega", "negar", "i cancel", "cancel", "no",
    "i reject", "reject",
}


def resolve_voice(text: str) -> bool:
    """Resolve a pending action only for a clear, standalone spoken command."""
    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = " ".join("".join(ch if ch.isalnum() else " " for ch in normalized).split())
    words = normalized.split()
    if words[:1] in (["jarvis"], ["mark"], ["marklv"]):
        normalized = " ".join(words[1:])
    normalized = normalized.removesuffix(" por favor").removesuffix(" please").strip()
    if normalized.startswith("sim ") and normalized[4:] in _VOICE_APPROVALS:
        normalized = normalized[4:]
    if normalized.startswith("yes ") and normalized[4:] in _VOICE_APPROVALS:
        normalized = normalized[4:]
    if normalized not in _VOICE_APPROVALS and normalized not in _VOICE_REJECTIONS:
        return False
    with _lock:
        pending = _pending
        if pending is None or time.monotonic() - pending.at > TIMEOUT_SECONDS:
            return False
    resolve(normalized in _VOICE_APPROVALS)
    _log(f"SYS: voice confirmation {'approved' if normalized in _VOICE_APPROVALS else 'cancelled'}: {pending.title}")
    return True


def pending_operation_id() -> str:
    with _lock:
        return _pending.operation_id if _pending else ""


def operation_status(operation_id: str) -> dict | None:
    return registry.get(operation_id)


def recent_operations(limit: int = 20) -> list[dict]:
    return registry.list_recent(limit)
