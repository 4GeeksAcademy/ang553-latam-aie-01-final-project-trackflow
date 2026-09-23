"""Persistence helpers for validated telemetry events.

This module only describes the telemetry table and builds SQLAlchemy Core
statements.  Table provisioning is owned by ``infra/sql/telemetry_events.sql``.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import Column, DateTime, MetaData, Numeric, Table, Text, insert, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from services.api.telemetry_schemas import TelemetryEvent


telemetry_metadata = MetaData()

telemetry_events = Table(
    "telemetry_events",
    telemetry_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("service", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("level", Text, server_default=text("'info'")),
    Column("value", Numeric, nullable=True),
    Column("message", Text, nullable=True),
    Column("tags", JSONB, server_default=text("'{}'::jsonb")),
)


EVENT_PROPERTY_ALLOWLISTS: dict[str, frozenset[str]] = {
    "inbound_order_created": frozenset(
        {"warehouse", "client_id", "product_id", "product_category", "quantity", "movement_id"}
    ),
    "outbound_order_created": frozenset(
        {"warehouse", "client_id", "product_id", "product_category", "quantity", "movement_id"}
    ),
    "stock_threshold_triggered": frozenset(
        {"warehouse", "client_id", "product_id", "product_category", "quantity", "threshold_quantity"}
    ),
    "direct_stock_edit_rejected": frozenset(
        {"warehouse", "client_id", "product_id", "product_category", "quantity"}
    ),
    "inventory_discrepancy_detected": frozenset(
        {
            "warehouse",
            "client_id",
            "product_id",
            "product_category",
            "quantity",
            "system_quantity",
            "counted_quantity",
            "discrepancy_delta",
        }
    ),
    "auth_login_succeeded": frozenset({"role"}),
    "auth_login_failed": frozenset({"failure_reason", "role_if_known"}),
    "auth_password_reset_requested": frozenset({"request_outcome_class"}),
    "auth_password_reset_completed": frozenset(),
    "inventory_validation_failed": frozenset(
        {"operation", "failure_reason", "warehouse", "product_id", "status_code"}
    ),
    "inventory_stock_insufficient": frozenset(
        {
            "warehouse",
            "client_id",
            "product_id",
            "product_category",
            "requested_quantity",
            "available_quantity",
        }
    ),
    "api_request_slow": frozenset(
        {"method", "path_template", "status_code", "duration_ms", "threshold_ms"}
    ),
    "api_request_failed": frozenset(
        {"method", "path_template", "status_code", "duration_ms"}
    ),
    "backoffice_section_entered": frozenset({"section"}),
    "inventory_workflow_started": frozenset({"workflow"}),
    "inventory_workflow_abandoned": frozenset(
        {"workflow", "abandonment_stage", "had_validation_error", "duration_ms"}
    ),
    "inventory_product_created": frozenset(
        {"warehouse", "client_id", "product_id", "product_category"}
    ),
}


def filter_tags(event_type: str, properties: dict[str, Any]) -> dict[str, Any]:
    """Return only the properties allowed for a known event type."""
    allowed = EVENT_PROPERTY_ALLOWLISTS.get(event_type)
    if not allowed or not properties:
        return {}
    return {key: value for key, value in properties.items() if key in allowed}


def map_event_to_row(event: TelemetryEvent) -> dict[str, Any]:
    """Map a validated event envelope to the insertable telemetry columns."""
    return {
        "timestamp": event.timestamp,
        "service": "backoffice",
        "event_type": event.event_type,
        "level": "info",
        "value": None,
        "message": None,
        "tags": filter_tags(event.event_type, event.properties),
    }


def bulk_insert_telemetry(session: Any, rows: Iterable[dict[str, Any]]) -> None:
    """Insert all supplied rows with one multi-row statement and transaction."""
    rows = list(rows)
    if not rows:
        return

    try:
        session.execute(insert(telemetry_events).values(rows))
        session.commit()
    except Exception:
        session.rollback()
        raise
