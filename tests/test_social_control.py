"""Unit tests for the API-free social UI controller."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

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
