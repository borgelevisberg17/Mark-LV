"""Small in-memory rate limiter for local dashboard authentication endpoints.

The limiter intentionally has no external dependencies and accepts an injectable
monotonic clock so its behaviour can be tested without sleeping.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Callable


class LoginAttemptLimiter:
    """Temporarily block an address after repeated failed authentication attempts."""

    def __init__(
        self,
        max_attempts: int = 8,
        window_seconds: float = 60.0,
        block_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if window_seconds <= 0 or block_seconds <= 0:
            raise ValueError("window_seconds and block_seconds must be positive")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.block_seconds = block_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}
        self._lock = Lock()

    def is_limited(self, identity: str) -> bool:
        """Return whether this identity is currently blocked."""
        now = self._clock()
        key = str(identity or "unknown")
        with self._lock:
            blocked_until = self._blocked_until.get(key, 0.0)
            if blocked_until > now:
                return True
            if blocked_until:
                self._blocked_until.pop(key, None)
            self._prune(key, now)
            return False

    def record_failure(self, identity: str) -> bool:
        """Record a failed attempt; return True if the identity is now blocked."""
        now = self._clock()
        key = str(identity or "unknown")
        with self._lock:
            if self._blocked_until.get(key, 0.0) > now:
                return True
            self._prune(key, now)
            attempts = self._failures[key]
            attempts.append(now)
            if len(attempts) >= self.max_attempts:
                self._blocked_until[key] = now + self.block_seconds
                attempts.clear()
                return True
            return False

    def record_success(self, identity: str) -> None:
        """Reset failed attempts after a successful authentication."""
        key = str(identity or "unknown")
        with self._lock:
            self._failures.pop(key, None)
            self._blocked_until.pop(key, None)

    def _prune(self, key: str, now: float) -> None:
        attempts = self._failures.get(key)
        if attempts is None:
            return
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            self._failures.pop(key, None)
