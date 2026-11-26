"""SQLAlchemy metadata for durable chat history, generations and cursors."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from services.incidents.models import operational_security_metadata


metadata = operational_security_metadata

chat_sessions = Table(
    "chat_sessions",
    metadata,
    Column("session_id", String(64), primary_key=True),
    Column("agent_id", String(64), nullable=False, server_default="first_line_cx"),
    Column("user_id", String(64), nullable=False),
    Column("client_id", String(36), ForeignKey("trackflow_client.client_id", ondelete="RESTRICT"), nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()),
    CheckConstraint("agent_id = 'first_line_cx'", name="ck_chat_session_agent"),
    CheckConstraint("status IN ('active','interrupted','closed')", name="ck_chat_session_status"),
)
Index("ix_chat_sessions_owner", chat_sessions.c.user_id, chat_sessions.c.client_id, chat_sessions.c.created_at)

chat_messages = Table(
    "chat_messages",
    metadata,
    Column("message_id", String(64), primary_key=True),
    Column("session_id", String(64), ForeignKey("chat_sessions.session_id", ondelete="RESTRICT"), nullable=False),
    Column("role", String(16), nullable=False),
    Column("content", Text, nullable=False, server_default=""),
    Column("status", String(16), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()),
    CheckConstraint("role IN ('user','assistant')", name="ck_chat_message_role"),
    CheckConstraint("status IN ('completed','streaming','interrupted','failed')", name="ck_chat_message_status"),
    CheckConstraint("sequence > 0", name="ck_chat_message_sequence"),
    UniqueConstraint("session_id", "sequence", name="uq_chat_message_sequence"),
)
Index("ix_chat_messages_session_created", chat_messages.c.session_id, chat_messages.c.sequence)

chat_generations = Table(
    "chat_generations",
    metadata,
    Column("generation_id", String(64), primary_key=True),
    Column("session_id", String(64), nullable=False),
    Column("user_message_id", String(64), nullable=False),
    Column("assistant_message_id", String(64), nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("chunk_sequence", BigInteger, nullable=False, server_default="0"),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()),
    Column("ended_at", DateTime(timezone=True)),
    ForeignKeyConstraint(("session_id",), ("chat_sessions.session_id",), ondelete="RESTRICT"),
    ForeignKeyConstraint(("user_message_id",), ("chat_messages.message_id",), ondelete="RESTRICT"),
    ForeignKeyConstraint(("assistant_message_id",), ("chat_messages.message_id",), ondelete="RESTRICT"),
    CheckConstraint("status IN ('active','completed','interrupted','failed')", name="ck_chat_generation_status"),
    CheckConstraint("chunk_sequence >= 0", name="ck_chat_generation_chunk_sequence"),
    UniqueConstraint("assistant_message_id", name="uq_chat_generation_assistant"),
)
Index(
    "uq_chat_generation_active",
    chat_generations.c.session_id,
    unique=True,
    postgresql_where=chat_generations.c.status == "active",
)

chat_event_cursors = Table(
    "chat_event_cursors",
    metadata,
    Column("session_id", String(64), ForeignKey("chat_sessions.session_id", ondelete="RESTRICT"), primary_key=True),
    Column("last_cursor", BigInteger, nullable=False, server_default="0"),
    CheckConstraint("last_cursor >= 0", name="ck_chat_event_cursor_nonnegative"),
)

chat_events = Table(
    "chat_events",
    metadata,
    Column("session_id", String(64), nullable=False),
    Column("cursor", BigInteger, nullable=False),
    Column("event_type", String(32), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()),
    ForeignKeyConstraint(("session_id",), ("chat_sessions.session_id",), ondelete="RESTRICT"),
    CheckConstraint("cursor > 0", name="ck_chat_event_positive_cursor"),
    CheckConstraint(
        "event_type IN ('token_chunk','coverage_updated','generation_interrupted','generation_completed')",
        name="ck_chat_event_type",
    ),
    PrimaryKeyConstraint("session_id", "cursor", name="pk_chat_events"),
)
Index("ix_chat_events_session_recorded", chat_events.c.session_id, chat_events.c.recorded_at)
