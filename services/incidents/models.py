"""Isolated SQLAlchemy metadata for explicitly provisioned operational tables.

These tables deliberately do not participate in ``SQLModel.metadata.create_all``.
Production provisioning is controlled by ``infra/sql/incident_query_security.sql``.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)


operational_security_metadata = MetaData()

clients = Table(
    "trackflow_client",
    operational_security_metadata,
    Column("client_id", String(36), primary_key=True),
    Column("display_name", String(200), nullable=False, unique=True),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    Column("created_by_user_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

user_client_memberships = Table(
    "user_client_membership",
    operational_security_metadata,
    Column("user_id", String(64), primary_key=True),
    Column(
        "client_id",
        String(36),
        ForeignKey("trackflow_client.client_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("can_view_sensitive", Boolean, nullable=False, server_default="false"),
    Column("assigned_by_user_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

incidents = Table(
    "incident",
    operational_security_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "incident_id",
        String(10),
        Computed(
            "'TRF-' || substr('000000' || CAST(id AS VARCHAR), "
            "length('000000' || CAST(id AS VARCHAR)) - 5, 6)"
        ),
        nullable=False,
    ),
    Column(
        "client_id",
        String(36),
        ForeignKey("trackflow_client.client_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("incident_date", Date, nullable=False),
    Column("country", String(2), nullable=False),
    Column("customer_type", String(3), nullable=False),
    Column("tracking_number", String(200), nullable=False),
    Column("carrier", String(50), nullable=False),
    Column("category", String(40), nullable=False),
    Column("description", Text, nullable=False),
    Column("status", String(20), nullable=False),
    Column("customer_email", String(320), nullable=True),
    Column("satisfaction_score", Integer, nullable=True),
    Column("source_channel", String(20), nullable=False),
    Column("source_reference", String(200), nullable=True),
    Column("created_by_user_id", String(64), nullable=False),
    Column("updated_by_user_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("incident_id", name="uq_incident_public_id"),
    CheckConstraint("id BETWEEN 1 AND 999999", name="ck_incident_public_id_capacity"),
    CheckConstraint("version >= 1", name="ck_incident_version_positive"),
    CheckConstraint(
        "status IN ('OPEN', 'CLOSED', 'DISCARDED')",
        name="ck_incident_status",
    ),
    CheckConstraint(
        "category IN ('LOST_PARCEL', 'DELAYED_DELIVERY', 'WRONG_ADDRESS', 'RETURN_REQUEST', 'DAMAGE')",
        name="ck_incident_category",
    ),
    CheckConstraint("country IN ('US', 'ES')", name="ck_incident_country"),
    CheckConstraint("customer_type IN ('B2B', 'B2C')", name="ck_incident_customer_type"),
    CheckConstraint(
        "source_channel IN ('email', 'whatsapp', 'phone', 'backoffice')",
        name="ck_incident_source_channel",
    ),
    CheckConstraint(
        "satisfaction_score IS NULL OR satisfaction_score BETWEEN 1 AND 5",
        name="ck_incident_satisfaction_score",
    ),
    CheckConstraint(
        "(status = 'CLOSED' AND satisfaction_score IS NOT NULL) OR "
        "(status <> 'CLOSED' AND satisfaction_score IS NULL)",
        name="ck_incident_status_score",
    ),
)

incident_history = Table(
    "incident_history",
    operational_security_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "incident_pk",
        Integer,
        ForeignKey("incident.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("version", Integer, nullable=False),
    Column("event_type", String(20), nullable=False),
    Column("previous_status", String(20), nullable=True),
    Column("new_status", String(20), nullable=False),
    Column("reason", Text, nullable=True),
    Column("actor_user_id", String(64), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("incident_pk", "version", name="uq_incident_history_version"),
    CheckConstraint(
        "event_type IN ('CREATED', 'STATUS_CHANGED')",
        name="ck_incident_history_event_type",
    ),
    CheckConstraint(
        "new_status IN ('OPEN', 'CLOSED', 'DISCARDED') AND "
        "(previous_status IS NULL OR previous_status IN ('OPEN', 'CLOSED', 'DISCARDED'))",
        name="ck_incident_history_statuses",
    ),
    CheckConstraint(
        "(event_type = 'CREATED' AND version = 1 AND previous_status IS NULL AND new_status = 'OPEN') OR "
        "(event_type = 'STATUS_CHANGED' AND version >= 2 AND previous_status IS NOT NULL AND "
        "((previous_status = 'OPEN' AND new_status IN ('CLOSED', 'DISCARDED')) OR "
        "(previous_status = 'CLOSED' AND new_status = 'OPEN')))",
        name="ck_incident_history_transition",
    ),
    CheckConstraint(
        "NOT (previous_status = 'CLOSED' AND new_status = 'OPEN') OR "
        "(reason IS NOT NULL AND length(trim(reason)) > 0)",
        name="ck_incident_history_reopen_reason",
    ),
)

Index("ix_membership_client", user_client_memberships.c.client_id)
Index("ix_incident_client_updated", incidents.c.client_id, incidents.c.updated_at)
