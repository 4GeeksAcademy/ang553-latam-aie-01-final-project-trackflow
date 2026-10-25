"""Pandas-based analysis pipeline for telemetry events.

All metric functions receive an explicit time window via ``start_date`` / ``end_date``.
No default temporal window is applied — that is the caller's responsibility.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import select

from services.api.telemetry_storage import telemetry_events


# ── Event-type allowlist for operational-failure metrics ─────────────────────

OPERATIONAL_FAILURE_ALLOWLIST: frozenset[str] = frozenset({
    "auth_login_failed",
    "inventory_validation_failed",
    "inventory_stock_insufficient",
    "api_request_failed",
})

# ── Metric A ─────────────────────────────────────────────────────────────────


def event_volume_by_day_and_type(
    session: Any,
    start_date: datetime,
    end_date: datetime,
) -> list[dict[str, Any]]:
    """How many events of each type were observed per day?

    Returns
    -------
    list[dict]
        Each record: ``{"day": "YYYY-MM-DD", "event_type": str, "count": int}``.
    """
    stmt = (
        select(
            telemetry_events.c.timestamp,
            telemetry_events.c.event_type,
        )
        .where(telemetry_events.c.timestamp >= start_date)
        .where(telemetry_events.c.timestamp < end_date)
    )
    rows = session.execute(stmt).mappings().all()
    if not rows:
        return []

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["day"] = df["timestamp"].dt.strftime("%Y-%m-%d")

    grouped = (
        df.groupby(["day", "event_type"])
        .size()
        .reset_index(name="count")
        .sort_values(by=["day", "event_type"])
    )
    return grouped.to_dict(orient="records")


# ── Metric B ─────────────────────────────────────────────────────────────────


def operational_failures_by_type_and_day(
    session: Any,
    start_date: datetime,
    end_date: datetime,
) -> list[dict[str, Any]]:
    """How many operational/technical failures of each type occurred per day?

    Only event types matching ``OPERATIONAL_FAILURE_ALLOWLIST`` are included.
    """
    stmt = (
        select(
            telemetry_events.c.timestamp,
            telemetry_events.c.event_type,
        )
        .where(telemetry_events.c.timestamp >= start_date)
        .where(telemetry_events.c.timestamp < end_date)
        .where(telemetry_events.c.event_type.in_(OPERATIONAL_FAILURE_ALLOWLIST))
    )
    rows = session.execute(stmt).mappings().all()
    if not rows:
        return []

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["day"] = df["timestamp"].dt.strftime("%Y-%m-%d")

    grouped = (
        df.groupby(["day", "event_type"])
        .size()
        .reset_index(name="count")
        .sort_values(by=["day", "event_type"])
    )
    return grouped.to_dict(orient="records")


# ── Metric C ─────────────────────────────────────────────────────────────────


def stock_insufficiency_by_day_warehouse_category(
    session: Any,
    start_date: datetime,
    end_date: datetime,
) -> list[dict[str, Any]]:
    """Where and when do stock-insufficient events concentrate?

    Returns
    -------
    list[dict]
        Each record: ``{"day": "YYYY-MM-DD", "warehouse": str,
        "product_category": str, "count": int}``.
    """
    stmt = (
        select(
            telemetry_events.c.timestamp,
            telemetry_events.c.tags,
        )
        .where(telemetry_events.c.timestamp >= start_date)
        .where(telemetry_events.c.timestamp < end_date)
        .where(telemetry_events.c.event_type == "inventory_stock_insufficient")
    )
    rows = session.execute(stmt).mappings().all()
    if not rows:
        return []

    df = pd.DataFrame(rows)

    # Defensive normalisation of tags
    df["tags"] = df["tags"].apply(
        lambda t: t if isinstance(t, dict) else {}
    )
    df["warehouse"] = df["tags"].apply(lambda t: t.get("warehouse"))
    df["product_category"] = df["tags"].apply(lambda t: t.get("product_category"))

    # Discard rows where either dimension is missing
    df = df.dropna(subset=["warehouse", "product_category"])

    if df.empty:
        return []

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["day"] = df["timestamp"].dt.strftime("%Y-%m-%d")

    grouped = (
        df.groupby(["day", "warehouse", "product_category"])
        .size()
        .reset_index(name="count")
        .sort_values(by=["day", "warehouse", "product_category"])
    )
    return grouped.to_dict(orient="records")


# ── Report builder ───────────────────────────────────────────────────────────


def build_telemetry_report(
    session: Any,
    start_date: datetime,
    end_date: datetime,
) -> dict[str, Any]:
    """Build a full telemetry analysis report for the given time window.

    Parameters
    ----------
    session
        A SQLAlchemy / SQLModel database session.
    start_date
        Inclusive start of the analysis window (UTC).
    end_date
        Exclusive end of the analysis window (UTC).

    Returns
    -------
    dict
        ``{"period": {"from": str, "to": str}, "metrics": {...}}``.
    """
    period_from = start_date.astimezone(timezone.utc).isoformat()
    period_to = end_date.astimezone(timezone.utc).isoformat()

    return {
        "period": {
            "from": period_from,
            "to": period_to,
        },
        "metrics": {
            "event_volume_by_day_and_type": event_volume_by_day_and_type(
                session, start_date, end_date,
            ),
            "operational_failures_by_type_and_day": operational_failures_by_type_and_day(
                session, start_date, end_date,
            ),
            "stock_insufficiency_by_day_warehouse_category": stock_insufficiency_by_day_warehouse_category(
                session, start_date, end_date,
            ),
        },
    }