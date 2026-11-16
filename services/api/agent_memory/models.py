"""Operational tables for explicitly provisioned TrackFlow agent memory."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Uuid,
    UniqueConstraint,
    func,
)

from services.incidents.models import operational_security_metadata


memory_conversations = Table(
    "agent_memory_conversation",
    operational_security_metadata,
    Column("id", Uuid(as_uuid=False), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "last_seen_at >= created_at",
        name="ck_agent_memory_conversation_times",
    ),
    UniqueConstraint(
        "id",
        "user_id",
        name="uq_agent_memory_conversation_owner",
    ),
)


memory_proposals = Table(
    "agent_memory_proposal",
    operational_security_metadata,
    Column("id", Uuid(as_uuid=False), primary_key=True),
    Column("conversation_id", Uuid(as_uuid=False), nullable=False),
    Column("user_id", String(64), nullable=False),
    Column("scope", String(10), nullable=False),
    Column(
        "client_id",
        String(36),
        ForeignKey("trackflow_client.client_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("category", String(40), nullable=False),
    Column("content", JSON, nullable=False),
    Column("canonical_key", String(200), nullable=False),
    Column("origin_interaction_id", String(64), nullable=False),
    Column("origin_message_sha256", String(64), nullable=False),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column(
        "parent_proposal_id",
        Uuid(as_uuid=False),
        ForeignKey("agent_memory_proposal.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("valid_until", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint(
        "user_id",
        "conversation_id",
        "origin_interaction_id",
        name="uq_agent_memory_proposal_origin",
    ),
    CheckConstraint("scope IN ('global','client')", name="ck_agent_memory_proposal_scope"),
    CheckConstraint(
        "(scope = 'global' AND client_id IS NULL) OR "
        "(scope = 'client' AND client_id IS NOT NULL)",
        name="ck_agent_memory_proposal_scope_client",
    ),
    CheckConstraint(
        "category IN ('carrier_coverage','recurring_incident','b2b_report_preference')",
        name="ck_agent_memory_proposal_category",
    ),
    CheckConstraint(
        "status IN ('pending','approved','rejected','edited','ambiguous_discard','expired')",
        name="ck_agent_memory_proposal_status",
    ),
    CheckConstraint(
        "(status = 'pending' AND resolved_at IS NULL) OR "
        "(status <> 'pending' AND resolved_at IS NOT NULL)",
        name="ck_agent_memory_proposal_resolution",
    ),
    CheckConstraint(
        "expires_at > created_at AND valid_until > created_at",
        name="ck_agent_memory_proposal_times",
    ),
    CheckConstraint(
        "category <> 'b2b_report_preference' OR scope = 'client'",
        name="ck_agent_memory_b2b_scope",
    ),
    CheckConstraint("version >= 1", name="ck_agent_memory_proposal_version"),
    ForeignKeyConstraint(
        ["conversation_id", "user_id"],
        ["agent_memory_conversation.id", "agent_memory_conversation.user_id"],
        name="fk_agent_memory_proposal_conversation_owner",
        ondelete="RESTRICT",
    ),
)

memory_decisions = Table(
    "agent_memory_decision",
    operational_security_metadata,
    Column("id", Uuid(as_uuid=False), primary_key=True),
    Column(
        "proposal_id",
        Uuid(as_uuid=False),
        ForeignKey("agent_memory_proposal.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    Column("outcome", String(24), nullable=False),
    Column("actor_user_id", String(64), nullable=False),
    Column("idempotency_key", String(64), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "outcome IN ('approved','rejected','edited','ambiguous_discard','expired')",
        name="ck_agent_memory_decision_outcome",
    ),
    UniqueConstraint(
        "actor_user_id",
        "idempotency_key",
        name="uq_agent_memory_decision_actor_idempotency",
    ),
)

memory_entries = Table(
    "agent_memory_entry",
    operational_security_metadata,
    Column("id", Uuid(as_uuid=False), primary_key=True),
    Column("scope", String(10), nullable=False),
    Column(
        "client_id",
        String(36),
        ForeignKey("trackflow_client.client_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("category", String(40), nullable=False),
    Column("content", JSON, nullable=False),
    Column("canonical_key", String(200), nullable=False),
    Column("verification_status", String(20), nullable=False, server_default="user_reported"),
    Column(
        "authorizing_decision_id",
        Uuid(as_uuid=False),
        ForeignKey("agent_memory_decision.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    Column(
        "supersedes_entry_id",
        Uuid(as_uuid=False),
        ForeignKey("agent_memory_entry.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("valid_from", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_by_user_id", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("scope IN ('global','client')", name="ck_agent_memory_entry_scope"),
    CheckConstraint(
        "(scope = 'global' AND client_id IS NULL) OR "
        "(scope = 'client' AND client_id IS NOT NULL)",
        name="ck_agent_memory_entry_scope_client",
    ),
    CheckConstraint(
        "category IN ('carrier_coverage','recurring_incident','b2b_report_preference')",
        name="ck_agent_memory_entry_category",
    ),
    CheckConstraint("status IN ('active','superseded','expired')", name="ck_agent_memory_entry_status"),
    CheckConstraint("version >= 1", name="ck_agent_memory_entry_version"),
    CheckConstraint(
        "verification_status IN ('user_reported','system_verified')",
        name="ck_agent_memory_entry_verification",
    ),
    CheckConstraint(
        "expires_at > valid_from AND updated_at >= created_at",
        name="ck_agent_memory_entry_times",
    ),
)

Index(
    "ix_agent_memory_conversation_owner",
    memory_conversations.c.user_id,
    memory_conversations.c.last_seen_at,
)
Index(
    "uq_agent_memory_one_pending_conversation",
    memory_proposals.c.user_id,
    memory_proposals.c.conversation_id,
    unique=True,
    sqlite_where=memory_proposals.c.status == "pending",
    postgresql_where=memory_proposals.c.status == "pending",
)
Index(
    "ix_agent_memory_proposal_expiry",
    memory_proposals.c.status,
    memory_proposals.c.expires_at,
)
Index(
    "uq_agent_memory_one_active_key",
    memory_entries.c.scope,
    func.coalesce(memory_entries.c.client_id, ""),
    memory_entries.c.category,
    memory_entries.c.canonical_key,
    unique=True,
    sqlite_where=memory_entries.c.status == "active",
    postgresql_where=memory_entries.c.status == "active",
)
Index(
    "ix_agent_memory_entry_lookup",
    memory_entries.c.scope,
    memory_entries.c.client_id,
    memory_entries.c.category,
    memory_entries.c.status,
)
