"""Parte 3 tables on the isolated explicit RFP SQLAlchemy metadata.

The declarations mirror ``infra/sql/rfp_approval.sql``.  They do not create
tables automatically and contain no authorization or workflow behavior.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from services.rfp_intake.models import rfp_metadata_registry
from services.rfp_response.models import (  # noqa: F401
    rfp_response_drafts,
    rfp_response_evaluations,
    rfp_response_handoffs,
)


rfp_principals = Table(
    "rfp_principals",
    rfp_metadata_registry,
    Column("principal_id", UUID(as_uuid=True), primary_key=True),
    Column("environment", String(16), nullable=False),
    Column("trackflow_user_id", Text, nullable=False),
    Column("responsible_code", String(32)),
    Column("synthetic", Boolean, nullable=False, server_default="false"),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("disabled_at", DateTime(timezone=True)),
    CheckConstraint("environment IN ('production','qa')", name="ck_rfp_principal_environment"),
    CheckConstraint(
        "responsible_code IS NULL OR responsible_code IN "
        "('ana_whitfield','carlos_vega','sofia_ramos','miguel_torres')",
        name="ck_rfp_principal_responsible",
    ),
    CheckConstraint("NOT synthetic OR environment='qa'", name="ck_rfp_principal_synthetic_environment"),
    CheckConstraint(
        "(active AND disabled_at IS NULL) OR (NOT active AND disabled_at IS NOT NULL)",
        name="ck_rfp_principal_disabled_shape",
    ),
    UniqueConstraint("principal_id", "environment", name="uq_rfp_principal_id_environment"),
    UniqueConstraint("environment", "trackflow_user_id", name="uq_rfp_principal_user_environment"),
    UniqueConstraint("environment", "responsible_code", name="uq_rfp_principal_responsible_environment"),
)

rfp_identity_bindings = Table(
    "rfp_identity_bindings",
    rfp_metadata_registry,
    Column("binding_id", UUID(as_uuid=True), primary_key=True),
    Column("principal_id", UUID(as_uuid=True), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("auth_source", String(16), nullable=False),
    Column("issuer", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("disabled_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["principal_id", "environment"],
        ["rfp_principals.principal_id", "rfp_principals.environment"],
        name="fk_rfp_identity_principal_environment",
        ondelete="RESTRICT",
    ),
    CheckConstraint("environment IN ('production','qa')", name="ck_rfp_identity_environment"),
    CheckConstraint("auth_source IN ('local','oidc')", name="ck_rfp_identity_source"),
    CheckConstraint(
        "(active AND disabled_at IS NULL) OR (NOT active AND disabled_at IS NOT NULL)",
        name="ck_rfp_identity_disabled_shape",
    ),
    CheckConstraint(
        "auth_source<>'local' OR issuer IN ('trackflow-local','urn:trackflow:qa:local')",
        name="ck_rfp_identity_local_issuer",
    ),
    UniqueConstraint("environment", "issuer", "subject", name="uq_rfp_identity_issuer_subject"),
    UniqueConstraint(
        "binding_id", "principal_id", "environment",
        name="uq_rfp_identity_binding_principal_environment",
    ),
)

rfp_authorizations = Table(
    "rfp_authorizations",
    rfp_metadata_registry,
    Column("authorization_id", UUID(as_uuid=True), primary_key=True),
    Column("principal_id", UUID(as_uuid=True), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("permission", String(32), nullable=False),
    Column("department_scope", String(32)),
    Column("trigger_scope", String(32)),
    Column("target_policy", String(32), nullable=False),
    Column("scope_fingerprint", String(64), nullable=False),
    Column("authorization_version", Integer, nullable=False, server_default="1"),
    Column("active_from", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
    Column("granted_by_principal_id", UUID(as_uuid=True), ForeignKey("rfp_principals.principal_id", ondelete="RESTRICT")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["principal_id", "environment"],
        ["rfp_principals.principal_id", "rfp_principals.environment"],
        name="fk_rfp_authorization_principal_environment",
        ondelete="RESTRICT",
    ),
    CheckConstraint("environment IN ('production','qa')", name="ck_rfp_authorization_environment"),
    CheckConstraint(
        "permission IN ('start_approval_run','manage_approval_run','approve_department',"
        "'provide_business_input','arbitrate')",
        name="ck_rfp_authorization_permission",
    ),
    CheckConstraint(
        "department_scope IS NULL OR department_scope IN ('warehouse','lastmile','reverse')",
        name="ck_rfp_authorization_department",
    ),
    CheckConstraint(
        "trigger_scope IS NULL OR trigger_scope IN "
        "('volume-vs-capacity','returns-sla-breach','currency-mismatch')",
        name="ck_rfp_authorization_trigger",
    ),
    CheckConstraint(
        "target_policy IN ('run','department_exact','proposal_wide','cross_department',"
        "'non_reverse_or_cross')",
        name="ck_rfp_authorization_policy",
    ),
    CheckConstraint("scope_fingerprint ~ '^[a-f0-9]{64}$'", name="ck_rfp_authorization_hash"),
    CheckConstraint("authorization_version>=1", name="ck_rfp_authorization_version"),
    CheckConstraint(
        "(expires_at IS NULL OR expires_at>active_from) AND "
        "(revoked_at IS NULL OR revoked_at>=active_from)",
        name="ck_rfp_authorization_time",
    ),
    CheckConstraint(
        "(permission IN ('start_approval_run','manage_approval_run') AND department_scope IS NULL "
        "AND trigger_scope IS NULL AND target_policy='run') OR "
        "(permission='approve_department' AND department_scope IS NOT NULL AND trigger_scope IS NULL "
        "AND target_policy='department_exact') OR "
        "(permission='provide_business_input' AND trigger_scope IS NULL AND "
        "((department_scope IS NOT NULL AND target_policy='department_exact') OR "
        "(department_scope IS NULL AND target_policy='proposal_wide'))) OR "
        "(permission='arbitrate' AND trigger_scope IS NOT NULL AND target_policy IN "
        "('department_exact','proposal_wide','cross_department','non_reverse_or_cross'))",
        name="ck_rfp_authorization_scope_shape",
    ),
    UniqueConstraint(
        "authorization_id", "principal_id", "environment", "authorization_version",
        name="uq_rfp_authorization_identity",
    ),
)
Index(
    "uq_rfp_active_authorization_scope",
    rfp_authorizations.c.principal_id,
    rfp_authorizations.c.permission,
    rfp_authorizations.c.scope_fingerprint,
    unique=True,
    postgresql_where=rfp_authorizations.c.revoked_at.is_(None),
)
Index(
    "ix_rfp_authorization_lookup",
    rfp_authorizations.c.principal_id,
    rfp_authorizations.c.permission,
    rfp_authorizations.c.active_from,
    rfp_authorizations.c.expires_at,
)

rfp_approval_runs = Table(
    "rfp_approval_runs",
    rfp_metadata_registry,
    Column("approval_run_id", UUID(as_uuid=True), primary_key=True),
    Column("ticket_id", UUID(as_uuid=True), ForeignKey("rfp_tickets.ticket_id", ondelete="RESTRICT"), nullable=False),
    Column("response_run_id", UUID(as_uuid=True), nullable=False),
    Column("contract_version", String(64), nullable=False),
    Column("response_handoff_version", String(32), nullable=False),
    Column("response_handoff_hash", String(64), nullable=False),
    Column("active_departments", JSONB, nullable=False),
    Column("max_review_revisions", Integer, nullable=False, server_default="3"),
    Column("status", String(32), nullable=False),
    Column("state_version", Integer, nullable=False, server_default="0"),
    Column("start_idempotency_key", UUID(as_uuid=True), nullable=False, unique=True),
    Column("start_request_hash", String(64), nullable=False),
    Column("started_by_principal_id", UUID(as_uuid=True), nullable=False),
    Column("started_by_binding_id", UUID(as_uuid=True), nullable=False),
    Column("start_authorization_id", UUID(as_uuid=True), nullable=False),
    Column("authorization_version", Integer, nullable=False),
    Column("started_by_general_role", String(16)),
    Column(
        "automate_proposal_materialization",
        Boolean,
        nullable=False,
        server_default="false",
    ),
    Column("environment", String(16), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["response_run_id", "ticket_id", "response_handoff_hash"],
        ["rfp_response_handoffs.run_id", "rfp_response_handoffs.ticket_id", "rfp_response_handoffs.payload_hash"],
        name="fk_rfp_approval_source_handoff",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["started_by_binding_id", "started_by_principal_id", "environment"],
        ["rfp_identity_bindings.binding_id", "rfp_identity_bindings.principal_id", "rfp_identity_bindings.environment"],
        name="fk_rfp_approval_starter_binding",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["start_authorization_id", "started_by_principal_id", "environment", "authorization_version"],
        ["rfp_authorizations.authorization_id", "rfp_authorizations.principal_id", "rfp_authorizations.environment", "rfp_authorizations.authorization_version"],
        name="fk_rfp_approval_start_authorization",
        ondelete="RESTRICT",
    ),
    CheckConstraint("contract_version='rfp-approval-start-v1'", name="ck_rfp_approval_contract"),
    CheckConstraint("response_handoff_version='rfp-response-v1'", name="ck_rfp_approval_handoff_version"),
    CheckConstraint("response_handoff_hash ~ '^[a-f0-9]{64}$'", name="ck_rfp_approval_handoff_hash"),
    CheckConstraint("max_review_revisions=3", name="ck_rfp_approval_max_revisions"),
    CheckConstraint("status IN ('active','blocked','finalizing','completed','failed')", name="ck_rfp_approval_status"),
    CheckConstraint("state_version>=0", name="ck_rfp_approval_state_version"),
    CheckConstraint(
        "started_by_general_role IS NULL OR "
        "started_by_general_role IN ('admin','manager','user')",
        name="ck_rfp_approval_starter_general_role",
    ),
    CheckConstraint(
        "NOT automate_proposal_materialization OR "
        "started_by_general_role IS NOT NULL",
        name="ck_rfp_approval_automation_delegation",
    ),
    CheckConstraint(
        "(status='completed' AND completed_at IS NOT NULL) OR "
        "(status<>'completed' AND completed_at IS NULL)",
        name="ck_rfp_approval_run_completion",
    ),
    UniqueConstraint("approval_run_id", "ticket_id", "response_run_id", name="uq_rfp_approval_run_ticket_response"),
    UniqueConstraint("approval_run_id", "ticket_id", name="uq_rfp_approval_run_ticket"),
)
Index("uq_rfp_approval_response_run", rfp_approval_runs.c.response_run_id, unique=True)
Index(
    "uq_rfp_approval_active_ticket",
    rfp_approval_runs.c.ticket_id,
    unique=True,
    postgresql_where=rfp_approval_runs.c.status.in_(["active", "blocked", "finalizing"]),
)
Index("ix_rfp_approval_ticket_created", rfp_approval_runs.c.ticket_id, rfp_approval_runs.c.created_at)

rfp_approval_corrected_drafts = Table(
    "rfp_approval_corrected_drafts",
    rfp_metadata_registry,
    Column("draft_artifact_id", UUID(as_uuid=True), primary_key=True),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("scope_key", String(32), nullable=False),
    Column("contract_version", String(64), nullable=False),
    Column("content_payload", JSONB, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("provenance", JSONB, nullable=False),
    Column("provenance_hash", String(64), nullable=False),
    Column("producer_node", Text, nullable=False),
    Column("execution_id", UUID(as_uuid=True), nullable=False),
    Column("produced_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["approval_run_id", "ticket_id"],
        ["rfp_approval_runs.approval_run_id", "rfp_approval_runs.ticket_id"],
        name="fk_rfp_corrected_draft_run",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "scope_key IN ('warehouse','lastmile','reverse','proposal')",
        name="ck_rfp_corrected_draft_scope",
    ),
    CheckConstraint(
        "contract_version='rfp-approval-corrected-draft-v1'",
        name="ck_rfp_corrected_draft_contract",
    ),
    CheckConstraint(
        "length(btrim(producer_node))>0",
        name="ck_rfp_corrected_draft_producer",
    ),
    CheckConstraint(
        "jsonb_typeof(content_payload)='object' AND jsonb_typeof(provenance)='object'",
        name="ck_rfp_corrected_draft_json",
    ),
    CheckConstraint(
        "content_hash ~ '^[a-f0-9]{64}$' AND payload_hash ~ '^[a-f0-9]{64}$' "
        "AND provenance_hash ~ '^[a-f0-9]{64}$'",
        name="ck_rfp_corrected_draft_hashes",
    ),
    CheckConstraint(
        "created_at>=produced_at", name="ck_rfp_corrected_draft_time"
    ),
    UniqueConstraint(
        "draft_artifact_id", "approval_run_id", "ticket_id", "scope_key",
        "content_hash", name="uq_rfp_corrected_draft_identity",
    ),
)
Index(
    "ix_rfp_corrected_draft_run_scope",
    rfp_approval_corrected_drafts.c.approval_run_id,
    rfp_approval_corrected_drafts.c.scope_key,
    rfp_approval_corrected_drafts.c.created_at,
)

rfp_approval_validation_artifacts = Table(
    "rfp_approval_validation_artifacts",
    rfp_metadata_registry,
    Column("artifact_id", UUID(as_uuid=True), primary_key=True),
    Column("artifact_type", String(32), nullable=False),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("scope_key", String(32), nullable=False),
    Column("corrected_draft_id", UUID(as_uuid=True), nullable=False),
    Column("evaluated_content_hash", String(64), nullable=False),
    Column("contract_version", String(100), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("provenance", JSONB, nullable=False),
    Column("provenance_hash", String(64), nullable=False),
    Column("producer_node", Text, nullable=False),
    Column("execution_id", UUID(as_uuid=True), nullable=False),
    Column("produced_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        [
            "corrected_draft_id", "approval_run_id", "ticket_id", "scope_key",
            "evaluated_content_hash",
        ],
        [
            "rfp_approval_corrected_drafts.draft_artifact_id",
            "rfp_approval_corrected_drafts.approval_run_id",
            "rfp_approval_corrected_drafts.ticket_id",
            "rfp_approval_corrected_drafts.scope_key",
            "rfp_approval_corrected_drafts.content_hash",
        ],
        name="fk_rfp_validation_artifact_draft",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "artifact_type IN ('evaluation','global_evaluation','safe_preview')",
        name="ck_rfp_validation_artifact_type",
    ),
    CheckConstraint(
        "scope_key IN ('warehouse','lastmile','reverse','proposal')",
        name="ck_rfp_validation_artifact_scope",
    ),
    CheckConstraint(
        "(artifact_type='global_evaluation' AND scope_key='proposal') OR "
        "(artifact_type='evaluation' AND scope_key<>'proposal') OR "
        "artifact_type='safe_preview'",
        name="ck_rfp_validation_artifact_scope_type",
    ),
    CheckConstraint(
        "jsonb_typeof(payload)='object' AND jsonb_typeof(provenance)='object'",
        name="ck_rfp_validation_artifact_json",
    ),
    CheckConstraint(
        "length(btrim(contract_version))>0 AND length(btrim(producer_node))>0",
        name="ck_rfp_validation_artifact_producer_contract",
    ),
    CheckConstraint(
        "evaluated_content_hash ~ '^[a-f0-9]{64}$' AND "
        "payload_hash ~ '^[a-f0-9]{64}$' AND "
        "provenance_hash ~ '^[a-f0-9]{64}$'",
        name="ck_rfp_validation_artifact_hashes",
    ),
    CheckConstraint(
        "created_at>=produced_at", name="ck_rfp_validation_artifact_time"
    ),
    UniqueConstraint(
        "artifact_id", "approval_run_id", "ticket_id", "scope_key",
        "corrected_draft_id", "evaluated_content_hash", "artifact_type",
        "contract_version", "payload_hash",
        name="uq_rfp_validation_artifact_identity",
    ),
)
Index(
    "ix_rfp_validation_artifact_draft_type",
    rfp_approval_validation_artifacts.c.corrected_draft_id,
    rfp_approval_validation_artifacts.c.artifact_type,
    rfp_approval_validation_artifacts.c.created_at,
)

rfp_approval_validation_bundles = Table(
    "rfp_approval_validation_bundles",
    rfp_metadata_registry,
    Column("validation_bundle_id", UUID(as_uuid=True), primary_key=True),
    Column("contract_version", String(64), nullable=False),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("scope_key", String(32), nullable=False),
    Column("corrected_draft_id", UUID(as_uuid=True), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("evaluation_artifact_id", UUID(as_uuid=True), nullable=False),
    Column("evaluation_artifact_type", String(32), nullable=False),
    Column("evaluation_contract_version", String(100), nullable=False),
    Column("evaluation_payload_hash", String(64), nullable=False),
    Column("safe_preview_artifact_id", UUID(as_uuid=True), nullable=False),
    Column(
        "safe_preview_artifact_type", String(32), nullable=False,
        server_default="safe_preview",
    ),
    Column("safe_preview_contract_version", String(100), nullable=False),
    Column("safe_preview_payload_hash", String(64), nullable=False),
    Column("revision_validation_hash", String(64), nullable=False),
    Column("mandatory_compliance_passed", Boolean, nullable=False),
    Column("blocks_commercial_completeness", Boolean, nullable=False),
    Column("blocking_rule_ids", JSONB, nullable=False),
    Column("unresolved_input_codes", JSONB, nullable=False),
    Column("open_conflict_ids", JSONB, nullable=False),
    Column("source_departments_approved", Boolean, nullable=False),
    Column("eligible", Boolean, nullable=False),
    Column("provenance", JSONB, nullable=False),
    Column("provenance_hash", String(64), nullable=False),
    Column("bundle_hash", String(64), nullable=False),
    Column("validated_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        [
            "corrected_draft_id", "approval_run_id", "ticket_id", "scope_key",
            "content_hash",
        ],
        [
            "rfp_approval_corrected_drafts.draft_artifact_id",
            "rfp_approval_corrected_drafts.approval_run_id",
            "rfp_approval_corrected_drafts.ticket_id",
            "rfp_approval_corrected_drafts.scope_key",
            "rfp_approval_corrected_drafts.content_hash",
        ],
        name="fk_rfp_validation_bundle_draft",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        [
            "evaluation_artifact_id", "approval_run_id", "ticket_id", "scope_key",
            "corrected_draft_id", "content_hash", "evaluation_artifact_type",
            "evaluation_contract_version", "evaluation_payload_hash",
        ],
        [
            "rfp_approval_validation_artifacts.artifact_id",
            "rfp_approval_validation_artifacts.approval_run_id",
            "rfp_approval_validation_artifacts.ticket_id",
            "rfp_approval_validation_artifacts.scope_key",
            "rfp_approval_validation_artifacts.corrected_draft_id",
            "rfp_approval_validation_artifacts.evaluated_content_hash",
            "rfp_approval_validation_artifacts.artifact_type",
            "rfp_approval_validation_artifacts.contract_version",
            "rfp_approval_validation_artifacts.payload_hash",
        ],
        name="fk_rfp_validation_bundle_evaluation",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        [
            "safe_preview_artifact_id", "approval_run_id", "ticket_id", "scope_key",
            "corrected_draft_id", "content_hash", "safe_preview_artifact_type",
            "safe_preview_contract_version", "safe_preview_payload_hash",
        ],
        [
            "rfp_approval_validation_artifacts.artifact_id",
            "rfp_approval_validation_artifacts.approval_run_id",
            "rfp_approval_validation_artifacts.ticket_id",
            "rfp_approval_validation_artifacts.scope_key",
            "rfp_approval_validation_artifacts.corrected_draft_id",
            "rfp_approval_validation_artifacts.evaluated_content_hash",
            "rfp_approval_validation_artifacts.artifact_type",
            "rfp_approval_validation_artifacts.contract_version",
            "rfp_approval_validation_artifacts.payload_hash",
        ],
        name="fk_rfp_validation_bundle_preview",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "scope_key IN ('warehouse','lastmile','reverse','proposal')",
        name="ck_rfp_validation_bundle_scope",
    ),
    CheckConstraint(
        "contract_version='rfp-approval-validation-bundle-v1'",
        name="ck_rfp_validation_bundle_contract",
    ),
    CheckConstraint(
        "evaluation_artifact_type IN ('evaluation','global_evaluation') AND "
        "safe_preview_artifact_type='safe_preview'",
        name="ck_rfp_validation_bundle_types",
    ),
    CheckConstraint(
        "length(btrim(evaluation_contract_version))>0 AND "
        "length(btrim(safe_preview_contract_version))>0",
        name="ck_rfp_validation_bundle_contract_versions",
    ),
    CheckConstraint(
        "(scope_key='proposal' AND evaluation_artifact_type='global_evaluation') OR "
        "(scope_key<>'proposal' AND evaluation_artifact_type='evaluation')",
        name="ck_rfp_validation_bundle_scope_type",
    ),
    CheckConstraint(
        "jsonb_typeof(blocking_rule_ids)='array' AND "
        "jsonb_typeof(unresolved_input_codes)='array' AND "
        "jsonb_typeof(open_conflict_ids)='array' AND "
        "jsonb_typeof(provenance)='object'",
        name="ck_rfp_validation_bundle_json",
    ),
    CheckConstraint(
        "content_hash ~ '^[a-f0-9]{64}$' AND "
        "evaluation_payload_hash ~ '^[a-f0-9]{64}$' AND "
        "safe_preview_payload_hash ~ '^[a-f0-9]{64}$' AND "
        "revision_validation_hash ~ '^[a-f0-9]{64}$' AND "
        "provenance_hash ~ '^[a-f0-9]{64}$' AND "
        "bundle_hash ~ '^[a-f0-9]{64}$'",
        name="ck_rfp_validation_bundle_hashes",
    ),
    CheckConstraint(
        "eligible=((scope_key<>'proposal' OR source_departments_approved) "
        "AND mandatory_compliance_passed "
        "AND jsonb_array_length(blocking_rule_ids)=0 "
        "AND jsonb_array_length(open_conflict_ids)=0 "
        "AND ((NOT blocks_commercial_completeness "
        "AND jsonb_array_length(unresolved_input_codes)=0) OR "
        "(blocks_commercial_completeness "
        "AND jsonb_array_length(unresolved_input_codes)>0 "
        "AND unresolved_input_codes <@ "
        "'[\"MONTHLY_STORAGE_VOLUME_REQUIRED\",\"LAST_MILE_RATE_REQUIRED\","
        "\"RETURN_PROCESSING_RATE_REQUIRED\","
        "\"VOLUME_DISCOUNT_APPROVAL_REQUIRED\"]'::jsonb "
        "AND jsonb_array_length(unresolved_input_codes)=("
        "(CASE WHEN unresolved_input_codes ? "
        "'MONTHLY_STORAGE_VOLUME_REQUIRED' THEN 1 ELSE 0 END)+"
        "(CASE WHEN unresolved_input_codes ? "
        "'LAST_MILE_RATE_REQUIRED' THEN 1 ELSE 0 END)+"
        "(CASE WHEN unresolved_input_codes ? "
        "'RETURN_PROCESSING_RATE_REQUIRED' THEN 1 ELSE 0 END)+"
        "(CASE WHEN unresolved_input_codes ? "
        "'VOLUME_DISCOUNT_APPROVAL_REQUIRED' THEN 1 ELSE 0 END)"
        ")))) AND blocks_commercial_completeness="
        "(jsonb_array_length(unresolved_input_codes)>0)",
        name="ck_rfp_validation_bundle_eligibility",
    ),
    CheckConstraint(
        "created_at>=validated_at", name="ck_rfp_validation_bundle_time"
    ),
    UniqueConstraint(
        "validation_bundle_id", "approval_run_id", "ticket_id", "scope_key",
        "content_hash", "revision_validation_hash",
        name="uq_rfp_validation_bundle_identity",
    ),
)
Index(
    "ix_rfp_validation_bundle_run_scope",
    rfp_approval_validation_bundles.c.approval_run_id,
    rfp_approval_validation_bundles.c.scope_key,
    rfp_approval_validation_bundles.c.validated_at,
)

rfp_approval_department_revisions = Table(
    "rfp_approval_department_revisions",
    rfp_metadata_registry,
    Column("revision_id", UUID(as_uuid=True), primary_key=True),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("response_run_id", UUID(as_uuid=True), nullable=False),
    Column("department_id", String(32), nullable=False),
    Column("review_revision_no", Integer, nullable=False),
    Column("source_part2_draft_id", UUID(as_uuid=True), nullable=False),
    Column("source_part2_draft_hash", String(64), nullable=False),
    Column("source_part2_evaluation_id", UUID(as_uuid=True), nullable=False),
    Column("source_part2_evaluation_hash", String(64), nullable=False),
    Column("source_part2_iteration", Integer, nullable=False),
    Column("supersedes_revision_id", UUID(as_uuid=True)),
    Column("superseded_by_revision_id", UUID(as_uuid=True)),
    Column("content_payload", JSONB, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("validation_payload", JSONB, nullable=False),
    Column("validation_hash", String(64), nullable=False),
    Column("evaluated_content_hash", String(64), nullable=False),
    Column("validation_bundle_id", UUID(as_uuid=True)),
    Column("status", String(32), nullable=False),
    Column("is_current", Boolean, nullable=False, server_default="true"),
    Column("state_version", Integer, nullable=False, server_default="0"),
    Column("applied_decision_id", UUID(as_uuid=True)),
    Column("created_by_principal_id", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["approval_run_id", "ticket_id", "response_run_id"],
        ["rfp_approval_runs.approval_run_id", "rfp_approval_runs.ticket_id", "rfp_approval_runs.response_run_id"],
        name="fk_rfp_department_revision_run",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["source_part2_draft_id", "response_run_id", "department_id"],
        ["rfp_response_drafts.draft_id", "rfp_response_drafts.run_id", "rfp_response_drafts.scope_key"],
        name="fk_rfp_department_revision_source_draft",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["source_part2_evaluation_id", "response_run_id", "source_part2_draft_id", "department_id"],
        ["rfp_response_evaluations.evaluation_id", "rfp_response_evaluations.run_id", "rfp_response_evaluations.draft_id", "rfp_response_evaluations.scope_key"],
        name="fk_rfp_department_revision_source_evaluation",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        [
            "validation_bundle_id", "approval_run_id", "ticket_id",
            "department_id", "content_hash", "validation_hash",
        ],
        [
            "rfp_approval_validation_bundles.validation_bundle_id",
            "rfp_approval_validation_bundles.approval_run_id",
            "rfp_approval_validation_bundles.ticket_id",
            "rfp_approval_validation_bundles.scope_key",
            "rfp_approval_validation_bundles.content_hash",
            "rfp_approval_validation_bundles.revision_validation_hash",
        ],
        name="fk_rfp_department_revision_validation_bundle",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["created_by_principal_id"],
        ["rfp_principals.principal_id"],
        name="fk_rfp_department_revision_creator",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["supersedes_revision_id", "approval_run_id", "ticket_id", "department_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id", "rfp_approval_department_revisions.department_id"],
        name="fk_rfp_department_revision_predecessor",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["superseded_by_revision_id", "approval_run_id", "ticket_id", "department_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id", "rfp_approval_department_revisions.department_id"],
        name="fk_rfp_department_revision_successor",
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ["applied_decision_id", "revision_id", "approval_run_id", "ticket_id"],
        ["rfp_human_decisions.decision_id", "rfp_human_decisions.revision_id", "rfp_human_decisions.approval_run_id", "rfp_human_decisions.ticket_id"],
        name="fk_rfp_department_applied_decision",
        ondelete="RESTRICT",
    ),
    CheckConstraint("department_id IN ('warehouse','lastmile','reverse')", name="ck_rfp_department_revision_department"),
    CheckConstraint("review_revision_no BETWEEN 1 AND 3", name="ck_rfp_department_revision_number"),
    CheckConstraint("source_part2_iteration BETWEEN 1 AND 3", name="ck_rfp_department_revision_source_iteration"),
    CheckConstraint(
        "status IN ('pending_assessment','blocked_input','blocked_compliance','blocked_conflict',"
        "'pending_approval','approved',"
        "'rejected','changes_requested','superseded','limit_reached')",
        name="ck_rfp_department_revision_status",
    ),
    CheckConstraint(
        "status<>'blocked_conflict' OR (("
        "jsonb_typeof(validation_payload->'eligible_for_approval')='boolean' AND "
        "validation_payload->>'eligible_for_approval'='false' AND "
        "jsonb_typeof(validation_payload->'mandatory_compliance_passed')='boolean' AND "
        "validation_payload->>'mandatory_compliance_passed'='true' AND "
        "jsonb_typeof(validation_payload->'blocks_commercial_completeness')='boolean' AND "
        "validation_payload->>'blocks_commercial_completeness'='false' AND "
        "jsonb_typeof(validation_payload->'open_conflict_ids')='array' AND "
        "jsonb_array_length(validation_payload->'open_conflict_ids')>0 AND "
        "jsonb_typeof(validation_payload->'blocking_rule_ids')='array' AND "
        "jsonb_array_length(validation_payload->'blocking_rule_ids')=0 AND "
        "jsonb_typeof(validation_payload->'unresolved_input_codes')='array' AND "
        "jsonb_array_length(validation_payload->'unresolved_input_codes')=0 AND "
        "jsonb_typeof(content_payload->'pending_commercial_items')='array' AND "
        "jsonb_array_length(content_payload->'pending_commercial_items')=0) IS TRUE)",
        name="ck_rfp_department_revision_blocked_conflict",
    ),
    CheckConstraint("evaluated_content_hash=content_hash", name="ck_rfp_department_revision_hash_binding"),
    CheckConstraint(
        "revision_id IS DISTINCT FROM supersedes_revision_id AND "
        "revision_id IS DISTINCT FROM superseded_by_revision_id AND "
        "((review_revision_no=1 AND supersedes_revision_id IS NULL) OR "
        "(review_revision_no>1 AND supersedes_revision_id IS NOT NULL))",
        name="ck_rfp_department_revision_lineage",
    ),
    CheckConstraint(
        "(status='superseded' AND NOT is_current AND superseded_by_revision_id IS NOT NULL) OR "
        "(status<>'superseded' AND is_current AND superseded_by_revision_id IS NULL)",
        name="ck_rfp_department_revision_current",
    ),
    CheckConstraint("status<>'limit_reached' OR review_revision_no=3", name="ck_rfp_department_revision_limit"),
    UniqueConstraint("approval_run_id", "department_id", "review_revision_no", name="uq_rfp_department_revision_number"),
    UniqueConstraint("revision_id", "approval_run_id", "ticket_id", "department_id", name="uq_rfp_department_revision_identity"),
    UniqueConstraint("revision_id", "approval_run_id", "ticket_id", name="uq_rfp_department_revision_run_identity"),
    UniqueConstraint("supersedes_revision_id", name="uq_rfp_department_revision_successor"),
)
Index(
    "uq_rfp_department_current",
    rfp_approval_department_revisions.c.approval_run_id,
    rfp_approval_department_revisions.c.department_id,
    unique=True,
    postgresql_where=rfp_approval_department_revisions.c.is_current,
)
Index(
    "ix_rfp_department_revision_run",
    rfp_approval_department_revisions.c.approval_run_id,
    rfp_approval_department_revisions.c.department_id,
    rfp_approval_department_revisions.c.created_at,
)

rfp_approval_proposal_revisions = Table(
    "rfp_approval_proposal_revisions",
    rfp_metadata_registry,
    Column("proposal_revision_id", UUID(as_uuid=True), primary_key=True),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("response_run_id", UUID(as_uuid=True), nullable=False),
    Column("revision_no", Integer, nullable=False),
    Column("source_global_draft_id", UUID(as_uuid=True), nullable=False),
    Column("source_global_draft_hash", String(64), nullable=False),
    Column("source_global_evaluation_id", UUID(as_uuid=True), nullable=False),
    Column("source_global_evaluation_hash", String(64), nullable=False),
    Column("source_scope_key", Text, Computed("'proposal'", persisted=True)),
    Column("supersedes_revision_id", UUID(as_uuid=True)),
    Column("superseded_by_revision_id", UUID(as_uuid=True)),
    Column("warehouse_revision_id", UUID(as_uuid=True)),
    Column("warehouse_department_id", Text, Computed("'warehouse'", persisted=True)),
    Column("lastmile_revision_id", UUID(as_uuid=True)),
    Column("lastmile_department_id", Text, Computed("'lastmile'", persisted=True)),
    Column("reverse_revision_id", UUID(as_uuid=True)),
    Column("reverse_department_id", Text, Computed("'reverse'", persisted=True)),
    Column("content_payload", JSONB, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("validation_payload", JSONB, nullable=False),
    Column("validation_hash", String(64), nullable=False),
    Column("evaluated_content_hash", String(64), nullable=False),
    Column("validation_bundle_id", UUID(as_uuid=True)),
    Column("eligible_for_synthesis", Boolean, nullable=False),
    Column("is_current", Boolean, nullable=False, server_default="true"),
    Column("state_version", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["approval_run_id", "ticket_id", "response_run_id"],
        ["rfp_approval_runs.approval_run_id", "rfp_approval_runs.ticket_id", "rfp_approval_runs.response_run_id"],
        name="fk_rfp_proposal_run",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["source_global_draft_id", "response_run_id", "source_scope_key"],
        ["rfp_response_drafts.draft_id", "rfp_response_drafts.run_id", "rfp_response_drafts.scope_key"],
        name="fk_rfp_proposal_source_draft",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["source_global_evaluation_id", "response_run_id", "source_global_draft_id", "source_scope_key"],
        ["rfp_response_evaluations.evaluation_id", "rfp_response_evaluations.run_id", "rfp_response_evaluations.draft_id", "rfp_response_evaluations.scope_key"],
        name="fk_rfp_proposal_source_evaluation",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        [
            "validation_bundle_id", "approval_run_id", "ticket_id",
            "source_scope_key", "content_hash", "validation_hash",
        ],
        [
            "rfp_approval_validation_bundles.validation_bundle_id",
            "rfp_approval_validation_bundles.approval_run_id",
            "rfp_approval_validation_bundles.ticket_id",
            "rfp_approval_validation_bundles.scope_key",
            "rfp_approval_validation_bundles.content_hash",
            "rfp_approval_validation_bundles.revision_validation_hash",
        ],
        name="fk_rfp_proposal_revision_validation_bundle",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["warehouse_revision_id", "approval_run_id", "ticket_id", "warehouse_department_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id", "rfp_approval_department_revisions.department_id"],
        name="fk_rfp_proposal_warehouse_revision",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["lastmile_revision_id", "approval_run_id", "ticket_id", "lastmile_department_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id", "rfp_approval_department_revisions.department_id"],
        name="fk_rfp_proposal_lastmile_revision",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["reverse_revision_id", "approval_run_id", "ticket_id", "reverse_department_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id", "rfp_approval_department_revisions.department_id"],
        name="fk_rfp_proposal_reverse_revision",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["supersedes_revision_id", "approval_run_id", "ticket_id"],
        ["rfp_approval_proposal_revisions.proposal_revision_id", "rfp_approval_proposal_revisions.approval_run_id", "rfp_approval_proposal_revisions.ticket_id"],
        name="fk_rfp_proposal_predecessor",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["superseded_by_revision_id", "approval_run_id", "ticket_id"],
        ["rfp_approval_proposal_revisions.proposal_revision_id", "rfp_approval_proposal_revisions.approval_run_id", "rfp_approval_proposal_revisions.ticket_id"],
        name="fk_rfp_proposal_successor",
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    ),
    CheckConstraint("revision_no BETWEEN 1 AND 3", name="ck_rfp_proposal_revision_number"),
    CheckConstraint("num_nonnulls(warehouse_revision_id,lastmile_revision_id,reverse_revision_id)>=1", name="ck_rfp_proposal_has_department"),
    CheckConstraint("evaluated_content_hash=content_hash", name="ck_rfp_proposal_hash_binding"),
    CheckConstraint(
        "proposal_revision_id IS DISTINCT FROM supersedes_revision_id AND "
        "proposal_revision_id IS DISTINCT FROM superseded_by_revision_id AND "
        "((revision_no=1 AND supersedes_revision_id IS NULL) OR "
        "(revision_no>1 AND supersedes_revision_id IS NOT NULL))",
        name="ck_rfp_proposal_lineage",
    ),
    CheckConstraint(
        "(NOT is_current AND superseded_by_revision_id IS NOT NULL) OR "
        "(is_current AND superseded_by_revision_id IS NULL)",
        name="ck_rfp_proposal_current",
    ),
    UniqueConstraint("approval_run_id", "revision_no", name="uq_rfp_proposal_revision_number"),
    UniqueConstraint("proposal_revision_id", "approval_run_id", "ticket_id", name="uq_rfp_proposal_revision_identity"),
    UniqueConstraint("supersedes_revision_id", name="uq_rfp_proposal_successor"),
)
Index(
    "uq_rfp_proposal_current",
    rfp_approval_proposal_revisions.c.approval_run_id,
    unique=True,
    postgresql_where=rfp_approval_proposal_revisions.c.is_current,
)

rfp_human_decisions = Table(
    "rfp_human_decisions",
    rfp_metadata_registry,
    Column("decision_id", UUID(as_uuid=True), primary_key=True),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("revision_id", UUID(as_uuid=True), nullable=False),
    Column("department_id", String(32), nullable=False),
    Column("idempotency_key", UUID(as_uuid=True), nullable=False),
    Column("application_key", String(79), nullable=False),
    Column("expected_content_hash", String(64), nullable=False),
    Column("action", String(24), nullable=False),
    Column("reason", Text),
    Column("processing_status", String(24), nullable=False, server_default="queued"),
    Column("actor_principal_id", UUID(as_uuid=True), nullable=False),
    Column("actor_binding_id", UUID(as_uuid=True), nullable=False),
    Column("authorization_id", UUID(as_uuid=True), nullable=False),
    Column("authorization_version", Integer, nullable=False),
    Column("environment", String(16), nullable=False),
    Column("claim_token", UUID(as_uuid=True)),
    Column("claim_epoch", Integer, nullable=False, server_default="0"),
    Column("claimed_at", DateTime(timezone=True)),
    Column("lease_until", DateTime(timezone=True)),
    Column("decided_at", DateTime(timezone=True), nullable=False),
    Column("applied_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["revision_id", "approval_run_id", "ticket_id", "department_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id", "rfp_approval_department_revisions.department_id"],
        name="fk_rfp_decision_revision",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["actor_binding_id", "actor_principal_id", "environment"],
        ["rfp_identity_bindings.binding_id", "rfp_identity_bindings.principal_id", "rfp_identity_bindings.environment"],
        name="fk_rfp_decision_actor_binding",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["authorization_id", "actor_principal_id", "environment", "authorization_version"],
        ["rfp_authorizations.authorization_id", "rfp_authorizations.principal_id", "rfp_authorizations.environment", "rfp_authorizations.authorization_version"],
        name="fk_rfp_decision_authorization",
        ondelete="RESTRICT",
    ),
    CheckConstraint("department_id IN ('warehouse','lastmile','reverse')", name="ck_rfp_decision_department"),
    CheckConstraint("application_key ~ '^decision-apply:[a-f0-9]{64}$'", name="ck_rfp_decision_application_key"),
    CheckConstraint("expected_content_hash ~ '^[a-f0-9]{64}$'", name="ck_rfp_decision_expected_hash"),
    CheckConstraint("action IN ('approve','reject','request_changes')", name="ck_rfp_decision_action"),
    CheckConstraint("processing_status IN ('queued','claimed','applied','rejected_stale')", name="ck_rfp_decision_processing_status"),
    CheckConstraint("action='approve' OR (reason IS NOT NULL AND length(btrim(reason))>0)", name="ck_rfp_decision_reason"),
    CheckConstraint(
        "(processing_status='queued' AND claim_token IS NULL AND claimed_at IS NULL AND lease_until IS NULL AND applied_at IS NULL) OR "
        "(processing_status='claimed' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL AND lease_until>claimed_at AND applied_at IS NULL) OR "
        "(processing_status IN ('applied','rejected_stale') AND applied_at IS NOT NULL)",
        name="ck_rfp_decision_claim_shape",
    ),
    UniqueConstraint("revision_id", name="uq_rfp_decision_revision"),
    UniqueConstraint("approval_run_id", "idempotency_key", name="uq_rfp_decision_idempotency"),
    UniqueConstraint("application_key", name="uq_rfp_decision_application"),
    UniqueConstraint("decision_id", "revision_id", "approval_run_id", "ticket_id", name="uq_rfp_decision_revision_identity"),
    UniqueConstraint("decision_id", "approval_run_id", "ticket_id", name="uq_rfp_decision_run_identity"),
)
Index("ix_rfp_decision_queue", rfp_human_decisions.c.processing_status, rfp_human_decisions.c.lease_until, rfp_human_decisions.c.created_at)

rfp_authorized_business_inputs = Table(
    "rfp_authorized_business_inputs",
    rfp_metadata_registry,
    Column("input_id", UUID(as_uuid=True), primary_key=True),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("input_kind", String(40), nullable=False),
    Column("department_scope", String(32)),
    Column("payload", JSONB, nullable=False),
    Column("provenance", JSONB, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("provenance_hash", String(64), nullable=False),
    Column("evidence_hash", String(64), nullable=False),
    Column("idempotency_key", UUID(as_uuid=True), nullable=False),
    Column("actor_principal_id", UUID(as_uuid=True), nullable=False),
    Column("actor_binding_id", UUID(as_uuid=True), nullable=False),
    Column("authorization_id", UUID(as_uuid=True), nullable=False),
    Column("authorization_version", Integer, nullable=False),
    Column("environment", String(16), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["approval_run_id", "ticket_id"],
        ["rfp_approval_runs.approval_run_id", "rfp_approval_runs.ticket_id"],
        name="fk_rfp_business_input_run",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["actor_binding_id", "actor_principal_id", "environment"],
        ["rfp_identity_bindings.binding_id", "rfp_identity_bindings.principal_id", "rfp_identity_bindings.environment"],
        name="fk_rfp_business_input_actor",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["authorization_id", "actor_principal_id", "environment", "authorization_version"],
        ["rfp_authorizations.authorization_id", "rfp_authorizations.principal_id", "rfp_authorizations.environment", "rfp_authorizations.authorization_version"],
        name="fk_rfp_business_input_authorization",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "input_kind IN ('client_price_quote','warehouse_capacity','operational_scope',"
        "'currency_quote','currency_conversion','global_commercial_term','volume_discount',"
        "'monthly_storage_volume')",
        name="ck_rfp_business_input_kind",
    ),
    CheckConstraint("department_scope IS NULL OR department_scope IN ('warehouse','lastmile','reverse')", name="ck_rfp_business_input_department"),
    CheckConstraint("jsonb_typeof(payload)='object' AND jsonb_typeof(provenance)='object'", name="ck_rfp_business_input_json"),
    CheckConstraint("payload_hash ~ '^[a-f0-9]{64}$' AND provenance_hash ~ '^[a-f0-9]{64}$' AND evidence_hash ~ '^[a-f0-9]{64}$'", name="ck_rfp_business_input_hashes"),
    UniqueConstraint("approval_run_id", "idempotency_key", name="uq_rfp_business_input_idempotency"),
    UniqueConstraint("input_id", "approval_run_id", "ticket_id", name="uq_rfp_business_input_identity"),
)
Index("ix_rfp_business_input_run_kind", rfp_authorized_business_inputs.c.approval_run_id, rfp_authorized_business_inputs.c.input_kind, rfp_authorized_business_inputs.c.recorded_at)

rfp_conflict_assessments = Table(
    "rfp_conflict_assessments",
    rfp_metadata_registry,
    Column("assessment_id", UUID(as_uuid=True), primary_key=True),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("trigger_id", String(32), nullable=False),
    Column("outcome", String(32), nullable=False),
    Column("facts_payload", JSONB, nullable=False),
    Column("missing_input_codes", JSONB, nullable=False),
    Column("assessed_content_hashes", JSONB, nullable=False),
    Column("assessment_hash", String(64), nullable=False),
    Column("assessed_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["approval_run_id", "ticket_id"],
        ["rfp_approval_runs.approval_run_id", "rfp_approval_runs.ticket_id"],
        name="fk_rfp_assessment_run",
        ondelete="RESTRICT",
    ),
    CheckConstraint("trigger_id IN ('volume-vs-capacity','returns-sla-breach','currency-mismatch')", name="ck_rfp_assessment_trigger"),
    CheckConstraint("outcome IN ('demonstrated','insufficient_information','no_conflict')", name="ck_rfp_assessment_outcome"),
    CheckConstraint(
        "(outcome='insufficient_information' AND jsonb_array_length(missing_input_codes)>0) OR "
        "(outcome<>'insufficient_information' AND jsonb_array_length(missing_input_codes)=0)",
        name="ck_rfp_assessment_missing_shape",
    ),
    UniqueConstraint("approval_run_id", "assessment_hash", name="uq_rfp_assessment_hash"),
    UniqueConstraint(
        "assessment_id", "approval_run_id", "ticket_id", "trigger_id", "outcome",
        name="uq_rfp_assessment_identity",
    ),
)

rfp_arbitration_conflicts = Table(
    "rfp_arbitration_conflicts",
    rfp_metadata_registry,
    Column("conflict_id", UUID(as_uuid=True), primary_key=True),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("assessment_id", UUID(as_uuid=True), nullable=False),
    Column("assessment_outcome", String(32), nullable=False),
    Column("trigger_id", String(32), nullable=False),
    Column("affected_targets", JSONB, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("state_version", Integer, nullable=False, server_default="0"),
    Column("detected_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["assessment_id", "approval_run_id", "ticket_id", "trigger_id", "assessment_outcome"],
        ["rfp_conflict_assessments.assessment_id", "rfp_conflict_assessments.approval_run_id", "rfp_conflict_assessments.ticket_id", "rfp_conflict_assessments.trigger_id", "rfp_conflict_assessments.outcome"],
        name="fk_rfp_conflict_assessment",
        ondelete="RESTRICT",
    ),
    CheckConstraint("assessment_outcome IN ('demonstrated','insufficient_information')", name="ck_rfp_conflict_assessment_outcome"),
    CheckConstraint("trigger_id IN ('volume-vs-capacity','returns-sla-breach','currency-mismatch')", name="ck_rfp_conflict_trigger"),
    CheckConstraint("status IN ('open','changes_requested','resolved','superseded')", name="ck_rfp_conflict_status"),
    CheckConstraint(
        "(status='resolved' AND resolved_at IS NOT NULL) OR (status<>'resolved' AND resolved_at IS NULL)",
        name="ck_rfp_conflict_resolution_shape",
    ),
    UniqueConstraint("approval_run_id", "fingerprint", name="uq_rfp_conflict_fingerprint"),
    UniqueConstraint(
        "conflict_id", "approval_run_id", "ticket_id", "trigger_id", "fingerprint",
        name="uq_rfp_conflict_identity",
    ),
    UniqueConstraint("conflict_id", "approval_run_id", "ticket_id", name="uq_rfp_conflict_run_identity"),
)
Index("ix_rfp_conflict_open", rfp_arbitration_conflicts.c.approval_run_id, rfp_arbitration_conflicts.c.status, rfp_arbitration_conflicts.c.detected_at)

rfp_arbitration_resolutions = Table(
    "rfp_arbitration_resolutions",
    rfp_metadata_registry,
    Column("resolution_id", UUID(as_uuid=True), primary_key=True),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("conflict_id", UUID(as_uuid=True), nullable=False),
    Column("trigger_id", String(32), nullable=False),
    Column("conflict_fingerprint", String(64), nullable=False),
    Column("idempotency_key", UUID(as_uuid=True), nullable=False),
    Column("action", String(24), nullable=False),
    Column("targets", JSONB, nullable=False),
    Column("authorized_input_ids", JSONB, nullable=False),
    Column("reason", Text, nullable=False),
    Column("actor_principal_id", UUID(as_uuid=True), nullable=False),
    Column("actor_binding_id", UUID(as_uuid=True), nullable=False),
    Column("authorization_id", UUID(as_uuid=True), nullable=False),
    Column("authorization_version", Integer, nullable=False),
    Column("environment", String(16), nullable=False),
    Column("post_assessment_id", UUID(as_uuid=True)),
    Column("post_assessment_outcome", String(32)),
    Column("resolution_hash", String(64), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["conflict_id", "approval_run_id", "ticket_id", "trigger_id", "conflict_fingerprint"],
        ["rfp_arbitration_conflicts.conflict_id", "rfp_arbitration_conflicts.approval_run_id", "rfp_arbitration_conflicts.ticket_id", "rfp_arbitration_conflicts.trigger_id", "rfp_arbitration_conflicts.fingerprint"],
        name="fk_rfp_resolution_conflict",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["actor_binding_id", "actor_principal_id", "environment"],
        ["rfp_identity_bindings.binding_id", "rfp_identity_bindings.principal_id", "rfp_identity_bindings.environment"],
        name="fk_rfp_resolution_actor",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["authorization_id", "actor_principal_id", "environment", "authorization_version"],
        ["rfp_authorizations.authorization_id", "rfp_authorizations.principal_id", "rfp_authorizations.environment", "rfp_authorizations.authorization_version"],
        name="fk_rfp_resolution_authorization",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["post_assessment_id", "approval_run_id", "ticket_id", "trigger_id", "post_assessment_outcome"],
        ["rfp_conflict_assessments.assessment_id", "rfp_conflict_assessments.approval_run_id", "rfp_conflict_assessments.ticket_id", "rfp_conflict_assessments.trigger_id", "rfp_conflict_assessments.outcome"],
        name="fk_rfp_resolution_post_assessment",
        ondelete="RESTRICT",
    ),
    CheckConstraint("action IN ('request_changes','accept_resolution')", name="ck_rfp_resolution_action"),
    CheckConstraint(
        "(action='request_changes' AND post_assessment_id IS NULL AND post_assessment_outcome IS NULL) OR "
        "(action='accept_resolution' AND post_assessment_id IS NOT NULL AND post_assessment_outcome='no_conflict')",
        name="ck_rfp_resolution_recheck_shape",
    ),
    UniqueConstraint(
        "approval_run_id", "idempotency_key", name="uq_rfp_resolution_idempotency"
    ),
    UniqueConstraint("conflict_id", "resolution_hash", name="uq_rfp_resolution_material"),
    UniqueConstraint("resolution_id", "approval_run_id", "ticket_id", name="uq_rfp_resolution_identity"),
)
Index(
    "uq_rfp_resolution_accepted_conflict",
    rfp_arbitration_resolutions.c.conflict_id,
    unique=True,
    postgresql_where=rfp_arbitration_resolutions.c.action == "accept_resolution",
)

rfp_arbitration_resolution_evidence = Table(
    "rfp_arbitration_resolution_evidence",
    rfp_metadata_registry,
    Column("evidence_id", UUID(as_uuid=True), primary_key=True),
    Column("resolution_id", UUID(as_uuid=True), nullable=False),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("original_department_revision_id", UUID(as_uuid=True)),
    Column("original_proposal_revision_id", UUID(as_uuid=True)),
    Column("corrected_department_revision_id", UUID(as_uuid=True)),
    Column("corrected_proposal_revision_id", UUID(as_uuid=True)),
    Column("corrected_content_hash", String(64), nullable=False),
    Column("revision_validation_artifact_id", UUID(as_uuid=True), nullable=False),
    Column("revision_validation_hash", String(64), nullable=False),
    Column("conflict_recheck_artifact_id", UUID(as_uuid=True), nullable=False),
    Column("conflict_recheck_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["resolution_id", "approval_run_id", "ticket_id"],
        ["rfp_arbitration_resolutions.resolution_id", "rfp_arbitration_resolutions.approval_run_id", "rfp_arbitration_resolutions.ticket_id"],
        name="fk_rfp_resolution_evidence_resolution",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["original_department_revision_id", "approval_run_id", "ticket_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id"],
        name="fk_rfp_resolution_original_department",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["original_proposal_revision_id", "approval_run_id", "ticket_id"],
        ["rfp_approval_proposal_revisions.proposal_revision_id", "rfp_approval_proposal_revisions.approval_run_id", "rfp_approval_proposal_revisions.ticket_id"],
        name="fk_rfp_resolution_original_proposal",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["corrected_department_revision_id", "approval_run_id", "ticket_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id"],
        name="fk_rfp_resolution_corrected_department",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["corrected_proposal_revision_id", "approval_run_id", "ticket_id"],
        ["rfp_approval_proposal_revisions.proposal_revision_id", "rfp_approval_proposal_revisions.approval_run_id", "rfp_approval_proposal_revisions.ticket_id"],
        name="fk_rfp_resolution_corrected_proposal",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "num_nonnulls(original_department_revision_id,original_proposal_revision_id)=1 AND "
        "num_nonnulls(corrected_department_revision_id,corrected_proposal_revision_id)=1 AND "
        "((original_department_revision_id IS NOT NULL AND corrected_department_revision_id IS NOT NULL) OR "
        "(original_proposal_revision_id IS NOT NULL AND corrected_proposal_revision_id IS NOT NULL))",
        name="ck_rfp_resolution_evidence_kind",
    ),
    CheckConstraint(
        "(original_department_revision_id IS NOT NULL AND "
        "original_department_revision_id<>corrected_department_revision_id) OR "
        "(original_proposal_revision_id IS NOT NULL AND "
        "original_proposal_revision_id<>corrected_proposal_revision_id)",
        name="ck_rfp_resolution_evidence_new_revision",
    ),
)
Index(
    "uq_rfp_resolution_evidence_department",
    rfp_arbitration_resolution_evidence.c.resolution_id,
    rfp_arbitration_resolution_evidence.c.corrected_department_revision_id,
    unique=True,
    postgresql_where=rfp_arbitration_resolution_evidence.c.corrected_department_revision_id.is_not(None),
)
Index(
    "uq_rfp_resolution_evidence_proposal",
    rfp_arbitration_resolution_evidence.c.resolution_id,
    rfp_arbitration_resolution_evidence.c.corrected_proposal_revision_id,
    unique=True,
    postgresql_where=rfp_arbitration_resolution_evidence.c.corrected_proposal_revision_id.is_not(None),
)

rfp_final_documents = Table(
    "rfp_final_documents",
    rfp_metadata_registry,
    Column("final_document_id", UUID(as_uuid=True), primary_key=True),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("proposal_revision_id", UUID(as_uuid=True), nullable=False),
    Column("warehouse_revision_id", UUID(as_uuid=True)),
    Column("warehouse_department_id", Text, Computed("'warehouse'", persisted=True)),
    Column("lastmile_revision_id", UUID(as_uuid=True)),
    Column("lastmile_department_id", Text, Computed("'lastmile'", persisted=True)),
    Column("reverse_revision_id", UUID(as_uuid=True)),
    Column("reverse_department_id", Text, Computed("'reverse'", persisted=True)),
    Column("contract_version", String(64), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("markdown", Text, nullable=False),
    Column("markdown_hash", String(64), nullable=False),
    Column("storage_reference", Text, nullable=False),
    Column("generated_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["approval_run_id", "ticket_id"],
        ["rfp_approval_runs.approval_run_id", "rfp_approval_runs.ticket_id"],
        name="fk_rfp_final_run",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["proposal_revision_id", "approval_run_id", "ticket_id"],
        ["rfp_approval_proposal_revisions.proposal_revision_id", "rfp_approval_proposal_revisions.approval_run_id", "rfp_approval_proposal_revisions.ticket_id"],
        name="fk_rfp_final_proposal",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["warehouse_revision_id", "approval_run_id", "ticket_id", "warehouse_department_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id", "rfp_approval_department_revisions.department_id"],
        name="fk_rfp_final_warehouse",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["lastmile_revision_id", "approval_run_id", "ticket_id", "lastmile_department_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id", "rfp_approval_department_revisions.department_id"],
        name="fk_rfp_final_lastmile",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["reverse_revision_id", "approval_run_id", "ticket_id", "reverse_department_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id", "rfp_approval_department_revisions.department_id"],
        name="fk_rfp_final_reverse",
        ondelete="RESTRICT",
    ),
    CheckConstraint("contract_version='rfp-persisted-final-document-v1'", name="ck_rfp_final_contract"),
    CheckConstraint("jsonb_typeof(payload)='object'", name="ck_rfp_final_payload_json"),
    CheckConstraint("num_nonnulls(warehouse_revision_id,lastmile_revision_id,reverse_revision_id)>=1", name="ck_rfp_final_has_department"),
    UniqueConstraint("approval_run_id", name="uq_rfp_final_run"),
    UniqueConstraint("ticket_id", name="uq_rfp_final_ticket"),
)

# These JSON columns intentionally mirror SQL's shallow top-level denylist.
# They are not a recursive secret filter.  The future store must project only
# allowlisted reference fields and reject all raw or nested business payloads.
rfp_workflow_events = Table(
    "rfp_workflow_events",
    rfp_metadata_registry,
    Column("event_id", UUID(as_uuid=True), primary_key=True),
    Column("event_key", Text, nullable=False, unique=True),
    Column("approval_run_id", UUID(as_uuid=True), nullable=False),
    Column("ticket_id", UUID(as_uuid=True), nullable=False),
    Column("thread_id", Text, nullable=False),
    Column("execution_id", UUID(as_uuid=True), nullable=False),
    Column("sequence_no", BigInteger, nullable=False),
    Column("node_name", Text, nullable=False),
    Column("agent_name", Text),
    Column("event_type", Text, nullable=False),
    Column("safe_input_ref", JSONB, nullable=False),
    Column("safe_output_ref", JSONB, nullable=False),
    Column("result", Text, nullable=False),
    Column("from_state", Text),
    Column("to_state", Text),
    Column("department_revision_id", UUID(as_uuid=True)),
    Column("proposal_revision_id", UUID(as_uuid=True)),
    Column("decision_id", UUID(as_uuid=True)),
    Column("conflict_id", UUID(as_uuid=True)),
    Column("actor_principal_id", UUID(as_uuid=True)),
    Column("actor_binding_id", UUID(as_uuid=True)),
    Column("authorization_id", UUID(as_uuid=True)),
    Column("authorization_version", Integer),
    Column("environment", String(16)),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["approval_run_id", "ticket_id"],
        ["rfp_approval_runs.approval_run_id", "rfp_approval_runs.ticket_id"],
        name="fk_rfp_event_run",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["department_revision_id", "approval_run_id", "ticket_id"],
        ["rfp_approval_department_revisions.revision_id", "rfp_approval_department_revisions.approval_run_id", "rfp_approval_department_revisions.ticket_id"],
        name="fk_rfp_event_department_revision",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["proposal_revision_id", "approval_run_id", "ticket_id"],
        ["rfp_approval_proposal_revisions.proposal_revision_id", "rfp_approval_proposal_revisions.approval_run_id", "rfp_approval_proposal_revisions.ticket_id"],
        name="fk_rfp_event_proposal_revision",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["decision_id", "approval_run_id", "ticket_id"],
        ["rfp_human_decisions.decision_id", "rfp_human_decisions.approval_run_id", "rfp_human_decisions.ticket_id"],
        name="fk_rfp_event_decision",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["conflict_id", "approval_run_id", "ticket_id"],
        ["rfp_arbitration_conflicts.conflict_id", "rfp_arbitration_conflicts.approval_run_id", "rfp_arbitration_conflicts.ticket_id"],
        name="fk_rfp_event_conflict",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["actor_binding_id", "actor_principal_id", "environment"],
        ["rfp_identity_bindings.binding_id", "rfp_identity_bindings.principal_id", "rfp_identity_bindings.environment"],
        name="fk_rfp_event_actor_binding",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["authorization_id", "actor_principal_id", "environment", "authorization_version"],
        ["rfp_authorizations.authorization_id", "rfp_authorizations.principal_id", "rfp_authorizations.environment", "rfp_authorizations.authorization_version"],
        name="fk_rfp_event_authorization",
        ondelete="RESTRICT",
    ),
    CheckConstraint("sequence_no>=1", name="ck_rfp_event_sequence"),
    CheckConstraint("finished_at>=started_at", name="ck_rfp_event_time"),
    CheckConstraint(
        "jsonb_typeof(safe_input_ref)='object' AND "
        "jsonb_typeof(safe_output_ref)='object'",
        name="ck_rfp_event_safe_ref_shape",
    ),
    CheckConstraint(
        "(actor_principal_id IS NULL AND actor_binding_id IS NULL AND authorization_id IS NULL AND "
        "authorization_version IS NULL AND environment IS NULL) OR "
        "(actor_principal_id IS NOT NULL AND actor_binding_id IS NOT NULL AND authorization_id IS NOT NULL AND "
        "authorization_version IS NOT NULL AND environment IS NOT NULL)",
        name="ck_rfp_event_actor_shape",
    ),
    CheckConstraint(
        "NOT (safe_input_ref ?| ARRAY['token','access_token','password','prompt','rate','tariff'])",
        name="ck_rfp_event_safe_input",
    ),
    CheckConstraint(
        "NOT (safe_output_ref ?| ARRAY['token','access_token','password','prompt','rate','tariff'])",
        name="ck_rfp_event_safe_output",
    ),
    UniqueConstraint("approval_run_id", "thread_id", "sequence_no", name="uq_rfp_event_sequence"),
)
Index("ix_rfp_event_run_time", rfp_workflow_events.c.approval_run_id, rfp_workflow_events.c.created_at, rfp_workflow_events.c.event_id)
Index("ix_rfp_event_correlation", rfp_workflow_events.c.ticket_id, rfp_workflow_events.c.execution_id, rfp_workflow_events.c.thread_id, rfp_workflow_events.c.sequence_no)
