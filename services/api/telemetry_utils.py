"""Shared helpers for telemetry vocabulary normalization."""

from __future__ import annotations


def canonical_telemetry_warehouse(warehouse: str) -> str | None:
    """Map supported domain warehouse values to telemetry vocabulary."""
    return {
        "LA": "los_angeles",
        "ZGZ": "zaragoza",
        "Los Angeles": "los_angeles",
        "Zaragoza": "zaragoza",
    }.get(warehouse)
