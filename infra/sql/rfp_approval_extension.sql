-- Forward-only Hito 9 Parte 3 evidence-integrity extension.
-- Apply only after rfp_intake.sql, rfp_response_generation.sql and
-- rfp_approval.sql on an explicitly authorized isolated PostgreSQL database.
BEGIN;

SELECT pg_advisory_xact_lock(hashtext('trackflow:rfp-approval-extension-v1'));

ALTER TABLE rfp_approval_department_revisions
  DROP CONSTRAINT IF EXISTS rfp_approval_department_revisions_status_check;
ALTER TABLE rfp_approval_department_revisions
  DROP CONSTRAINT IF EXISTS ck_rfp_department_revision_status;
ALTER TABLE rfp_approval_department_revisions
  ADD CONSTRAINT ck_rfp_department_revision_status CHECK(status IN (
    'pending_assessment','blocked_input','blocked_compliance','blocked_conflict','pending_approval',
    'approved','rejected','changes_requested','superseded','limit_reached'
  ));
ALTER TABLE rfp_approval_department_revisions
  DROP CONSTRAINT IF EXISTS ck_rfp_department_revision_blocked_conflict;
ALTER TABLE rfp_approval_department_revisions
  ADD CONSTRAINT ck_rfp_department_revision_blocked_conflict CHECK(
    status<>'blocked_conflict' OR ((
      jsonb_typeof(validation_payload->'eligible_for_approval')='boolean'
      AND validation_payload->>'eligible_for_approval'='false'
      AND jsonb_typeof(validation_payload->'mandatory_compliance_passed')='boolean'
      AND validation_payload->>'mandatory_compliance_passed'='true'
      AND jsonb_typeof(validation_payload->'blocks_commercial_completeness')='boolean'
      AND validation_payload->>'blocks_commercial_completeness'='false'
      AND jsonb_typeof(validation_payload->'open_conflict_ids')='array'
      AND jsonb_array_length(validation_payload->'open_conflict_ids')>0
      AND jsonb_typeof(validation_payload->'blocking_rule_ids')='array'
      AND jsonb_array_length(validation_payload->'blocking_rule_ids')=0
      AND jsonb_typeof(validation_payload->'unresolved_input_codes')='array'
      AND jsonb_array_length(validation_payload->'unresolved_input_codes')=0
      AND jsonb_typeof(content_payload->'pending_commercial_items')='array'
      AND jsonb_array_length(content_payload->'pending_commercial_items')=0
    ) IS TRUE)
  );

CREATE TABLE IF NOT EXISTS rfp_approval_corrected_drafts (
  draft_artifact_id uuid PRIMARY KEY,
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  scope_key text NOT NULL CHECK(
    scope_key IN ('warehouse','lastmile','reverse','proposal')
  ),
  contract_version text NOT NULL CHECK(
    contract_version='rfp-approval-corrected-draft-v1'
  ),
  content_payload jsonb NOT NULL CHECK(jsonb_typeof(content_payload)='object'),
  content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{64}$'),
  payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{64}$'),
  provenance jsonb NOT NULL CHECK(jsonb_typeof(provenance)='object'),
  provenance_hash text NOT NULL CHECK(provenance_hash ~ '^[a-f0-9]{64}$'),
  producer_node text NOT NULL CHECK(length(btrim(producer_node))>0),
  execution_id uuid NOT NULL,
  produced_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_corrected_draft_run FOREIGN KEY(approval_run_id,ticket_id)
    REFERENCES rfp_approval_runs(approval_run_id,ticket_id) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_corrected_draft_identity UNIQUE(
    draft_artifact_id,approval_run_id,ticket_id,scope_key,content_hash
  ),
  CONSTRAINT ck_rfp_corrected_draft_time CHECK(created_at>=produced_at)
);
CREATE INDEX IF NOT EXISTS ix_rfp_corrected_draft_run_scope
  ON rfp_approval_corrected_drafts(approval_run_id,scope_key,created_at);

CREATE TABLE IF NOT EXISTS rfp_approval_validation_artifacts (
  artifact_id uuid PRIMARY KEY,
  artifact_type text NOT NULL CHECK(
    artifact_type IN ('evaluation','global_evaluation','safe_preview')
  ),
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  scope_key text NOT NULL CHECK(
    scope_key IN ('warehouse','lastmile','reverse','proposal')
  ),
  corrected_draft_id uuid NOT NULL,
  evaluated_content_hash text NOT NULL CHECK(
    evaluated_content_hash ~ '^[a-f0-9]{64}$'
  ),
  contract_version text NOT NULL CHECK(length(btrim(contract_version))>0),
  payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
  payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{64}$'),
  provenance jsonb NOT NULL CHECK(jsonb_typeof(provenance)='object'),
  provenance_hash text NOT NULL CHECK(provenance_hash ~ '^[a-f0-9]{64}$'),
  producer_node text NOT NULL CHECK(length(btrim(producer_node))>0),
  execution_id uuid NOT NULL,
  produced_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_validation_artifact_draft FOREIGN KEY(
    corrected_draft_id,approval_run_id,ticket_id,scope_key,evaluated_content_hash
  ) REFERENCES rfp_approval_corrected_drafts(
    draft_artifact_id,approval_run_id,ticket_id,scope_key,content_hash
  ) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_validation_artifact_identity UNIQUE(
    artifact_id,approval_run_id,ticket_id,scope_key,corrected_draft_id,
    evaluated_content_hash,artifact_type,contract_version,payload_hash
  ),
  CONSTRAINT ck_rfp_validation_artifact_scope_type CHECK(
    (artifact_type='global_evaluation' AND scope_key='proposal')
    OR (artifact_type='evaluation' AND scope_key<>'proposal')
    OR artifact_type='safe_preview'
  ),
  CONSTRAINT ck_rfp_validation_artifact_time CHECK(created_at>=produced_at)
);
CREATE INDEX IF NOT EXISTS ix_rfp_validation_artifact_draft_type
  ON rfp_approval_validation_artifacts(corrected_draft_id,artifact_type,created_at);

CREATE TABLE IF NOT EXISTS rfp_approval_validation_bundles (
  validation_bundle_id uuid PRIMARY KEY,
  contract_version text NOT NULL CHECK(
    contract_version='rfp-approval-validation-bundle-v1'
  ),
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  scope_key text NOT NULL CHECK(
    scope_key IN ('warehouse','lastmile','reverse','proposal')
  ),
  corrected_draft_id uuid NOT NULL,
  content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{64}$'),
  evaluation_artifact_id uuid NOT NULL,
  evaluation_artifact_type text NOT NULL CHECK(
    evaluation_artifact_type IN ('evaluation','global_evaluation')
  ),
  evaluation_contract_version text NOT NULL CHECK(
    length(btrim(evaluation_contract_version))>0
  ),
  evaluation_payload_hash text NOT NULL CHECK(
    evaluation_payload_hash ~ '^[a-f0-9]{64}$'
  ),
  safe_preview_artifact_id uuid NOT NULL,
  safe_preview_artifact_type text NOT NULL DEFAULT 'safe_preview' CHECK(
    safe_preview_artifact_type='safe_preview'
  ),
  safe_preview_contract_version text NOT NULL CHECK(
    length(btrim(safe_preview_contract_version))>0
  ),
  safe_preview_payload_hash text NOT NULL CHECK(
    safe_preview_payload_hash ~ '^[a-f0-9]{64}$'
  ),
  revision_validation_hash text NOT NULL CHECK(
    revision_validation_hash ~ '^[a-f0-9]{64}$'
  ),
  mandatory_compliance_passed boolean NOT NULL,
  blocks_commercial_completeness boolean NOT NULL,
  blocking_rule_ids jsonb NOT NULL CHECK(jsonb_typeof(blocking_rule_ids)='array'),
  unresolved_input_codes jsonb NOT NULL CHECK(
    jsonb_typeof(unresolved_input_codes)='array'
  ),
  open_conflict_ids jsonb NOT NULL CHECK(jsonb_typeof(open_conflict_ids)='array'),
  source_departments_approved boolean NOT NULL,
  eligible boolean NOT NULL,
  provenance jsonb NOT NULL CHECK(jsonb_typeof(provenance)='object'),
  provenance_hash text NOT NULL CHECK(provenance_hash ~ '^[a-f0-9]{64}$'),
  bundle_hash text NOT NULL CHECK(bundle_hash ~ '^[a-f0-9]{64}$'),
  validated_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_validation_bundle_draft FOREIGN KEY(
    corrected_draft_id,approval_run_id,ticket_id,scope_key,content_hash
  ) REFERENCES rfp_approval_corrected_drafts(
    draft_artifact_id,approval_run_id,ticket_id,scope_key,content_hash
  ) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_validation_bundle_evaluation FOREIGN KEY(
    evaluation_artifact_id,approval_run_id,ticket_id,scope_key,
    corrected_draft_id,content_hash,evaluation_artifact_type,evaluation_contract_version,
    evaluation_payload_hash
  ) REFERENCES rfp_approval_validation_artifacts(
    artifact_id,approval_run_id,ticket_id,scope_key,
    corrected_draft_id,evaluated_content_hash,artifact_type,contract_version,payload_hash
  ) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_validation_bundle_preview FOREIGN KEY(
    safe_preview_artifact_id,approval_run_id,ticket_id,scope_key,
    corrected_draft_id,content_hash,safe_preview_artifact_type,
    safe_preview_contract_version,
    safe_preview_payload_hash
  ) REFERENCES rfp_approval_validation_artifacts(
    artifact_id,approval_run_id,ticket_id,scope_key,
    corrected_draft_id,evaluated_content_hash,artifact_type,contract_version,payload_hash
  ) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_validation_bundle_identity UNIQUE(
    validation_bundle_id,approval_run_id,ticket_id,scope_key,content_hash,
    revision_validation_hash
  ),
  CONSTRAINT ck_rfp_validation_bundle_scope_type CHECK(
    (scope_key='proposal' AND evaluation_artifact_type='global_evaluation')
    OR (scope_key<>'proposal' AND evaluation_artifact_type='evaluation')
  ),
  CONSTRAINT ck_rfp_validation_bundle_eligibility CHECK(
    eligible=(
      (scope_key<>'proposal' OR source_departments_approved)
      AND mandatory_compliance_passed
      AND jsonb_array_length(blocking_rule_ids)=0
      AND jsonb_array_length(open_conflict_ids)=0
      AND (
        (
          NOT blocks_commercial_completeness
          AND jsonb_array_length(unresolved_input_codes)=0
        )
        OR (
          blocks_commercial_completeness
          AND jsonb_array_length(unresolved_input_codes)>0
          AND unresolved_input_codes <@ '[
            "MONTHLY_STORAGE_VOLUME_REQUIRED",
            "LAST_MILE_RATE_REQUIRED",
            "RETURN_PROCESSING_RATE_REQUIRED",
            "VOLUME_DISCOUNT_APPROVAL_REQUIRED"
          ]'::jsonb
          AND jsonb_array_length(unresolved_input_codes)=(
            CASE WHEN unresolved_input_codes ?
              'MONTHLY_STORAGE_VOLUME_REQUIRED' THEN 1 ELSE 0 END
            + CASE WHEN unresolved_input_codes ?
              'LAST_MILE_RATE_REQUIRED' THEN 1 ELSE 0 END
            + CASE WHEN unresolved_input_codes ?
              'RETURN_PROCESSING_RATE_REQUIRED' THEN 1 ELSE 0 END
            + CASE WHEN unresolved_input_codes ?
              'VOLUME_DISCOUNT_APPROVAL_REQUIRED' THEN 1 ELSE 0 END
          )
        )
      )
    )
    AND blocks_commercial_completeness=(
      jsonb_array_length(unresolved_input_codes)>0
    )
  ),
  CONSTRAINT ck_rfp_validation_bundle_time CHECK(created_at>=validated_at)
);
COMMENT ON CONSTRAINT ck_rfp_validation_bundle_eligibility
  ON rfp_approval_validation_bundles IS
  'trackflow:indicative-validation-bundle-v1';
CREATE INDEX IF NOT EXISTS ix_rfp_validation_bundle_run_scope
  ON rfp_approval_validation_bundles(approval_run_id,scope_key,validated_at);

ALTER TABLE rfp_approval_department_revisions
  ADD COLUMN IF NOT EXISTS validation_bundle_id uuid;
ALTER TABLE rfp_approval_proposal_revisions
  ADD COLUMN IF NOT EXISTS validation_bundle_id uuid;

ALTER TABLE rfp_approval_department_revisions
  DROP CONSTRAINT IF EXISTS fk_rfp_department_revision_validation_bundle;
ALTER TABLE rfp_approval_department_revisions
  ADD CONSTRAINT fk_rfp_department_revision_validation_bundle FOREIGN KEY(
    validation_bundle_id,approval_run_id,ticket_id,department_id,content_hash,
    validation_hash
  ) REFERENCES rfp_approval_validation_bundles(
    validation_bundle_id,approval_run_id,ticket_id,scope_key,content_hash,
    revision_validation_hash
  ) ON DELETE RESTRICT;

ALTER TABLE rfp_approval_proposal_revisions
  DROP CONSTRAINT IF EXISTS fk_rfp_proposal_revision_validation_bundle;
ALTER TABLE rfp_approval_proposal_revisions
  ADD CONSTRAINT fk_rfp_proposal_revision_validation_bundle FOREIGN KEY(
    validation_bundle_id,approval_run_id,ticket_id,source_scope_key,content_hash,
    validation_hash
  ) REFERENCES rfp_approval_validation_bundles(
    validation_bundle_id,approval_run_id,ticket_id,scope_key,content_hash,
    revision_validation_hash
  ) ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION require_rfp_department_correction_bundle()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.review_revision_no>1 AND NEW.validation_bundle_id IS NULL THEN
    RAISE EXCEPTION 'corrected department revision requires validation bundle';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_rfp_department_correction_bundle
  ON rfp_approval_department_revisions;
CREATE TRIGGER trg_rfp_department_correction_bundle
BEFORE INSERT ON rfp_approval_department_revisions
FOR EACH ROW EXECUTE FUNCTION require_rfp_department_correction_bundle();

CREATE OR REPLACE FUNCTION require_rfp_proposal_validation_bundle()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.validation_bundle_id IS NULL THEN
    RAISE EXCEPTION 'proposal revision requires validation bundle';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_rfp_proposal_validation_bundle
  ON rfp_approval_proposal_revisions;
CREATE TRIGGER trg_rfp_proposal_validation_bundle
BEFORE INSERT ON rfp_approval_proposal_revisions
FOR EACH ROW EXECUTE FUNCTION require_rfp_proposal_validation_bundle();

CREATE OR REPLACE FUNCTION protect_rfp_department_revision_evidence()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.validation_bundle_id IS NOT NULL
     AND NEW.validation_bundle_id IS DISTINCT FROM OLD.validation_bundle_id THEN
    RAISE EXCEPTION 'department revision validation bundle is immutable once linked';
  END IF;
  IF NEW.revision_id IS DISTINCT FROM OLD.revision_id
     OR NEW.approval_run_id IS DISTINCT FROM OLD.approval_run_id
     OR NEW.ticket_id IS DISTINCT FROM OLD.ticket_id
     OR NEW.response_run_id IS DISTINCT FROM OLD.response_run_id
     OR NEW.department_id IS DISTINCT FROM OLD.department_id
     OR NEW.review_revision_no IS DISTINCT FROM OLD.review_revision_no
     OR NEW.source_part2_draft_id IS DISTINCT FROM OLD.source_part2_draft_id
     OR NEW.source_part2_draft_hash IS DISTINCT FROM OLD.source_part2_draft_hash
     OR NEW.source_part2_evaluation_id IS DISTINCT FROM OLD.source_part2_evaluation_id
     OR NEW.source_part2_evaluation_hash IS DISTINCT FROM OLD.source_part2_evaluation_hash
     OR NEW.source_part2_iteration IS DISTINCT FROM OLD.source_part2_iteration
     OR NEW.supersedes_revision_id IS DISTINCT FROM OLD.supersedes_revision_id
     OR NEW.content_payload IS DISTINCT FROM OLD.content_payload
     OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
     OR NEW.validation_payload IS DISTINCT FROM OLD.validation_payload
     OR NEW.validation_hash IS DISTINCT FROM OLD.validation_hash
     OR NEW.evaluated_content_hash IS DISTINCT FROM OLD.evaluated_content_hash
     OR NEW.created_by_principal_id IS DISTINCT FROM OLD.created_by_principal_id
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'department revision evidence is immutable';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_rfp_department_revision_evidence_immutable
  ON rfp_approval_department_revisions;
CREATE TRIGGER trg_rfp_department_revision_evidence_immutable
BEFORE UPDATE ON rfp_approval_department_revisions
FOR EACH ROW EXECUTE FUNCTION protect_rfp_department_revision_evidence();

CREATE OR REPLACE FUNCTION protect_rfp_proposal_revision_evidence()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.validation_bundle_id IS NOT NULL
     AND NEW.validation_bundle_id IS DISTINCT FROM OLD.validation_bundle_id THEN
    RAISE EXCEPTION 'proposal revision validation bundle is immutable once linked';
  END IF;
  IF NEW.proposal_revision_id IS DISTINCT FROM OLD.proposal_revision_id
     OR NEW.approval_run_id IS DISTINCT FROM OLD.approval_run_id
     OR NEW.ticket_id IS DISTINCT FROM OLD.ticket_id
     OR NEW.response_run_id IS DISTINCT FROM OLD.response_run_id
     OR NEW.revision_no IS DISTINCT FROM OLD.revision_no
     OR NEW.source_global_draft_id IS DISTINCT FROM OLD.source_global_draft_id
     OR NEW.source_global_draft_hash IS DISTINCT FROM OLD.source_global_draft_hash
     OR NEW.source_global_evaluation_id IS DISTINCT FROM OLD.source_global_evaluation_id
     OR NEW.source_global_evaluation_hash IS DISTINCT FROM OLD.source_global_evaluation_hash
     OR NEW.supersedes_revision_id IS DISTINCT FROM OLD.supersedes_revision_id
     OR NEW.warehouse_revision_id IS DISTINCT FROM OLD.warehouse_revision_id
     OR NEW.lastmile_revision_id IS DISTINCT FROM OLD.lastmile_revision_id
     OR NEW.reverse_revision_id IS DISTINCT FROM OLD.reverse_revision_id
     OR NEW.content_payload IS DISTINCT FROM OLD.content_payload
     OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
     OR NEW.validation_payload IS DISTINCT FROM OLD.validation_payload
     OR NEW.validation_hash IS DISTINCT FROM OLD.validation_hash
     OR NEW.evaluated_content_hash IS DISTINCT FROM OLD.evaluated_content_hash
     OR NEW.eligible_for_synthesis IS DISTINCT FROM OLD.eligible_for_synthesis
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'proposal revision evidence is immutable';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_rfp_proposal_revision_evidence_immutable
  ON rfp_approval_proposal_revisions;
CREATE TRIGGER trg_rfp_proposal_revision_evidence_immutable
BEFORE UPDATE ON rfp_approval_proposal_revisions
FOR EACH ROW EXECUTE FUNCTION protect_rfp_proposal_revision_evidence();

CREATE OR REPLACE FUNCTION reject_rfp_authorized_business_input_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'rfp_authorized_business_inputs is append-only';
END;
$$;
DROP TRIGGER IF EXISTS trg_rfp_authorized_business_inputs_append_only
  ON rfp_authorized_business_inputs;
CREATE TRIGGER trg_rfp_authorized_business_inputs_append_only
BEFORE UPDATE OR DELETE ON rfp_authorized_business_inputs
FOR EACH ROW EXECUTE FUNCTION reject_rfp_authorized_business_input_mutation();

CREATE OR REPLACE FUNCTION reject_rfp_approval_evidence_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

DROP TRIGGER IF EXISTS trg_rfp_corrected_drafts_append_only
  ON rfp_approval_corrected_drafts;
CREATE TRIGGER trg_rfp_corrected_drafts_append_only
BEFORE UPDATE OR DELETE ON rfp_approval_corrected_drafts
FOR EACH ROW EXECUTE FUNCTION reject_rfp_approval_evidence_mutation();

DROP TRIGGER IF EXISTS trg_rfp_validation_artifacts_append_only
  ON rfp_approval_validation_artifacts;
CREATE TRIGGER trg_rfp_validation_artifacts_append_only
BEFORE UPDATE OR DELETE ON rfp_approval_validation_artifacts
FOR EACH ROW EXECUTE FUNCTION reject_rfp_approval_evidence_mutation();

DROP TRIGGER IF EXISTS trg_rfp_validation_bundles_append_only
  ON rfp_approval_validation_bundles;
CREATE TRIGGER trg_rfp_validation_bundles_append_only
BEFORE UPDATE OR DELETE ON rfp_approval_validation_bundles
FOR EACH ROW EXECUTE FUNCTION reject_rfp_approval_evidence_mutation();

COMMIT;
