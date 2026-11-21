"""Parte 2 tables on the isolated explicit RFP SQLAlchemy metadata."""

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Index, Integer,
    String, Table, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from services.rfp_intake.models import rfp_metadata_registry


rfp_response_runs = Table(
    "rfp_response_runs", rfp_metadata_registry,
    Column("run_id", UUID(as_uuid=True), primary_key=True),
    Column("ticket_id", UUID(as_uuid=True), ForeignKey("rfp_tickets.ticket_id", ondelete="RESTRICT"), nullable=False),
    Column("source_handoff_version", String(32), nullable=False),
    Column("source_handoff_hash", String(64), nullable=False),
    Column("carrier_model", String(32), nullable=False, server_default="unknown"),
    Column("carrier_confirmation_actor", String(128)),
    Column("carrier_confirmation_source", String(64), nullable=False,
           server_default="legacy_migration"),
    Column("carrier_confirmed_at", DateTime(timezone=True)),
    Column("status", String(32), nullable=False),
    Column("max_business_iterations", Integer, nullable=False, server_default="3"),
    Column("global_iteration_count", Integer, nullable=False, server_default="0"),
    Column("state_version", Integer, nullable=False, server_default="0"),
    Column("claim_token", UUID(as_uuid=True)),
    Column("execution_epoch", Integer, nullable=False, server_default="0"),
    Column("lease_until", DateTime(timezone=True)), Column("heartbeat_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("ticket_id", "source_handoff_hash", name="uq_rfp_response_source"),
    CheckConstraint("source_handoff_version='rfp-intake-v1'", name="ck_rfp_response_source_version"),
    CheckConstraint("carrier_model IN ('unknown','trackflow_managed','customer_managed')",
                    name="ck_rfp_response_carrier_model"),
    CheckConstraint("status IN ('pending','generando_borrador','en_evaluación','ready_for_part3','needs_human_review','technical_failure')", name="ck_rfp_response_run_status"),
    CheckConstraint("max_business_iterations=3 AND global_iteration_count BETWEEN 0 AND 3", name="ck_rfp_response_iterations"),
)

rfp_response_sections = Table(
    "rfp_response_sections", rfp_metadata_registry,
    Column("run_id", UUID(as_uuid=True), ForeignKey("rfp_response_runs.run_id", ondelete="RESTRICT"), primary_key=True),
    Column("department_id", String(32), primary_key=True), Column("status", String(32), nullable=False),
    Column("iteration_count", Integer, nullable=False, server_default="0"),
    Column("final_draft_id", UUID(as_uuid=True)), Column("final_evaluation_id", UUID(as_uuid=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("department_id IN ('warehouse','lastmile','reverse')", name="ck_rfp_response_section_department"),
    CheckConstraint("status IN ('pending','generando_borrador','en_evaluación','passed','needs_human_input','iteration_limit_reached','technical_failure')", name="ck_rfp_response_section_status"),
    CheckConstraint("iteration_count BETWEEN 0 AND 3", name="ck_rfp_response_section_iterations"),
)

rfp_response_drafts = Table(
    "rfp_response_drafts", rfp_metadata_registry,
    Column("draft_id", UUID(as_uuid=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("rfp_response_runs.run_id", ondelete="RESTRICT"), nullable=False),
    Column("scope_key", String(32), nullable=False), Column("iteration_count", Integer, nullable=False),
    Column("contract_version", String(64), nullable=False), Column("payload", JSONB, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("run_id", "scope_key", "iteration_count", name="uq_rfp_response_draft_iteration"),
    CheckConstraint("scope_key IN ('warehouse','lastmile','reverse','proposal')", name="ck_rfp_response_draft_scope"),
    CheckConstraint("iteration_count BETWEEN 1 AND 3", name="ck_rfp_response_draft_iteration"),
)

rfp_response_previews = Table(
    "rfp_response_previews", rfp_metadata_registry,
    Column("preview_id", UUID(as_uuid=True), primary_key=True),
    Column("draft_id", UUID(as_uuid=True), ForeignKey("rfp_response_drafts.draft_id", ondelete="RESTRICT"), nullable=False, unique=True),
    Column("gate_status", String(32), nullable=False), Column("safe_content", Text),
    Column("reason_codes", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("gate_status IN ('passed','blocked','indeterminate','technical_error')", name="ck_rfp_response_preview_status"),
)

rfp_response_evaluator_results = Table(
    "rfp_response_evaluator_results", rfp_metadata_registry,
    Column("evaluator_result_id", UUID(as_uuid=True), primary_key=True),
    Column("draft_id", UUID(as_uuid=True), ForeignKey("rfp_response_drafts.draft_id", ondelete="RESTRICT"), nullable=False),
    Column("evaluator", String(32), nullable=False), Column("contract_version", String(64), nullable=False),
    Column("payload", JSONB, nullable=False), Column("payload_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("draft_id", "evaluator", name="uq_rfp_response_evaluator_draft"),
    CheckConstraint("evaluator IN ('readability','relevance','section_compliance','global_compliance')", name="ck_rfp_response_evaluator"),
)

rfp_response_evaluations = Table(
    "rfp_response_evaluations", rfp_metadata_registry,
    Column("evaluation_id", UUID(as_uuid=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("rfp_response_runs.run_id", ondelete="RESTRICT"), nullable=False),
    Column("draft_id", UUID(as_uuid=True), ForeignKey("rfp_response_drafts.draft_id", ondelete="RESTRICT"), nullable=False, unique=True),
    Column("scope_key", String(32), nullable=False), Column("contract_version", String(64), nullable=False),
    Column("overall_pass", Boolean, nullable=False), Column("terminal_status", String(32)),
    Column("payload", JSONB, nullable=False), Column("payload_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("scope_key IN ('warehouse','lastmile','reverse','proposal')", name="ck_rfp_response_evaluation_scope"),
)

rfp_response_execution_failures = Table(
    "rfp_response_execution_failures", rfp_metadata_registry,
    Column("failure_id", UUID(as_uuid=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("rfp_response_runs.run_id", ondelete="RESTRICT"), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), ForeignKey("rfp_tickets.ticket_id", ondelete="RESTRICT"), nullable=False),
    Column("draft_id", UUID(as_uuid=True), ForeignKey("rfp_response_drafts.draft_id", ondelete="RESTRICT")),
    Column("evaluator", String(32), nullable=False), Column("infrastructure_attempts", Integer, nullable=False),
    Column("department", String(32)),
    Column("error_code", String(64), nullable=False), Column("diagnostic_reference", String(500), nullable=False),
    Column("terminal", Boolean, nullable=False), Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

rfp_response_llm_invocations = Table(
    "rfp_response_llm_invocations", rfp_metadata_registry,
    Column("invocation_id", UUID(as_uuid=True), primary_key=True),
    Column("ticket_id", UUID(as_uuid=True),
           ForeignKey("rfp_tickets.ticket_id", ondelete="RESTRICT"), nullable=False),
    Column("run_id", UUID(as_uuid=True),
           ForeignKey("rfp_response_runs.run_id", ondelete="RESTRICT"), nullable=False),
    Column("artifact_id", UUID(as_uuid=True), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("model", String(200), nullable=False),
    Column("stage", String(32), nullable=False),
    Column("department", String(32), nullable=False),
    Column("call_index", Integer, nullable=False),
    Column("provider_request_id", String(200)),
    Column("latency_ms", Float, nullable=False),
    Column("estimated_input_tokens", Integer, nullable=False),
    Column("prompt_tokens", Integer),
    Column("completion_tokens", Integer),
    Column("total_tokens", Integer),
    Column("outcome", String(32), nullable=False),
    Column("error_category", String(64)),
    Column("validation_path", String(200)),
    Column("validation_error_type", String(64)),
    Column("validation_received_type", String(16)),
    Column("violated_constraint", String(64)),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("provider='fourgeeks_gateway'",
                    name="ck_rfp_response_llm_provider"),
    CheckConstraint("stage IN ('department_generation','relevance_evaluation')",
                    name="ck_rfp_response_llm_stage"),
    CheckConstraint("department IN ('warehouse','lastmile','reverse')",
                    name="ck_rfp_response_llm_department"),
    CheckConstraint("call_index>=1 AND latency_ms>=0 AND estimated_input_tokens>=0",
                    name="ck_rfp_response_llm_metrics"),
    CheckConstraint("outcome IN ('succeeded','technical_error','invalid_output')",
                    name="ck_rfp_response_llm_outcome"),
    CheckConstraint(
        "(outcome='succeeded' AND error_category IS NULL) OR "
        "(outcome<>'succeeded' AND error_category IS NOT NULL)",
        name="ck_rfp_response_llm_error_shape"),
)

rfp_response_handoffs = Table(
    "rfp_response_handoffs", rfp_metadata_registry,
    Column("run_id", UUID(as_uuid=True), ForeignKey("rfp_response_runs.run_id", ondelete="RESTRICT"), primary_key=True),
    Column("ticket_id", UUID(as_uuid=True), ForeignKey("rfp_tickets.ticket_id", ondelete="RESTRICT"), nullable=False, unique=True),
    Column("contract_version", String(32), nullable=False), Column("outcome", String(32), nullable=False),
    Column("payload", JSONB, nullable=False), Column("payload_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("contract_version='rfp-response-v1'", name="ck_rfp_response_handoff_version"),
    CheckConstraint("outcome IN ('ready_for_part3','needs_human_review')", name="ck_rfp_response_handoff_outcome"),
)

Index("uq_rfp_response_active_run", rfp_response_runs.c.ticket_id, unique=True,
      postgresql_where=rfp_response_runs.c.status.in_(["pending", "generando_borrador", "en_evaluación"]))
Index("ix_rfp_response_run_ticket", rfp_response_runs.c.ticket_id, rfp_response_runs.c.created_at)
Index("ix_rfp_response_draft_run", rfp_response_drafts.c.run_id, rfp_response_drafts.c.scope_key, rfp_response_drafts.c.iteration_count)
Index("ix_rfp_response_failure_run", rfp_response_execution_failures.c.run_id, rfp_response_execution_failures.c.created_at)
Index("ix_rfp_response_llm_run", rfp_response_llm_invocations.c.run_id,
      rfp_response_llm_invocations.c.created_at)
