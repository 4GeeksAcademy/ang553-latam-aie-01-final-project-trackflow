"""Unit tests for the process-local TTL cache primitive."""

from __future__ import annotations

import pytest

from services.api.cache import (
    ORDERS_CACHE_TTL_SECONDS,
    PRODUCTS_CACHE_TTL_SECONDS,
    TTLCache,
    invalidate_products_cache,
    inventory_cache_key,
    orders_cache,
    products_cache,
)


def test_inventory_cache_ttl_configuration_is_resource_specific() -> None:
    assert PRODUCTS_CACHE_TTL_SECONDS == 30
    assert ORDERS_CACHE_TTL_SECONDS == 15


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


def test_missing_key_returns_none() -> None:
    cache = TTLCache[str](ttl_seconds=10)

    assert cache.get("missing") is None


def test_set_same_key_replaces_value_and_restarts_ttl() -> None:
    clock = Clock()
    cache = TTLCache[str](ttl_seconds=10, clock=clock)
    cache.set("products", "first")
    clock.advance(9)
    cache.set("products", "second")
    clock.advance(9)

    assert cache.get("products") == "second"
    clock.advance(1)
    assert cache.get("products") is None


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


@pytest.mark.parametrize("ttl_seconds", [0, -1])
def test_cache_rejects_non_positive_ttl(ttl_seconds: float) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        TTLCache[int](ttl_seconds=ttl_seconds)


def test_invalidate_products_preserves_orders() -> None:
    class Session:
        bind = object()

    session = Session()
    products_key = inventory_cache_key("products", session)
    orders_key = inventory_cache_key("orders", session)
    products_cache.set(products_key, ["products"])
    orders_cache.set(orders_key, ["orders"])

    invalidate_products_cache(session)

    assert products_cache.get(products_key) is None
    assert orders_cache.get(orders_key) == ["orders"]
