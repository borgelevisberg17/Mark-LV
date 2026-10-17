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


def _desktop_window(platform: dict):
    """Find the visible installed-app window through local Windows UI Automation."""
    import platform as system_platform
    if system_platform.system() != "Windows":
        raise RuntimeError(
            "Native social-app control currently requires Windows UI Automation. "
            "Use mode='browser' on this operating system."
        )
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise RuntimeError("pywinauto is not installed; install the project's Windows dependencies.") from exc

    expected = platform["app"].casefold()
    matches = []
    for window in Desktop(backend="uia").windows(visible_only=True):
        try:
            title = (window.window_text() or "").strip()
            if title and expected in title.casefold() and window.is_visible() and window.is_enabled():
                matches.append(window)
        except Exception:
            continue
    if len(matches) != 1:
        raise RuntimeError(
            f"Could not uniquely identify a visible {platform['app']} window "
            f"(matches: {len(matches)}). Focus the intended app and retry."
        )
    return matches[0]


def _uia_text(control) -> str:
    try:
        value = (control.window_text() or "").strip()
        if value:
            return value
    except Exception:
        pass
    try:
        value = str(control.iface_value.CurrentValue or "").strip()
        if value:
            return value
    except Exception:
        pass
    return ""


def _uia_controls(window) -> list[dict]:
    controls = []
    try:
        descendants = window.descendants()
    except Exception as exc:
        raise RuntimeError(f"Could not inspect the app accessibility tree: {exc}") from exc
    for control in descendants:
        try:
            if not control.is_visible() or not control.is_enabled():
                continue
            rect = control.rectangle()
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            name = _uia_text(control)
            control_type = str(control.element_info.control_type or "")
            if name or control_type == "Edit":
                controls.append({
                    "control": control,
                    "name": name,
                    "type": control_type,
                    "left": rect.left,
                    "top": rect.top,
                    "right": rect.right,
                    "bottom": rect.bottom,
                })
        except Exception:
            continue
    return controls


def _desktop_snapshot(platform: dict, recipient: str = "") -> dict:
    """Inspect the local Windows accessibility tree; no screenshot leaves the PC."""
    window = _desktop_window(platform)
    controls = _uia_controls(window)
    if not controls:
        raise RuntimeError("The app exposes no readable visible UI controls; no action was taken.")
    visible_text = "\n".join(dict.fromkeys(
        item["name"] for item in controls if item["name"]
    ))[:_MAX_PAGE_TEXT]
    composer = ""
    for item in sorted(controls, key=lambda row: row["bottom"], reverse=True):
        if item["type"] == "Edit" and item["bottom"] >= window.rectangle().bottom - 180:
            composer = _uia_text(item["control"])
            if composer:
                break

    active_title = ""
    if recipient:
        bounds = window.rectangle()
        candidates = [
            item for item in controls
            if item["name"].casefold() == recipient.casefold()
            and item["type"] in {"Text", "Button", "Hyperlink", "TabItem"}
            and item["left"] >= bounds.left + int(bounds.width() * 0.25)
            and item["top"] <= bounds.top + int(bounds.height() * 0.45)
        ]
        if len(candidates) == 1:
            active_title = candidates[0]["name"]
    return {
        "active_conversation_title": active_title,
        "visible_text": visible_text,
        "message_composer_text": composer,
        "visible_controls": [item["name"] for item in controls if item["name"]][:100],
    }


def _desktop_click(description: str, platform: dict, recipient: str = "") -> str:
    """Click a named/role-matched visible UIA element in the selected app window."""
    window = _desktop_window(platform)
    controls = _uia_controls(window)
    query = description.casefold()
    candidates = []
    bounds = window.rectangle()

    for item in controls:
        name, kind = item["name"].casefold(), item["type"]
        score = None
        if query == "search field":
            if kind == "Edit" and ("search" in name or not name):
                score = 0 if "search" in name else 2
            elif kind == "Button" and "search" in name:
                score = 3
        elif query.startswith("search result named "):
            target = recipient or description[len("search result named "):]
            if name == target.casefold() and kind in {"Text", "Button", "ListItem", "Hyperlink"}:
                score = 0
            elif target.casefold() in name and kind in {"Text", "Button", "ListItem", "Hyperlink"}:
                score = 1
        elif query == "message composer":
            if kind == "Edit" and item["bottom"] >= bounds.top + int(bounds.height() * 0.55):
                score = 0
        elif query == "send message button":
            if kind == "Button" and name in {"send", "send message", "send message (enter)"}:
                score = 0
            elif kind == "Button" and "send" in name:
                score = 1
        elif query == "start voice call button":
            if kind == "Button" and ("call" in name or "voice" in name) and "video" not in name and "end" not in name and "hang up" not in name:
                score = 0 if "voice" in name else 1
        elif query == "start video call button":
            if kind == "Button" and "video" in name and "call" in name:
                score = 0
        elif query == "end call button":
            if kind == "Button" and ("end call" in name or "hang up" in name or "leave call" in name):
                score = 0
        if score is not None:
            candidates.append((score, item))

    if not candidates:
        raise RuntimeError(f"Could not find visible UI element '{description}' in {platform['app']}.")
    candidates.sort(key=lambda pair: (pair[0], pair[1]["top"], pair[1]["left"]))
    best_score = candidates[0][0]
    best = [item for score, item in candidates if score == best_score]
    if query.startswith("search result named ") and len(best) != 1:
        raise RuntimeError("The search result is ambiguous; no element was clicked.")
    if len(best) != 1:
        raise RuntimeError(f"UI element '{description}' is ambiguous; no click was performed.")
    target = best[0]["control"]
    try:
        target.click_input()
        return f"Clicked visible {target.element_info.control_type}: {target.window_text() or description}"
    except Exception as exc:
        raise RuntimeError(f"Could not click '{description}' in {platform['app']}: {exc}") from exc


def _desktop_type(platform: dict, text: str) -> str:
    """Type into the currently focused local control, preferring UIA value setting."""
    try:
        from pywinauto.keyboard import send_keys
    except ImportError as exc:
        raise RuntimeError("pywinauto keyboard support is unavailable.") from exc
    window = _desktop_window(platform)
    focused = window.get_focus()
    if focused is None:
        raise RuntimeError("No focused input field is available; no text was entered.")
    try:
        if focused.element_info.control_type == "Edit":
            try:
                focused.set_edit_text(text)
            except Exception:
                # Some Electron controls expose Edit semantics without ValuePattern.
                focused.set_focus()
                escaped = text.replace("{", "{{}").replace("}", "{}}")
                send_keys(escaped, with_spaces=True, pause=0.005)
        else:
            # Clipboard-independent local keyboard input; escape pywinauto metacharacters.
            escaped = text.replace("{", "{{}").replace("}", "{}}")
            send_keys(escaped, with_spaces=True, pause=0.005)
        return "Text entered into the focused local UI control."
    except Exception as exc:
        raise RuntimeError(f"Could not enter text into the focused UI control: {exc}") from exc


def _desktop_prepare_message(platform: dict, recipient: str, message: str) -> str:
    if not recipient or len(recipient.strip()) > 160:
        raise ValueError("A valid contact or conversation name is required.")
    if not message or not message.strip() or len(message) > _MAX_MESSAGE:
        raise ValueError(f"Message must contain 1–{_MAX_MESSAGE} characters.")
    window = _desktop_window(platform)
    try:
        window.set_focus()
    except Exception as exc:
        raise RuntimeError(f"Could not focus the {platform['app']} window: {exc}") from exc
    _desktop_snapshot(platform)
    _desktop_click("search field", platform)
    _desktop_type(platform, recipient)
    search_state = _desktop_snapshot(platform)
    if recipient.casefold() not in search_state["visible_text"].casefold():
        raise RuntimeError("The requested contact is not visible in desktop search results; nothing was composed.")
    _desktop_click(f"search result named {recipient}", platform, recipient)
    selected = _desktop_snapshot(platform, recipient)
    header = selected["active_conversation_title"]
    if not header or header.casefold() != recipient.casefold():
        raise RuntimeError(
            "The active conversation header could not be verified as the requested recipient. "
            "No message was composed or sent."
        )
    _desktop_click("message composer", platform)
    _desktop_type(platform, message)
    drafted = _desktop_snapshot(platform, recipient)
    if message not in drafted["message_composer_text"]:
        raise RuntimeError("The exact draft could not be verified in the message composer; it was not sent.")
    return (
        f"Draft prepared in the {platform['app']} desktop UI for '{recipient}'. "
        "The active conversation header and exact draft were checked using local UI Automation."
    )


def _desktop_send_message(platform: dict, recipient: str, message: str) -> str:
    prepared = _desktop_prepare_message(platform, recipient, message)
    if "draft prepared" not in prepared.lower():
        raise RuntimeError("Desktop message preparation was not verified.")
    _desktop_click("send message button", platform)
    after = _desktop_snapshot(platform, recipient)
    if message not in after["visible_text"] or message in after["message_composer_text"]:
        raise RuntimeError("The exact message was not confirmed in conversation history with the composer cleared; delivery is unconfirmed.")
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
                "end_call. Browser mode checks an exact HTTPS host; installed-app mode "
                "uses local Windows UI Automation and stops on ambiguous controls."
            )
        if action == "open":
            mode = str(params.get("mode", "browser")).lower()
            if mode in {"app", "desktop"}:
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
                    "This is local Windows accessibility-tree extraction; unsupported or uncertain controls are not treated as verified."
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
                    f"Mode: {mode or 'browser'}\nAfter approval, the controller will verify the active "
                    "conversation and prepare the message. Sending is attempted only if the target "
                    "is verified; the UI result is then checked. Do not approve unless correct."
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
                        if not recipient:
                            raise RuntimeError("A recipient is required for desktop call controls; no action was taken.")
                        before = _desktop_snapshot(platform, recipient)
                        if not before["active_conversation_title"] or (
                            recipient and recipient.casefold() not in before["active_conversation_title"].casefold()
                        ):
                            raise RuntimeError("The active desktop conversation header does not match the requested contact; no call control was clicked.")
                        result = _desktop_click({
                            "call": "Start voice call button",
                            "video_call": "Start video call button",
                            "end_call": "End call button",
                        }[control_action], platform, recipient)
                        after = _desktop_snapshot(platform, recipient)
                        return f"Desktop UI control clicked and screen re-read. Result: {result}. Visible text: {after['visible_text'][:1200]}. Call state still requires visual confirmation."
                    _, before = _checked_page(platform)
                    if recipient and recipient.casefold() not in before.casefold():
                        raise RuntimeError("The requested contact is not visible; no call control was clicked.")
                    result = _click_first(platform[control_action])
                    _, after = _checked_page(platform)
                    return f"Clicked UI control and re-read page. Control result: {result}. UI text after action: {after[:1200]}. Call state still requires visual confirmation."
            fingerprint = hashlib.sha256(
                f"{key}|{action}|{mode}|{recipient}|{message}".encode("utf-8")
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
