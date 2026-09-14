"""Small process-local TTL cache used by read-heavy API projections.

The cache is deliberately dependency-free and bounded by explicit invalidation
from the write paths. It is safe for concurrent requests within one process;
it is not a distributed cache and does not replace database consistency.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Callable, Generic, TypeVar


T = TypeVar("T")
_MISSING = object()


@dataclass(frozen=True)
class _Entry(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    """A minimal thread-safe cache with lazy TTL expiration.

    ``clock`` is injectable so expiration can be tested without sleeping.
    Values are returned by reference; callers should cache immutable response
    projections or treat returned collections as read-only.
    """

    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = monotonic) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _Entry[T]] = {}
        self._lock = RLock()

    def get(self, key: str, default: T | None = None) -> T | None:
        """Return a non-expired value, or ``default`` on a miss."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return default
            if entry.expires_at <= self._clock():
                del self._entries[key]
                return default
            return entry.value

    def set(self, key: str, value: T) -> None:
        """Store or replace a value with a fresh TTL."""
        with self._lock:
            self._entries[key] = _Entry(value, self._clock() + self.ttl_seconds)

    def delete(self, key: str) -> bool:
        """Remove a key and report whether it existed."""
        with self._lock:
            return self._entries.pop(key, _MISSING) is not _MISSING

    def clear(self) -> None:
        """Remove all cached values."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            now = self._clock()
            expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
            for key in expired:
                del self._entries[key]
            return len(self._entries)


# Product/order projections will share this process-local cache in the next
# milestones. Keeping the primitive independent makes the TTL contract
# testable before endpoint integration.
