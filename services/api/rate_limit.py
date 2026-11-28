"""Small process-local rate limiters for authenticated API boundaries."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import os
from threading import Lock
import time
from typing import Callable


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int | None = None


@dataclass
class _Window:
    started_at: float
    requests: int


class FixedWindowRateLimiter:
    """Bounded, thread-safe fixed-window limiter keyed by trusted identity.

    State intentionally remains process-local. A deployment with multiple API
    workers therefore needs a shared limiter before treating the configured
    ceiling as a global guarantee.
    """

    def __init__(
        self,
        *,
        requests: int,
        window_seconds: int,
        max_identities: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if requests <= 0:
            raise ValueError("requests must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_identities <= 0:
            raise ValueError("max_identities must be positive")
        self.requests = requests
        self.window_seconds = window_seconds
        self.max_identities = max_identities
        self._clock = clock
        self._windows: OrderedDict[str, _Window] = OrderedDict()
        self._lock = Lock()

    @classmethod
    def from_env(cls) -> "FixedWindowRateLimiter":
        return cls(
            requests=_bounded_int(
                "TRACKFLOW_SUPPORT_RATE_LIMIT_REQUESTS", default=60, maximum=10_000
            ),
            window_seconds=_bounded_int(
                "TRACKFLOW_SUPPORT_RATE_LIMIT_WINDOW_SECONDS",
                default=60,
                maximum=86_400,
            ),
            max_identities=_bounded_int(
                "TRACKFLOW_SUPPORT_RATE_LIMIT_MAX_IDENTITIES",
                default=10_000,
                maximum=1_000_000,
            ),
        )

    def admit(self, identity_key: str) -> RateLimitDecision:
        if not identity_key:
            raise ValueError("identity_key must not be empty")
        with self._lock:
            # Reading the clock and mutating the window are one atomic operation.
            # Otherwise an older timestamp can enter after a newer request and
            # incorrectly discard the newer window.
            now = self._clock()
            if not math.isfinite(now):
                raise ValueError("rate limiter clock must be finite")
            self._discard_expired(now)
            window = self._windows.get(identity_key)
            if window is None:
                if len(self._windows) >= self.max_identities:
                    return RateLimitDecision(
                        allowed=False,
                        retry_after_seconds=self._capacity_retry_after(now),
                    )
                self._windows[identity_key] = _Window(now, 1)
                return RateLimitDecision(allowed=True)

            # A monotonic clock should not regress. Resetting this identity is
            # conservative for availability and keeps retry calculations valid
            # for injected clocks used in deterministic tests.
            if (
                now < window.started_at
                or now - window.started_at >= self.window_seconds
            ):
                self._windows[identity_key] = _Window(now, 1)
                self._windows.move_to_end(identity_key)
                return RateLimitDecision(allowed=True)

            self._windows.move_to_end(identity_key)
            if window.requests >= self.requests:
                return RateLimitDecision(
                    allowed=False,
                    retry_after_seconds=_positive_ceiling(
                        window.started_at + self.window_seconds - now
                    ),
                )
            window.requests += 1
            return RateLimitDecision(allowed=True)

    def clear(self) -> None:
        """Drop process-local counters; intended for controlled test teardown."""

        with self._lock:
            self._windows.clear()

    def _discard_expired(self, now: float) -> None:
        expired = [
            key
            for key, window in self._windows.items()
            if now < window.started_at
            or now - window.started_at >= self.window_seconds
        ]
        for key in expired:
            self._windows.pop(key, None)

    def _capacity_retry_after(self, now: float) -> int:
        if not self._windows:
            return 1
        first_expiry = min(
            window.started_at + self.window_seconds
            for window in self._windows.values()
        )
        return _positive_ceiling(first_expiry - now)


def _bounded_int(name: str, *, default: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if value <= 0 or value > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _positive_ceiling(value: float) -> int:
    return max(1, math.ceil(value))
