"""Isolated SQLAlchemy metadata for explicitly provisioned RFP tables."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer, MetaData, Numeric, String, Table, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

rfp_metadata_registry = MetaData()

rfp_tickets = Table(
    "rfp_tickets", rfp_metadata_registry,
    Column("ticket_id", UUID(as_uuid=True), primary_key=True), Column("rfp_id", Text),
    Column("status", String(32), nullable=False), Column("raw_pdf_path", Text, nullable=False),
    Column("markdown_path", Text), Column("discard_reason_code", String(128)), Column("discard_summary", Text),
    Column("uploaded_by_subject", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('analizando','descartado','analisis_completo','generando_borrador','en_evaluación','needs_human_review')", name="ck_rfp_ticket_status"),
    CheckConstraint("(status='descartado' AND discard_reason_code IS NOT NULL) OR (status<>'descartado' AND discard_reason_code IS NULL AND discard_summary IS NULL)", name="ck_rfp_ticket_discard_shape"),
)
rfp_metadata = Table(
    "rfp_metadata", rfp_metadata_registry,
    Column("ticket_id", UUID(as_uuid=True), ForeignKey("rfp_tickets.ticket_id", ondelete="RESTRICT"), primary_key=True),
    Column("client_name", Text), Column("client_country_code", String(2)), Column("client_country_original", Text),
    Column("operation_country_codes", JSONB), Column("currency", String(3)), Column("services_requested", JSONB, nullable=False),
    Column("monthly_volume_value", Numeric), Column("monthly_volume_unit", String(64)), Column("monthly_volume_source_ref", JSONB),
    Column("deadline", Date), Column("onboarding_target", Text), Column("budget_range", JSONB),
    Column("departments_needed", JSONB, nullable=False), Column("readability_metrics", JSONB, nullable=False), Column("conversion_metrics", JSONB, nullable=False),
    CheckConstraint("client_country_code IS NULL OR client_country_code IN ('ES','US')", name="ck_rfp_metadata_country"),
    CheckConstraint("currency IS NULL OR currency IN ('EUR','USD')", name="ck_rfp_metadata_currency"),
)
rfp_department_sections = Table(
    "rfp_department_sections", rfp_metadata_registry,
    Column("ticket_id", UUID(as_uuid=True), ForeignKey("rfp_tickets.ticket_id", ondelete="RESTRICT"), primary_key=True),
    Column("department_id", String(32), primary_key=True), Column("key_aspects", JSONB, nullable=False),
    Column("open_questions", JSONB, nullable=False), Column("source_references", JSONB, nullable=False), Column("detected_conflicts", JSONB, nullable=False),
    CheckConstraint("department_id IN ('warehouse','lastmile','reverse')", name="ck_rfp_department_id"),
)
rfp_handoffs = Table(
    "rfp_handoffs", rfp_metadata_registry,
    Column("ticket_id", UUID(as_uuid=True), ForeignKey("rfp_tickets.ticket_id", ondelete="RESTRICT"), primary_key=True),
    Column("contract_version", String(32), nullable=False), Column("summary_for_sales", Text, nullable=False), Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("contract_version='rfp-intake-v1'", name="ck_rfp_handoff_version"),
)
rfp_jobs = Table(
    "rfp_jobs", rfp_metadata_registry,
    Column("job_id", UUID(as_uuid=True), primary_key=True), Column("ticket_id", UUID(as_uuid=True), ForeignKey("rfp_tickets.ticket_id", ondelete="RESTRICT"), nullable=False),
    Column("job_kind", String(32), nullable=False, server_default="intake"),
    Column("status", String(32), nullable=False), Column("attempt_count", Integer, nullable=False, server_default="0"), Column("max_attempts", Integer, nullable=False, server_default="3"),
    Column("next_attempt_at", DateTime(timezone=True)), Column("claim_token", UUID(as_uuid=True)), Column("execution_epoch", Integer, nullable=False, server_default="0"),
    Column("lease_until", DateTime(timezone=True)), Column("heartbeat_at", DateTime(timezone=True)), Column("last_error_code", String(128)), Column("last_error_summary", String(500)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()), Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('queued','running','retry_wait','succeeded','failed','dead_letter')", name="ck_rfp_job_status"),
    CheckConstraint("job_kind IN ('intake','response_generation')", name="ck_rfp_job_kind"),
    CheckConstraint("max_attempts=3 AND attempt_count BETWEEN 0 AND 3", name="ck_rfp_job_attempts"),
)
rfp_outbox = Table(
    "rfp_outbox", rfp_metadata_registry,
    Column("outbox_id", UUID(as_uuid=True), primary_key=True), Column("job_id", UUID(as_uuid=True), ForeignKey("rfp_jobs.job_id", ondelete="RESTRICT"), nullable=False),
    Column("attempt_no", Integer, nullable=False), Column("status", String(32), nullable=False), Column("publish_attempts", Integer, nullable=False, server_default="0"),
    Column("next_publish_at", DateTime(timezone=True), nullable=False, server_default=func.now()), Column("lease_token", UUID(as_uuid=True)), Column("lease_until", DateTime(timezone=True)),
    Column("last_error_code", String(128)), Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()), Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("job_id", "attempt_no", name="uq_rfp_outbox_job_attempt"), CheckConstraint("attempt_no BETWEEN 1 AND 3", name="ck_rfp_outbox_attempt"),
    CheckConstraint("status IN ('pending','published','publish_failed')", name="ck_rfp_outbox_status"),
)
Index("ix_rfp_ticket_created", rfp_tickets.c.created_at)
Index("ix_rfp_job_ticket", rfp_jobs.c.ticket_id, rfp_jobs.c.created_at)
Index("uq_rfp_active_job_per_ticket", rfp_jobs.c.ticket_id, rfp_jobs.c.job_kind,
      unique=True, postgresql_where=rfp_jobs.c.status.in_(["queued", "running", "retry_wait"]))
Index("ix_rfp_outbox_ready", rfp_outbox.c.status, rfp_outbox.c.next_publish_at)
