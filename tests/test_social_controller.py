"""Tests for the universal social controller contract."""
import unittest

from core.social_controller import (
    SocialAction, SocialActionResult, SocialAppDescriptor, SocialCapability,
    SocialControllerRegistry,
)


class FakeController:
    def __init__(self, app_id="sample", enabled=True, actions=None, result=None, raises=False):
        self._descriptor = SocialAppDescriptor(
            app_id=app_id,
            display_name="Sample App",
            enabled=enabled,
            capabilities=tuple(
                SocialCapability(action, True)
                for action in (actions or [SocialAction.READ_MESSAGES])
            ),
        )
        self.result = result or SocialActionResult(True, "Read complete.", verified=True)
        self.raises = raises
        self.calls = 0

    @property
    def descriptor(self):
        return self._descriptor

    def execute(self, action, parameters):
        self.calls += 1
        if self.raises:
            raise RuntimeError("private message content must not leak")
        return self.result


class SocialControllerRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = SocialControllerRegistry()
        self.controller = FakeController()
        self.registry.register(self.controller)

    def test_dispatches_declared_capability(self):
        result = self.registry.dispatch("sample", SocialAction.READ_MESSAGES, {"limit": 5})
        self.assertTrue(result.success)
        self.assertTrue(result.verified)
        self.assertEqual(self.controller.calls, 1)

    def test_unsupported_capability_is_not_invoked(self):
        result = self.registry.dispatch("sample", SocialAction.SEND_MESSAGE, {"text": "secret"})
        self.assertFalse(result.success)
        self.assertEqual(self.controller.calls, 0)

    def test_unknown_or_disabled_app_is_not_invoked(self):
        self.assertFalse(self.registry.dispatch("missing", SocialAction.READ_MESSAGES).success)
        disabled = SocialControllerRegistry()
        disabled.register(FakeController(app_id="disabled", enabled=False))
        self.assertFalse(disabled.dispatch("disabled", SocialAction.READ_MESSAGES).success)

    def test_duplicate_registration_is_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register(FakeController())

    def test_success_without_verification_is_downgraded(self):
        registry = SocialControllerRegistry()
        controller = FakeController(result=SocialActionResult(True, "sent", verified=False))
        registry.register(controller)
        result = registry.dispatch("sample", SocialAction.READ_MESSAGES)
        self.assertFalse(result.success)
        self.assertFalse(result.verified)

    def test_controller_exception_is_not_reported_as_success(self):
        registry = SocialControllerRegistry()
        controller = FakeController(raises=True)
        registry.register(controller)
        result = registry.dispatch("sample", SocialAction.READ_MESSAGES)
        self.assertFalse(result.success)
        self.assertFalse(result.verified)
        self.assertNotIn("private message content", result.message)

    def test_noncanonical_app_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register(FakeController(app_id=" Sample "))


if __name__ == "__main__":
    unittest.main()
