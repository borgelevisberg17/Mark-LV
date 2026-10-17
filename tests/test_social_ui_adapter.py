"""Tests for the API-free browser social UI adapter."""
import unittest

from core.social_controller import SocialAction, SocialControllerRegistry
from core.social_ui_adapter import BrowserSocialUIController


class FakeUIDriver:
    def __init__(self, url, text="Inbox\nVisible conversation content"):
        self.url = url
        self.text = text
        self.url_calls = 0
        self.text_calls = 0

    def current_url(self):
        self.url_calls += 1
        return self.url

    def visible_text(self):
        self.text_calls += 1
        return self.text


class BrowserSocialUIControllerTests(unittest.TestCase):
    def make_controller(self, url, text="Inbox\nVisible conversation content"):
        driver = FakeUIDriver(url, text)
        controller = BrowserSocialUIController("whatsapp", "WhatsApp Web", driver)
        return controller, driver

    def test_reads_visible_text_from_approved_https_host(self):
        controller, driver = self.make_controller("https://web.whatsapp.com/")
        registry = SocialControllerRegistry()
        registry.register(controller)

        result = registry.dispatch("whatsapp", SocialAction.INSPECT_VISIBLE_PAGE)

        self.assertTrue(result.success)
        self.assertTrue(result.verified)
        self.assertIn("Visible conversation content", result.details["visible_text"])
        self.assertEqual(driver.url_calls, 1)
        self.assertEqual(driver.text_calls, 1)
        self.assertEqual(controller.descriptor.metadata["uses_social_api"], "false")

    def test_rejects_wrong_host_without_reading_page_text(self):
        controller, driver = self.make_controller("https://evil.example/web.whatsapp.com")
        result = controller.execute(SocialAction.INSPECT_VISIBLE_PAGE, {})

        self.assertFalse(result.success)
        self.assertEqual(driver.text_calls, 0)

    def test_rejects_http_even_when_hostname_matches(self):
        controller, driver = self.make_controller("http://web.whatsapp.com/")
        result = controller.execute(SocialAction.INSPECT_VISIBLE_PAGE, {})

        self.assertFalse(result.success)
        self.assertEqual(driver.text_calls, 0)

    def test_empty_visible_text_is_not_success(self):
        controller, _ = self.make_controller("https://web.whatsapp.com/", "  ")
        result = controller.execute(SocialAction.INSPECT_VISIBLE_PAGE, {})

        self.assertFalse(result.success)
        self.assertFalse(result.verified)

    def test_does_not_claim_message_sending_capability(self):
        controller, _ = self.make_controller("https://web.whatsapp.com/")
        self.assertTrue(controller.descriptor.supports(SocialAction.INSPECT_VISIBLE_PAGE))
        self.assertFalse(controller.descriptor.supports(SocialAction.SEND_MESSAGE))
        self.assertFalse(controller.descriptor.supports(SocialAction.READ_MESSAGES))

    def test_unknown_app_is_rejected(self):
        with self.assertRaises(ValueError):
            BrowserSocialUIController("unknown", "Unknown", FakeUIDriver("https://example.com")).descriptor


if __name__ == "__main__":
    unittest.main()
