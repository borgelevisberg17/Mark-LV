"""Regression tests for unique Playwright social-control locators."""
import sys
import types
import unittest

# Keep this unit test dependency-free: it never starts a browser or localhost bridge.
if "playwright.async_api" not in sys.modules:
    playwright_package = types.ModuleType("playwright")
    playwright_api = types.ModuleType("playwright.async_api")
    class PlaywrightTimeout(Exception):
        pass
    playwright_api.async_playwright = lambda: None
    playwright_api.BrowserContext = object
    playwright_api.Page = object
    playwright_api.Playwright = object
    playwright_api.TimeoutError = PlaywrightTimeout
    sys.modules.setdefault("playwright", playwright_package)
    sys.modules["playwright.async_api"] = playwright_api

if "core.browser_bridge" not in sys.modules:
    bridge_module = types.ModuleType("core.browser_bridge")
    class FakeBrowserBridge:
        def start(self): return False
        def connected(self): return False
    bridge_module.BrowserBridge = FakeBrowserBridge
    sys.modules["core.browser_bridge"] = bridge_module

from actions.browser_control import _BrowserSession


class FakeLocator:
    def __init__(self, count=0, click_error=None, fill_error=None):
        self.matches = count
        self.click_error = click_error
        self.fill_error = fill_error
        self.click_calls = 0
        self.fill_calls = []

    async def count(self):
        return self.matches

    async def click(self, **_kwargs):
        self.click_calls += 1
        if self.click_error:
            raise self.click_error

    async def fill(self, value, **_kwargs):
        self.fill_calls.append(value)
        if self.fill_error:
            raise self.fill_error


class FakePage:
    def __init__(self, role_locators=None, placeholder_locators=None, label_locators=None):
        self.role_locators = role_locators or {}
        self.placeholder_locators = placeholder_locators or {}
        self.label_locators = label_locators or {}

    def get_by_role(self, role, name=None, exact=None):
        return self.role_locators.get((role, name), FakeLocator())

    def get_by_text(self, _text, exact=None):
        return FakeLocator()

    def get_by_placeholder(self, text, exact=None):
        return self.placeholder_locators.get(text, FakeLocator())

    def get_by_label(self, text, exact=None):
        return self.label_locators.get(text, FakeLocator())

    def get_by_alt_text(self, _text, exact=None):
        return FakeLocator()

    def get_by_title(self, _text, exact=None):
        return FakeLocator()


def make_session(page):
    session = _BrowserSession.__new__(_BrowserSession)
    async def get_page():
        return page
    session._get_page = get_page
    return session


class UniqueLocatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_clicks_exactly_one_match(self):
        target = FakeLocator(count=1)
        page = FakePage(role_locators={("button", "Send"): target})
        result = await make_session(page).smart_click("Send")
        self.assertIn("Clicked unique target", result)
        self.assertEqual(target.click_calls, 1)

    async def test_ambiguous_click_fails_closed_without_fallback(self):
        target = FakeLocator(count=2)
        fallback = FakeLocator(count=1)
        page = FakePage(role_locators={
            ("button", "Send"): target,
            ("link", "Send"): fallback,
        })
        result = await make_session(page).smart_click("Send")
        self.assertIn("Ambiguous target", result)
        self.assertEqual(target.click_calls, 0)
        self.assertEqual(fallback.click_calls, 0)

    async def test_uncertain_click_does_not_try_another_locator(self):
        target = FakeLocator(count=1, click_error=TimeoutError("timeout"))
        fallback = FakeLocator(count=1)
        page = FakePage(role_locators={
            ("button", "Send"): target,
            ("link", "Send"): fallback,
        })
        result = await make_session(page).smart_click("Send")
        self.assertIn("outcome uncertain", result)
        self.assertEqual(target.click_calls, 1)
        self.assertEqual(fallback.click_calls, 0)

    async def test_ambiguous_input_does_not_type(self):
        target = FakeLocator(count=2)
        page = FakePage(placeholder_locators={"Message": target})
        result = await make_session(page).smart_type("Message", "private text")
        self.assertIn("Ambiguous input", result)
        self.assertEqual(target.fill_calls, [])

    async def test_unique_input_uses_fill_once(self):
        target = FakeLocator(count=1)
        page = FakePage(placeholder_locators={"Message": target})
        result = await make_session(page).smart_type("Message", "hello")
        self.assertIn("Typed into unique input", result)
        self.assertEqual(target.fill_calls, ["hello"])


if __name__ == "__main__":
    unittest.main()
