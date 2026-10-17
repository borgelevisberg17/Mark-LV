"""Local, opt-in WhatsApp Web conversation monitor state and polling."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path

_lock = threading.RLock()
_STATE_PATH = Path.home() / ".mark-lv" / "whatsapp_monitor.json"


def get_monitor() -> dict:
    with _lock:
        try:
            value = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) and value.get("enabled") else {}
        except (OSError, ValueError, TypeError):
            return {}


def set_monitor(contact: str, preview: str, unread: bool = False, unread_count: int = 0) -> None:
    record = {
        "enabled": True,
        "contact": contact,
        "last_preview_hash": _preview_hash(preview),
        "last_unread": bool(unread),
        "last_unread_count": max(1 if unread else 0, int(unread_count)),
    }
    with _lock:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = _STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(_STATE_PATH)


def stop_monitor() -> bool:
    with _lock:
        try:
            _STATE_PATH.unlink()
            return True
        except FileNotFoundError:
            return False


def _preview_hash(preview: str) -> str:
    return hashlib.sha256(preview.encode("utf-8", "replace")).hexdigest()


def check_for_message(monitor: dict) -> str | None:
    """Poll the target chat row without opening it; return only a new unread preview."""
    from actions.browser_control import _try_existing_edge

    contact = str(monitor.get("contact", "")).strip()
    if not contact:
        return None
    raw = _try_existing_edge("whatsapp", "edge", {"operation": "peek_chat", "contact": contact})
    if not raw:
        return None
    try:
        current = json.loads(raw)
        preview = str(current.get("preview", ""))
        unread = current.get("unread") is True
        unread_count = max(0, int(current.get("unreadCount", 0)))
    except (TypeError, ValueError, AttributeError):
        return None
    current_hash = _preview_hash(preview)
    old_hash = str(monitor.get("last_preview_hash", ""))
    old_unread_count = int(monitor.get("last_unread_count", 1 if monitor.get("last_unread") else 0))
    notify = unread and (
        unread_count > old_unread_count
        or current_hash != old_hash
        or (old_unread_count == 0 and monitor.get("last_unread") is not True)
    )
    updated = {
        "enabled": True,
        "contact": contact,
        "last_preview_hash": current_hash,
        "last_unread": unread,
        "last_unread_count": unread_count,
    }
    with _lock:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = _STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(updated, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(_STATE_PATH)
    return preview if notify else None


def notify_message(contact: str, preview: str, player=None) -> None:
    message = " ".join(preview.split())[:400]
    title = f"WhatsApp · {' '.join(contact.split())}"[:120]
    if player is not None:
        try:
            player.write_log(f"🔔 {title}: {message}")
            player.show_content(title, message or "Nova mensagem não lida.")
        except Exception:
            pass

    system = platform.system()
    try:
        if system == "Windows":
            from win10toast import ToastNotifier
            ToastNotifier().show_toast(title, message or "Nova mensagem não lida.", duration=10, threaded=True)
        elif system == "Darwin":
            escape = lambda value: value.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e", f'display notification "{escape(message)}" with title "{escape(title)}"'],
                check=False, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            notify_send = shutil.which("notify-send")
            if notify_send:
                subprocess.run(
                    [notify_send, "--app-name=Mark-LV", title, message or "Nova mensagem não lida."],
                    check=False, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
    except Exception:
        pass
