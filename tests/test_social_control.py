"""Unit tests for the API-free social UI controller."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "actions" / "social_control.py"
SPEC = importlib.util.spec_from_file_location("social_control_test_module", MODULE_PATH)
social = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(social)


def test_platform_allowlist_rejects_unknown_platform():
    try:
        social._platform("unknown-network")
    except ValueError as exc:
        assert "Unsupported social platform" in str(exc)
    else:
        raise AssertionError("Unknown platform should be rejected")


def test_only_exact_https_host_is_accepted():
    platform = social._PLATFORMS["whatsapp"]
    with patch.object(social, "_browser", side_effect=["http://web.whatsapp.com/", "should not be read"]):
        try:
            social._checked_page(platform)
        except RuntimeError as exc:
            assert "not a supported HTTPS page" in str(exc)
        else:
            raise AssertionError("HTTP URL should be rejected")


def test_unrelated_host_is_rejected_before_reading_text():
    platform = social._PLATFORMS["whatsapp"]
    with patch.object(social, "_browser", return_value="https://evil.example/") as browser:
        try:
            social._checked_page(platform)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Unexpected host should be rejected")
        assert browser.call_count == 1


def test_inspect_reports_bridge_errors_as_failure():
    platform = social._PLATFORMS["whatsapp"]
    with patch.object(social, "_browser", side_effect=["https://web.whatsapp.com/", "Bridge error: timeout"]):
        try:
            social._checked_page(platform)
        except RuntimeError as exc:
            assert "Could not verify" in str(exc)
        else:
            raise AssertionError("Bridge error should not be reported as page content")


def test_unverified_recipient_stops_before_composer_is_touched():
    platform = social._PLATFORMS["whatsapp"]
    with patch.object(social, "_checked_page", side_effect=[
        ("https://web.whatsapp.com/", "Inbox"),
        ("https://web.whatsapp.com/", "Chat with Alice"),
    ]), patch.object(social, "_click_first") as click, patch.object(social, "_type_first") as type_text:
        try:
            social._prepare_message(platform, "Bob", "Hello")
        except RuntimeError as exc:
            assert "could not be verified after selection" in str(exc)
        else:
            raise AssertionError("Unverified recipient must stop the operation")
        click.assert_any_call(platform["search"])
        click.assert_any_call(("Bob",))
        type_text.assert_any_call(platform["search"], "Bob")
        assert type_text.call_count == 1


def test_empty_message_is_rejected():
    platform = social._PLATFORMS["whatsapp"]
    try:
        social._prepare_message(platform, "Alice", "  ")
    except ValueError as exc:
        assert "Message must contain" in str(exc)
    else:
        raise AssertionError("Empty message should be rejected")


def test_action_contract_is_discoverable():
    assert social.TOOL["name"] == "social_control"
    assert callable(social.TOOL["handler"])
    assert social.TOOL["parameters"]["required"] == ["platform", "action"]



def test_desktop_prepare_stops_when_active_header_does_not_match():
    platform = social._PLATFORMS["whatsapp"]
    snapshots = [
        {"visible_text": "Inbox", "active_conversation_title": "", "message_composer_text": "", "visible_controls": []},
        {"visible_text": "Alice Bob", "active_conversation_title": "", "message_composer_text": "", "visible_controls": []},
        {"visible_text": "Chat with Alice", "active_conversation_title": "Alice", "message_composer_text": "", "visible_controls": []},
    ]
    with patch.object(social, "_desktop_window", return_value=MagicMock()), \
         patch.object(social, "_desktop_snapshot", side_effect=snapshots), \
         patch.object(social, "_desktop_click") as click, \
         patch.object(social, "_desktop_type") as type_text:
        try:
            social._desktop_prepare_message(platform, "Bob", "Hello")
        except RuntimeError as exc:
            assert "active conversation header could not be verified" in str(exc)
        else:
            raise AssertionError("A mismatched desktop conversation must stop")
        assert not any(call.args[0] == "message composer" for call in click.call_args_list)
        type_text.assert_called_once_with(platform, "Bob")


def test_desktop_prepare_requires_exact_composer_draft():
    platform = social._PLATFORMS["whatsapp"]
    snapshots = [
        {"visible_text": "Inbox", "active_conversation_title": "", "message_composer_text": "", "visible_controls": []},
        {"visible_text": "Alice", "active_conversation_title": "", "message_composer_text": "", "visible_controls": []},
        {"visible_text": "Alice", "active_conversation_title": "Alice", "message_composer_text": "", "visible_controls": []},
        {"visible_text": "Alice", "active_conversation_title": "Alice", "message_composer_text": "Hello", "visible_controls": []},
    ]
    with patch.object(social, "_desktop_window", return_value=MagicMock()), \
         patch.object(social, "_desktop_snapshot", side_effect=snapshots), \
         patch.object(social, "_desktop_click") as click, \
         patch.object(social, "_desktop_type") as type_text:
        result = social._desktop_prepare_message(platform, "Alice", "Hello")
    assert "Draft prepared" in result
    assert click.call_args_list[-1].args[0] == "message composer"
    type_text.assert_any_call(platform, "Hello")


def test_desktop_send_never_clicks_send_if_preparation_fails():
    platform = social._PLATFORMS["whatsapp"]
    with patch.object(social, "_desktop_prepare_message", side_effect=RuntimeError("header not verified")), \
         patch.object(social, "_desktop_click") as click:
        try:
            social._desktop_send_message(platform, "Alice", "Hello")
        except RuntimeError as exc:
            assert "header not verified" in str(exc)
        else:
            raise AssertionError("Unverified preparation must stop before sending")
        click.assert_not_called()


def test_desktop_send_requires_message_in_history_and_cleared_composer():
    platform = social._PLATFORMS["whatsapp"]
    verified = {
        "visible_text": "Alice\\nHello",
        "active_conversation_title": "Alice",
        "message_composer_text": "",
        "visible_controls": [],
    }
    with patch.object(social, "_desktop_prepare_message", return_value="Draft prepared"), \
         patch.object(social, "_desktop_click") as click, \
         patch.object(social, "_desktop_snapshot", return_value=verified):
        result = social._desktop_send_message(platform, "Alice", "Hello")
    assert "does not prove server receipt" in result
    click.assert_called_once_with("send message button", platform)


def test_desktop_send_fails_if_text_remains_in_composer():
    platform = social._PLATFORMS["whatsapp"]
    still_draft = {
        "visible_text": "Alice\\nHello",
        "active_conversation_title": "Alice",
        "message_composer_text": "Hello",
        "visible_controls": [],
    }
    with patch.object(social, "_desktop_prepare_message", return_value="Draft prepared"), \
         patch.object(social, "_desktop_click"), \
         patch.object(social, "_desktop_snapshot", return_value=still_draft):
        try:
            social._desktop_send_message(platform, "Alice", "Hello")
        except RuntimeError as exc:
            assert "composer cleared" in str(exc)
        else:
            raise AssertionError("A still-populated composer must not be treated as a sent message")


def test_every_configured_social_platform_has_an_approved_app_launcher_alias():
    from actions.open_app import _normalize
    for platform in social._PLATFORMS.values():
        assert _normalize(platform["app"]) is not None, platform["app"]


def test_plan_message_is_read_only_and_orders_verification_before_send():
    with patch.object(social, "_browser") as browser, patch("core.execution.request_execution") as request:
        result = social.social_control({
            "platform": "whatsapp",
            "action": "plan",
            "target_action": "send_message",
            "recipient": "Alice",
            "message": "Hello",
            "mode": "browser",
        })
    assert "Preflight" in result
    assert "verify the active conversation" in result
    assert "Send once" in result
    assert "do not claim delivery/read receipt" in result
    browser.assert_not_called()
    request.assert_not_called()


def test_capabilities_do_not_advertise_instagram_calls():
    result = social.social_control({"platform": "instagram", "action": "capabilities"})
    assert "start_call" not in result
    assert "start_video_call" not in result
    assert "end_call" not in result


def test_unsupported_call_is_rejected_before_confirmation():
    with patch("core.execution.request_execution") as request:
        result = social.social_control({
            "platform": "instagram",
            "action": "start_call",
            "recipient": "Alice",
            "mode": "browser",
        })
    assert "not configured" in result
    request.assert_not_called()


def test_call_requires_explicit_recipient_before_confirmation():
    with patch("core.execution.request_execution") as request:
        result = social.social_control({
            "platform": "whatsapp",
            "action": "start_call",
            "mode": "browser",
        })
    assert "recipient/contact is required" in result
    request.assert_not_called()


def test_uncertain_click_does_not_retry_another_label():
    with patch.object(social, "_browser", return_value="Bridge error: timeout") as browser:
        try:
            social._click_first(("Send", "Message"))
        except RuntimeError as exc:
            assert "outcome" in str(exc).lower() or "uncertain" in str(exc).lower()
        else:
            raise AssertionError("An uncertain click must fail closed")
        browser.assert_called_once()


def test_missing_locator_can_try_next_label_without_claiming_click():
    with patch.object(social, "_browser", side_effect=[
        "Could not find element: Search",
        "Clicked element: Search Messenger",
    ]) as browser:
        result = social._click_first(("Search", "Search Messenger"))
    assert "Clicked element" in result
    assert browser.call_count == 2


def test_message_validation_rejects_control_chars_in_recipient_and_nul_in_message():
    for recipient, message in [("Alice" + chr(10) + "Bob", "Hello"), ("Alice", "Hello" + chr(0) + "World")]:
        try:
            social._validate_message_request(recipient, message)
        except ValueError:
            pass
        else:
            raise AssertionError("Unsafe input must be rejected before UI interaction")


def test_unsupported_open_mode_is_rejected_without_opening_browser():
    with patch.object(social, "_browser") as browser:
        result = social.social_control({
            "platform": "whatsapp",
            "action": "open",
            "mode": "unknown",
        })
    assert "Unsupported mode" in result
    browser.assert_not_called()


def test_plan_rejects_calls_not_configured_for_platform():
    result = social.social_control({
        "platform": "instagram",
        "action": "plan",
        "target_action": "start_call",
        "recipient": "Alice",
    })
    assert "not configured" in result



def test_ambiguous_click_result_is_not_treated_as_success():
    with patch.object(social, "_browser", return_value="Ambiguous click target for 'Send': 2 matches. No click was performed.") as browser:
        try:
            social._click_first(("Send", "Message"))
        except RuntimeError as exc:
            assert "uncertain" in str(exc).lower() or "outcome" in str(exc).lower()
        else:
            raise AssertionError("Ambiguous click target must fail closed")
        browser.assert_called_once()


def test_ambiguous_input_result_is_not_treated_as_success():
    with patch.object(social, "_browser", return_value="Ambiguous input target for 'Message': 2 matches. No text was entered.") as browser:
        try:
            social._type_first(("Message",), "private text")
        except RuntimeError as exc:
            assert "uncertain" in str(exc).lower() or "outcome" in str(exc).lower()
        else:
            raise AssertionError("Ambiguous input target must fail closed")
        browser.assert_called_once()
