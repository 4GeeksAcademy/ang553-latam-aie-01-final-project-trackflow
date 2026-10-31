"""Transactional outbox primitives for authentic inventory business events.

The table uses metadata separate from ``SQLModel.metadata`` so importing the API
cannot provision DDL in the operational database.  Authorized environments must
apply ``infra/sql/inventory_event_outbox.sql`` explicitly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    insert,
)
from sqlalchemy.dialects.postgresql import JSONB


OUTBOX_SOURCE_EVENT_TYPES = frozenset(
    {
        ("stockentry", "inbound_order_created"),
        ("commercial_order", "outbound_order_created"),
        ("stockexit", "stock_threshold_triggered"),
        ("inventory_physical_count", "inventory_discrepancy_detected"),
    }
)
OUTBOX_SOURCE_TABLES = frozenset(source for source, _event in OUTBOX_SOURCE_EVENT_TYPES)
OUTBOX_EVENT_TYPES = frozenset(event for _source, event in OUTBOX_SOURCE_EVENT_TYPES)

inventory_outbox_metadata = MetaData()
inventory_event_outbox = Table(
    "inventory_event_outbox",
    inventory_outbox_metadata,
    Column("event_id", Uuid(as_uuid=True), primary_key=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("source_table", Text, nullable=False),
    Column("source_pk", BigInteger, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("tags", JSON().with_variant(JSONB, "postgresql"), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("last_attempt_at", DateTime(timezone=True), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "(source_table = 'stockentry' AND event_type = 'inbound_order_created') OR "
        "(source_table = 'commercial_order' AND event_type = 'outbound_order_created') OR "
        "(source_table = 'stockexit' AND event_type = 'stock_threshold_triggered') OR "
        "(source_table = 'inventory_physical_count' AND "
        "event_type = 'inventory_discrepancy_detected')",
        name="ck_inventory_event_outbox_source_event_pair",
    ),
    CheckConstraint(
        "attempt_count >= 0", name="ck_inventory_event_outbox_attempt_count"
    ),
    UniqueConstraint(
        "source_table",
        "source_pk",
        "event_type",
        name="uq_inventory_event_outbox_source_event",
    ),
)
Index(
    "idx_inventory_event_outbox_pending",
    inventory_event_outbox.c.occurred_at,
    inventory_event_outbox.c.event_id,
    postgresql_where=inventory_event_outbox.c.published_at.is_(None),
)


def enqueue_inventory_event(
    session: Any,
    *,
    event_id: UUID,
    occurred_at: datetime,
    source_table: str,
    source_pk: int,
    event_type: str,
    tags: Mapping[str, Any],
) -> None:
    """Insert one movement event using the caller's uncommitted transaction."""
    if not isinstance(event_id, UUID) or event_id.version != 4:
        raise ValueError("event_id must be a UUID v4")
    if occurred_at.tzinfo is None or occurred_at.utcoffset() != timezone.utc.utcoffset(
        occurred_at
    ):
        raise ValueError("occurred_at must be timezone-aware UTC")
    if source_table not in OUTBOX_SOURCE_TABLES:
        raise ValueError("source_table is not an authorized inventory event source")
    if not isinstance(source_pk, int) or isinstance(source_pk, bool) or source_pk <= 0:
        raise ValueError("source_pk must be a positive integer")
    if event_type not in OUTBOX_EVENT_TYPES:
        raise ValueError("event_type is not an authorized inventory business event")
    if (source_table, event_type) not in OUTBOX_SOURCE_EVENT_TYPES:
        raise ValueError("source_table and event_type are not an authorized pair")
    if not isinstance(tags, Mapping):
        raise ValueError("tags must be a mapping")

    session.execute(
        insert(inventory_event_outbox).values(
            event_id=event_id,
            occurred_at=occurred_at,
            source_table=source_table,
            source_pk=source_pk,
            event_type=event_type,
            tags=dict(tags),
            published_at=None,
            attempt_count=0,
            last_attempt_at=None,
            last_error=None,
            created_at=occurred_at,
        )
    )
