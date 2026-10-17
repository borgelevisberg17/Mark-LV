"""Local WhatsApp Web access through the user's already authenticated Edge tab."""
from __future__ import annotations

import hashlib
import json


def _call(parameters: dict) -> dict:
    from actions.browser_control import _try_existing_edge

    result = _try_existing_edge("whatsapp", "edge", parameters)
    if result is None:
        raise RuntimeError(
            "WhatsApp access is not connected. Open WhatsApp Web in Edge, enable "
            "the Mark-LV Browser Bridge, and confirm its local token is configured."
        )
    try:
        value = json.loads(result)
    except (TypeError, json.JSONDecodeError) as exc:
        # The bridge returns plain text for execution errors. Preserve that
        # diagnostic instead of replacing it with a generic JSON parse error.
        detail = str(result or "").strip()
        if detail:
            raise RuntimeError(detail[:500]) from exc
        raise RuntimeError("WhatsApp Web returned an empty response.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("WhatsApp Web returned an unexpected result.")
    return value


def whatsapp(parameters: dict | None = None, player=None) -> str:
    p = parameters or {}
    operation = str(p.get("operation", "status")).strip().lower()
    allowed = {
        "open", "status", "list_chats", "read_messages", "send_message",
        "monitor_start", "monitor_stop", "monitor_status",
    }
    if operation not in allowed:
        return "Unsupported WhatsApp operation."
    if operation == "open":
        from actions.browser_control import _try_existing_edge
        result = _try_existing_edge("new_tab", "edge", {"url": "https://web.whatsapp.com/"})
        return result or "The Mark-LV Edge Bridge is not connected. Enable the extension and configure its local token."
    if operation == "monitor_stop":
        from core.whatsapp_monitor import stop_monitor
        return "WhatsApp message monitoring stopped." if stop_monitor() else "WhatsApp message monitoring was not active."
    if operation == "monitor_status":
        from core.whatsapp_monitor import get_monitor
        monitor = get_monitor()
        return (f"Monitoring unread messages from {monitor['contact']}."
                if monitor else "WhatsApp message monitoring is off.")
    if operation == "monitor_start":
        contact = str(p.get("contact", "")).strip()
        if not contact or len(contact) > 200:
            return "To start monitoring, provide the exact WhatsApp contact or group name."
        try:
            baseline = _call({"operation": "peek_chat", "contact": contact})
        except RuntimeError as exc:
            return str(exc)
        from core.whatsapp_monitor import set_monitor
        set_monitor(
            contact,
            str(baseline.get("preview", "")),
            baseline.get("unread") is True,
            baseline.get("unreadCount", 0),
        )
        return (
            f"Monitoring {contact}. Mark-LV will check for new unread messages about every 20 seconds "
            "while it is running and show a desktop notification."
        )
    if operation == "send_message":
        contact = str(p.get("contact", "")).strip()
        message = str(p.get("message", ""))
        if not contact or len(contact) > 200 or not message.strip() or len(message) > 4000:
            return "Sending requires a contact or group name and a message of 1 to 4000 characters."

        def send() -> str:
            result = _call({"operation": operation, "contact": contact, "message": message})
            if result.get("sent") is not True or result.get("verified") is not True:
                return "WhatsApp did not verify the outgoing message; delivery is unconfirmed."
            return f"Message sent to {contact} via WhatsApp Web and visible in the outgoing conversation."

        from core.execution import request_execution
        fingerprint = hashlib.sha256(
            json.dumps([contact, message], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return request_execution(
            key="social:whatsapp:send_message",
            title="Send WhatsApp message",
            detail=f"Contact or group: {contact}\nMessage:\n{message}\n\nThis sends a message from your WhatsApp account.",
            operation=send,
            player=player,
            category="desktop",
            permissions=("social", "send_message"),
            fingerprint=fingerprint,
        )

    params = {"operation": operation}
    if operation in {"list_chats", "read_messages"}:
        try:
            limit = max(1, min(100, int(p.get("limit", 30))))
        except (TypeError, ValueError):
            return "limit must be a whole number from 1 to 100."
        params["limit"] = limit
    if operation == "read_messages":
        contact = str(p.get("contact", "")).strip()
        if not contact or len(contact) > 200:
            return "Reading messages requires a contact or group name."
        params["contact"] = contact
    try:
        result = _call(params)
    except RuntimeError as exc:
        return str(exc)
    if operation == "status" and result.get("connected") is False:
        result["message"] = (
            "WhatsApp Web is open, but its chat list is not available. "
            "Complete sign-in or wait for WhatsApp Web to finish loading, then retry."
        )
    return json.dumps(result, ensure_ascii=False, indent=2)


TOOL = {
    "name": "whatsapp",
    "description": (
        "Open or access WhatsApp Web through the user's Edge session. "
        "Check status, list visible chats, read recent messages from a named chat, "
        "or send a text message after explicit on-screen confirmation. This uses "
        "the local Mark-LV Edge Bridge; it cannot bypass WhatsApp login or access "
        "hidden/archived content that is not surfaced by the Web interface. "
        "Start monitoring a named chat with monitor_start; use monitor_status or monitor_stop to manage it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "enum": ["open", "status", "list_chats", "read_messages", "send_message", "monitor_start", "monitor_status", "monitor_stop"]},
            "contact": {"type": "STRING", "description": "Visible chat name, required for reading or sending."},
            "message": {"type": "STRING", "description": "Text to send; requires on-screen confirmation."},
            "limit": {"type": "INTEGER", "description": "Maximum visible chats/messages to return, 1–100."},
        },
        "required": ["operation"],
    },
    "handler": whatsapp,
}
