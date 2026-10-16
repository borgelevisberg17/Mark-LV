import unittest

from dashboard.security import LoginAttemptLimiter


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LoginAttemptLimiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = LoginAttemptLimiter(
            max_attempts=3,
            window_seconds=10,
            block_seconds=20,
            clock=self.clock,
        )

    def test_blocks_after_threshold(self):
        self.assertFalse(self.limiter.record_failure("192.0.2.1"))
        self.assertFalse(self.limiter.record_failure("192.0.2.1"))
        self.assertTrue(self.limiter.record_failure("192.0.2.1"))
        self.assertTrue(self.limiter.is_limited("192.0.2.1"))

    def test_unblocks_after_block_period(self):
        for _ in range(3):
            self.limiter.record_failure("client")
        self.clock.advance(20)
        self.assertFalse(self.limiter.is_limited("client"))
        self.assertFalse(self.limiter.record_failure("client"))

    def test_old_failures_expire_from_window(self):
        self.limiter.record_failure("client")
        self.clock.advance(11)
        self.limiter.record_failure("client")
        self.limiter.record_failure("client")
        self.assertFalse(self.limiter.is_limited("client"))

    def test_success_clears_failures_and_block(self):
        self.limiter.record_failure("client")
        self.limiter.record_success("client")
        self.assertFalse(self.limiter.is_limited("client"))
        self.assertFalse(self.limiter.record_failure("client"))

    def test_identities_are_isolated(self):
        for _ in range(3):
            self.limiter.record_failure("client-a")
        self.assertTrue(self.limiter.is_limited("client-a"))
        self.assertFalse(self.limiter.is_limited("client-b"))

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            LoginAttemptLimiter(max_attempts=0, clock=self.clock)
        with self.assertRaises(ValueError):
            LoginAttemptLimiter(window_seconds=0, clock=self.clock)


if __name__ == "__main__":
    unittest.main()
