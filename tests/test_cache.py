"""Unit tests for the process-local TTL cache primitive."""

from __future__ import annotations

import pytest

from services.api.cache import TTLCache


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_cache_returns_value_before_ttl() -> None:
    clock = Clock()
    cache = TTLCache[str](ttl_seconds=10, clock=clock)
    cache.set("products", "snapshot")

    clock.advance(9.999)

    assert cache.get("products") == "snapshot"
    assert len(cache) == 1


def test_cache_expires_at_ttl_boundary() -> None:
    clock = Clock()
    cache = TTLCache[str](ttl_seconds=10, clock=clock)
    cache.set("products", "snapshot")

    clock.advance(10)

    assert cache.get("products") is None
    assert len(cache) == 0


def test_cache_delete_and_clear_are_idempotent() -> None:
    cache = TTLCache[int](ttl_seconds=10)
    cache.set("a", 1)
    cache.set("b", 2)

    assert cache.delete("a") is True
    assert cache.delete("a") is False
    cache.clear()
    cache.clear()

    assert cache.get("b") is None
    assert len(cache) == 0


def test_cache_rejects_non_positive_ttl() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        TTLCache[int](ttl_seconds=0)
