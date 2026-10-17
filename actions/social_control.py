"""API-free social app controller driven by the user's local app/browser UI.

This action deliberately does not use platform APIs, cookies, credentials, or
private application databases. It delegates to Mark-LV's existing browser
control and app launcher. A UI action is never described as successful merely
because a keystroke was issued.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

# Exact HTTPS origins only. Add a platform here only after testing its UI flow.
_PLATFORMS = {
    "whatsapp": {
        "hosts": {"web.whatsapp.com"},
        "url": "https://web.whatsapp.com/",
        "app": "WhatsApp",
        "search": ("Search", "Search or start a new chat"),
        "composer": ("Type a message", "Message"),
        "send": ("Send",),
        "call": ("Call", "Voice call"),
        "video_call": ("Video call",),
        "end_call": ("End call", "Hang up"),
    },
    "telegram": {
        "hosts": {"web.telegram.org"},
        "url": "https://web.telegram.org/",
        "app": "Telegram",
        "search": ("Search",),
        "composer": ("Message", "Write a message..."),
        "send": ("Send",),
        "call": ("Call",),
        "video_call": ("Video call",),
        "end_call": ("End call",),
    },
    "discord": {
        "hosts": {"discord.com", "www.discord.com"},
        "url": "https://discord.com/app",
        "app": "Discord",
        "search": ("Find or start a conversation", "Search"),
        "composer": ("Message", "Message #"),
        "send": ("Send",),
        "call": ("Start Voice Call", "Call"),
        "video_call": ("Start Video Call", "Video"),
        "end_call": ("Disconnect", "End call"),
    },
    "instagram": {
        "hosts": {"instagram.com", "www.instagram.com"},
        "url": "https://www.instagram.com/direct/inbox/",
        "app": "Instagram",
        "search": ("Search",),
        "composer": ("Message...", "Message"),
        "send": ("Send",),
        "call": (),
        "video_call": (),
        "end_call": (),
    },
    "messenger": {
        "hosts": {"messenger.com", "www.messenger.com"},
        "url": "https://www.messenger.com/",
        "app": "Messenger",
        "search": ("Search Messenger", "Search"),
        "composer": ("Aa", "Message"),
        "send": ("Send",),
        "call": ("Audio call", "Call"),
        "video_call": ("Video call",),
        "end_call": ("End call",),
    },
    "slack": {
        "hosts": {"app.slack.com"},
        "url": "https://app.slack.com/client/",
        "app": "Slack",
        "search": ("Search",),
        "composer": ("Message", "Message to"),
        "send": ("Send",),
        "call": ("Start a huddle",),
        "video_call": ("Turn on video",),
        "end_call": ("Leave huddle",),
    },
    "teams": {
        "hosts": {"teams.microsoft.com"},
        "url": "https://teams.microsoft.com/",
        "app": "Teams",
        "search": ("Search",),
        "composer": ("Type a message", "Message"),
        "send": ("Send",),
        "call": ("Call",),
        "video_call": ("Video call",),
        "end_call": ("Leave", "Hang up"),
    },
}

# Actions which can trigger external effects always go through the app's
# existing visible human-confirmation mechanism.
_SENSITIVE = {"send_message", "start_call", "start_video_call", "end_call"}
_MAX_MESSAGE = 4000
_MAX_PAGE_TEXT = 6000


def _platform(name: str) -> tuple[str, dict]:
    key = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    aliases = {
        "whatsappweb": "whatsapp", "wa": "whatsapp",
        "telegramweb": "telegram", "ig": "instagram",
        "facebookmessenger": "messenger", "msteams": "teams",
    }
    key = aliases.get(key, key)
    if key not in _PLATFORMS:
        raise ValueError(
            f"Unsupported social platform '{name}'. Supported: "
            + ", ".join(sorted(_PLATFORMS))
        )
    return key, _PLATFORMS[key]


def _browser(action: str, **kwargs) -> str:
    from actions.browser_control import browser_control
    params = {"action": action, "browser": "edge", **kwargs}
    return str(browser_control(parameters=params) or "")


def _checked_page(platform: dict) -> tuple[str, str]:
    """Return (URL, visible text) only when the active page is the expected HTTPS host."""
    url_result = _browser("get_url")
    # Existing browser bridge may return explanatory error text rather than a URL.
    url = url_result.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in platform["hosts"]:
        raise RuntimeError(
            "The active Edge page is not a supported HTTPS page for this platform. "
            "Open the platform in Edge and sign in first; no page content was read."
        )
    text_result = _browser("get_text")
    lowered = text_result.lower()
    if lowered.startswith(("browser error", "bridge error", "could not get page text",
                           "the edge extension did not respond", "unknown browser action")):
        raise RuntimeError(f"Could not verify the visible page: {text_result[:240]}")
    visible = text_result.strip()
    if not visible:
        raise RuntimeError("The page returned no visible text; no action was taken.")
    return url, visible[:_MAX_PAGE_TEXT]


def _must_succeed(result: str, operation: str) -> None:
    bad = ("could not find", "could not start", "browser error", "bridge error",
           "unknown browser action", "did not respond", "timed out", "failed")
    if not result.strip() or any(token in result.lower() for token in bad):
        raise RuntimeError(f"{operation} was not confirmed by the UI: {result[:240]}")


def _click_first(labels: tuple[str, ...]) -> str:
    if not labels:
        raise RuntimeError("This platform has no configured UI control for that action.")
    last = ""
    for label in labels:
        last = _browser("smart_click", description=label)
        if "could not find" not in last.lower() and "error" not in last.lower():
            return last
    raise RuntimeError(f"Could not locate a supported control. Last result: {last[:180]}")


def _type_first(labels: tuple[str, ...], text: str) -> str:
    if not labels:
        raise RuntimeError("This platform has no configured message field.")
    last = ""
    for label in labels:
        last = _browser("smart_type", description=label, text=text)
        if "could not find" not in last.lower() and "error" not in last.lower():
            return last
    raise RuntimeError(f"Could not locate a supported input. Last result: {last[:180]}")


def _prepare_message(platform: dict, recipient: str, message: str) -> str:
    if not recipient or len(recipient.strip()) > 160:
        raise ValueError("A valid contact or conversation name is required.")
    if not message or not message.strip() or len(message) > _MAX_MESSAGE:
        raise ValueError(f"Message must contain 1–{_MAX_MESSAGE} characters.")
    url, _ = _checked_page(platform)
    # Resolve through the visible search UI rather than trusting a sidebar mention
    # or an old chat message as proof that the intended conversation is active.
    _click_first(platform["search"])
    _type_first(platform["search"], recipient)
    _click_first((recipient,))
    _, visible = _checked_page(platform)
    if recipient.casefold() not in visible.casefold():
        raise RuntimeError(
            f"The conversation for '{recipient}' could not be verified after selection. "
            "No message was composed or sent."
        )
    _type_first(platform["composer"], message)
    return (
        f"Draft prepared for '{recipient}' on {url}. The contact was selected through "
        "the visible search UI; review the conversation and draft before sending."
    )


def social_control(parameters=None, player=None, response=None, session_memory=None) -> str:
    params = parameters or {}
    try:
        action = str(params.get("action", "inspect")).strip().lower()
        key, platform = _platform(params.get("platform", ""))
        if action == "capabilities":
            return (
                f"{key}: local UI only; no social-network API. Supported here: open, "
                "inspect, prepare_message, send_message, start_call, start_video_call, "
                "end_call. UI labels vary by platform/version; each operation is checked "
                "against the active HTTPS host."
            )
        if action == "open":
            mode = str(params.get("mode", "browser")).lower()
            if mode == "app":
                from actions.open_app import open_app
                result = open_app(parameters={"app_name": platform["app"]}, player=player)
                if "could not" in result.lower() or "failed" in result.lower() or "not in the approved" in result.lower():
                    return result
                # Opening an app is not proof that it is signed in or controllable.
                return result + " This confirms launch only; it does not confirm sign-in or UI automation."
            result = _browser("go_to", url=platform["url"])
            if "opened" in result.lower() or "navigat" in result.lower() or "url" in result.lower():
                return result + " Sign in manually if required; no credentials were accessed."
            return f"Could not confirm platform opened: {result}"
        if action == "inspect":
            url, visible = _checked_page(platform)
            return f"Verified visible page: {url}\nVisible text (truncated):\n{visible}\nOnly visible page text is inspected; hidden messages and conversation meaning are not inferred."
        if action == "prepare_message":
            return _prepare_message(platform, str(params.get("recipient", "")), str(params.get("message", "")))
        if action in {"send_message", "start_call", "start_video_call", "end_call"}:
            from core.execution import request_execution
            recipient = str(params.get("recipient", "")).strip()
            message = str(params.get("message", ""))
            if action == "send_message":
                if not recipient or not message.strip():
                    return "A recipient and non-empty message are required. Nothing was sent."
                title = f"Send {key} message to {recipient}"
                detail = (
                    f"Platform: {key}\nRecipient: {recipient}\nMessage:\n{message}\n"
                    "After approval, the controller will verify the active page and "
                    "prepare the message. Sending is attempted only if the target is "
                    "visible; the UI result is then checked. Do not approve unless correct."
                )
                def operation():
                    prepared = _prepare_message(platform, recipient, message)
                    # Do not send if preparation reports a mismatch or any uncertainty.
                    if "draft prepared" not in prepared.lower():
                        raise RuntimeError("Message preparation was not verified.")
                    _click_first(platform["send"])
                    _, visible_after = _checked_page(platform)
                    if message not in visible_after:
                        raise RuntimeError("The exact message was not visible after clicking Send; delivery is unconfirmed.")
                    return f"UI shows the exact message after Send on {key}. This confirms visible UI state, not recipient delivery/read status."
            else:
                control_action = {
                    "start_call": "call",
                    "start_video_call": "video_call",
                    "end_call": "end_call",
                }[action]
                title = f"{action.replace('_', ' ')} on {key}"
                detail = (
                    f"Platform: {key}\nRequested UI action: {control_action}\n"
                    f"Visible page must be verified before clicking. Approve only if the correct conversation is open."
                )
                def operation():
                    _, before = _checked_page(platform)
                    if recipient and recipient.casefold() not in before.casefold():
                        raise RuntimeError("The requested contact is not visible; no call control was clicked.")
                    result = _click_first(platform[control_action])
                    _, after = _checked_page(platform)
                    return f"Clicked UI control and re-read page. Control result: {result}. UI text after action: {after[:1200]}. Call state still requires visual confirmation."
            fingerprint = hashlib.sha256(
                f"{key}|{action}|{recipient}|{message}".encode("utf-8")
            ).hexdigest()
            return request_execution(
                key=f"social-ui:{key}:{action}",
                title=title,
                detail=detail,
                operation=operation,
                player=player,
                category="desktop",
                permissions=("local_ui", "external_communication") if action in _SENSITIVE else ("local_ui",),
                fingerprint=fingerprint,
            )
        return "Unsupported action. Use capabilities to list supported actions."
    except Exception as exc:
        return f"Social UI controller error: {exc}. No success is claimed."


TOOL = {
    "name": "social_control",
    "description": (
        "Controls supported social platforms through the user's local installed app "
        "or existing authenticated Edge UI, without social-network APIs. Use actions "
        "open, inspect, capabilities, prepare_message, send_message, start_call, "
        "start_video_call, end_call. Side effects require visible human approval. "
        "Never claim delivery/read status unless the UI verifies it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "platform": {"type": "STRING", "description": "whatsapp, telegram, discord, instagram, messenger, slack, teams"},
            "action": {"type": "STRING", "description": "open | inspect | capabilities | prepare_message | send_message | start_call | start_video_call | end_call"},
            "mode": {"type": "STRING", "description": "For open: browser (default) or app"},
            "recipient": {"type": "STRING", "description": "Exact visible contact or conversation name"},
            "message": {"type": "STRING", "description": "Message text; only used for prepare_message/send_message"},
        },
        "required": ["platform", "action"],
    },
    "handler": social_control,
}
