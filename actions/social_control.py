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
        try:
            _must_succeed(last, f"Click '{label}'")
            return last
        except RuntimeError:
            continue
    raise RuntimeError(f"Could not locate a supported control. Last result: {last[:180]}")


def _type_first(labels: tuple[str, ...], text: str) -> str:
    if not labels:
        raise RuntimeError("This platform has no configured message field.")
    last = ""
    for label in labels:
        last = _browser("smart_type", description=label, text=text)
        try:
            _must_succeed(last, f"Type into '{label}'")
            return last
        except RuntimeError:
            continue
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


def _desktop_control(action: str, **kwargs) -> str:
    """Use the existing local desktop action; never invokes a social API."""
    from actions.computer_control import computer_control
    return str(computer_control(parameters={"action": action, **kwargs}) or "")


def _desktop_snapshot(platform: dict) -> dict:
    """Read only the visible desktop screenshot using the existing Gemini client."""
    try:
        import io
        import json
        import pyautogui
        from google.genai import types as gtypes
    except ImportError as exc:
        raise RuntimeError(f"Desktop visual inspection dependency unavailable: {exc}") from exc
    image = pyautogui.screenshot()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    from core import gemini
    prompt = (
        "Inspect this screenshot of a desktop application. Extract only text and controls "
        "that are visibly readable; do not infer hidden state. Return ONLY valid JSON with "
        "keys active_conversation_title (string or empty), visible_text (string), "
        "message_composer_text (string or empty), and visible_controls (array of strings). "
        "For active_conversation_title, use only the clear title/header of the currently "
        "open conversation, not search results, sidebar entries, or old messages. If uncertain, "
        "return an empty string. Do not include credentials or speculate. "
        f"Target application: {platform['app']}."
    )
    response = gemini.call(
        [gtypes.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png"), prompt],
        tier=gemini.FAST, timeout_ms=20_000,
    )
    if response is None:
        raise RuntimeError("Visual inspection did not return a result; no action was taken.")
    raw = (response.text or "").strip()
    try:
        result = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Visual inspection returned unreadable structured data; no action was taken.") from exc
    if not isinstance(result, dict) or not isinstance(result.get("visible_text"), str):
        raise RuntimeError("Visual inspection did not provide visible text; no action was taken.")
    result["active_conversation_title"] = str(result.get("active_conversation_title") or "").strip()
    result["message_composer_text"] = str(result.get("message_composer_text") or "").strip()
    result["visible_controls"] = result.get("visible_controls") if isinstance(result.get("visible_controls"), list) else []
    result["visible_text"] = result["visible_text"][:_MAX_PAGE_TEXT]
    return result


def _desktop_find(description: str) -> tuple[int, int]:
    """Locate one visible UI element and reject coordinates outside the current screen."""
    result = _desktop_control("screen_find", description=description).strip()
    match = re.fullmatch(r"(\d+)\s*,\s*(\d+)", result)
    if not match:
        raise RuntimeError(f"Could not locate '{description}' on the desktop; no action was taken.")
    x, y = int(match.group(1)), int(match.group(2))
    try:
        import pyautogui
        width, height = pyautogui.size()
    except ImportError as exc:
        raise RuntimeError("PyAutoGUI is required for desktop interaction.") from exc
    if not (0 <= x < width and 0 <= y < height):
        raise RuntimeError("The visual locator returned out-of-screen coordinates; no action was taken.")
    return x, y


def _desktop_click(description: str) -> str:
    x, y = _desktop_find(description)
    result = _desktop_control("click", x=x, y=y)
    _must_succeed(result, f"Click desktop element '{description}'")
    return result


def _desktop_type(text: str) -> str:
    result = _desktop_control("smart_type", text=text, clear_first=True)
    _must_succeed(result, "Type into desktop field")
    return result


def _desktop_prepare_message(platform: dict, recipient: str, message: str) -> str:
    if not recipient or len(recipient.strip()) > 160:
        raise ValueError("A valid contact or conversation name is required.")
    if not message or not message.strip() or len(message) > _MAX_MESSAGE:
        raise ValueError(f"Message must contain 1–{_MAX_MESSAGE} characters.")
    focus = _desktop_control("focus_window", title=platform["app"])
    if any(token in focus.lower() for token in ("failed", "requires", "unknown os")):
        raise RuntimeError(f"Could not focus the {platform['app']} window: {focus}")
    initial = _desktop_snapshot(platform)
    if not initial["visible_text"].strip():
        raise RuntimeError("The app window has no readable visible text; no action was taken.")
    _desktop_click("search field")
    _desktop_type(recipient)
    search_state = _desktop_snapshot(platform)
    if recipient.casefold() not in search_state["visible_text"].casefold():
        raise RuntimeError("The requested contact is not visible in desktop search results; nothing was composed.")
    _desktop_click(f"search result named {recipient}")
    selected = _desktop_snapshot(platform)
    header = selected["active_conversation_title"]
    if not header or recipient.casefold() not in header.casefold():
        raise RuntimeError(
            "The active conversation header could not be verified as the requested recipient. "
            "No message was composed or sent."
        )
    _desktop_click("message composer")
    _desktop_type(message)
    drafted = _desktop_snapshot(platform)
    if message not in drafted["message_composer_text"]:
        raise RuntimeError("The exact draft could not be verified in the message composer; it was not sent.")
    return (
        f"Draft prepared in the {platform['app']} desktop UI for '{recipient}'. "
        "The active conversation header and exact draft were visually checked. Review before sending."
    )


def _desktop_send_message(platform: dict, recipient: str, message: str) -> str:
    prepared = _desktop_prepare_message(platform, recipient, message)
    if "draft prepared" not in prepared.lower():
        raise RuntimeError("Desktop message preparation was not verified.")
    _desktop_click("Send message button")
    after = _desktop_snapshot(platform)
    if message not in after["visible_text"]:
        raise RuntimeError("The exact message was not visible after clicking Send; delivery is unconfirmed.")
    return (
        f"The desktop UI shows the exact message after Send in {platform['app']}. "
        "This does not prove server receipt, delivery, or read status."
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
        mode = str(params.get("mode", "browser")).strip().lower()
        desktop_mode = mode in {"app", "desktop"}
        if action == "inspect":
            if desktop_mode:
                state = _desktop_snapshot(platform)
                return (
                    f"Desktop app: {platform['app']}\n"
                    f"Active conversation header: {state['active_conversation_title'] or '(not verified)'}\n"
                    f"Visible text (truncated):\n{state['visible_text']}\n"
                    "This is screenshot-based extraction; uncertain text is not treated as verified."
                )
            url, visible = _checked_page(platform)
            return f"Verified visible page: {url}\nVisible text (truncated):\n{visible}\nOnly visible page text is inspected; hidden messages and conversation meaning are not inferred."
        if action == "prepare_message":
            helper = _desktop_prepare_message if desktop_mode else _prepare_message
            return helper(platform, str(params.get("recipient", "")), str(params.get("message", "")))
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
                    if desktop_mode:
                        return _desktop_send_message(platform, recipient, message)
                    prepared = _prepare_message(platform, recipient, message)
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
                    if desktop_mode:
                        before = _desktop_snapshot(platform)
                        if not before["active_conversation_title"] or (
                            recipient and recipient.casefold() not in before["active_conversation_title"].casefold()
                        ):
                            raise RuntimeError("The active desktop conversation header does not match the requested contact; no call control was clicked.")
                        result = _desktop_click({
                            "call": "Start voice call button",
                            "video_call": "Start video call button",
                            "end_call": "End call button",
                        }[control_action])
                        after = _desktop_snapshot(platform)
                        return f"Desktop UI control clicked and screen re-read. Result: {result}. Visible text: {after['visible_text'][:1200]}. Call state still requires visual confirmation."
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
            "mode": {"type": "STRING", "description": "browser (default) for authenticated Edge UI; app or desktop for installed desktop UI"},
            "recipient": {"type": "STRING", "description": "Exact visible contact or conversation name"},
            "message": {"type": "STRING", "description": "Message text; only used for prepare_message/send_message"},
        },
        "required": ["platform", "action"],
    },
    "handler": social_control,
}
