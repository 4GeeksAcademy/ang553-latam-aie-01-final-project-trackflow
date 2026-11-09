"""Small process-local TTL cache used by read-heavy API projections.

The cache is deliberately dependency-free and bounded by explicit invalidation
from the write paths. It is safe for concurrent requests within one process;
it is not a distributed cache and does not replace database consistency.
"""

from __future__ import annotations

import os
from itertools import count
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Callable, Generic, TypeVar
from weakref import WeakKeyDictionary


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

    def delete_prefix(self, prefix: str) -> int:
        """Delete every entry in a namespace and return the count."""
        with self._lock:
            keys = [key for key in self._entries if key.startswith(prefix)]
            for key in keys:
                del self._entries[key]
            return len(keys)

    def __len__(self) -> int:
        with self._lock:
            now = self._clock()
            expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
            for key in expired:
                del self._entries[key]
            return len(self._entries)


PRODUCTS_CACHE_TTL_SECONDS = float(
    os.getenv("TRACKFLOW_PRODUCTS_CACHE_TTL_SECONDS", "30")
)
ORDERS_CACHE_TTL_SECONDS = float(
    os.getenv("TRACKFLOW_ORDERS_CACHE_TTL_SECONDS", "15")
)

# Product/order projections use separate namespaces so a write can invalidate
# only the projections affected by it. The database identity is part of each
# key because tests and embedded deployments may use more than one engine in
# the same Python process.
products_cache: TTLCache[list] = TTLCache(PRODUCTS_CACHE_TTL_SECONDS)
orders_cache: TTLCache[list] = TTLCache(ORDERS_CACHE_TTL_SECONDS)
_bind_tokens: WeakKeyDictionary[object, int] = WeakKeyDictionary()
_bind_tokens_lock = RLock()
_bind_token_sequence = count(1)


def _bind_token(bind: object) -> str:
    """Return a process-unique token without relying on reusable object IDs."""
    try:
        with _bind_tokens_lock:
            token = _bind_tokens.get(bind)
            if token is None:
                token = next(_bind_token_sequence)
                _bind_tokens[bind] = token
        return str(token)
    except TypeError:
        # Non-weak-referenceable binds are unusual; disabling cache reuse is
        # safer than risking a projection from another database.
        return f"uncacheable-{next(_bind_token_sequence)}"


def inventory_cache_key(kind: str, session: object, access_scope: str = "global") -> str:
    """Build a process-local key scoped to the session's database engine."""
    bind = getattr(session, "bind", None)
    return f"{kind}:{_bind_token(bind)}:{access_scope}"


def invalidate_products_cache(session: object) -> None:
    """Invalidate the products projection for a database session."""
    products_cache.delete_prefix(inventory_cache_key("products", session, ""))


def invalidate_orders_cache(session: object) -> None:
    """Invalidate the orders projection for a database session."""
    orders_cache.delete_prefix(inventory_cache_key("orders", session, ""))


def invalidate_inventory_cache(session: object) -> None:
    """Invalidate both inventory projections after a movement write."""
    invalidate_products_cache(session)
    invalidate_orders_cache(session)
