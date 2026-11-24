-- Forward-only Hito 9 Parte 3 business schema.
-- Apply only to an explicitly authorized, isolated PostgreSQL database after
-- infra/sql/rfp_intake.sql and infra/sql/rfp_response_generation.sql.
-- LangGraph checkpointer tables do not belong to this migration.
BEGIN;

SELECT pg_advisory_xact_lock(hashtext('trackflow:rfp-approval-schema-v1'));

ALTER TABLE rfp_tickets DROP CONSTRAINT IF EXISTS rfp_tickets_status_check;
ALTER TABLE rfp_tickets DROP CONSTRAINT IF EXISTS ck_rfp_ticket_status;
ALTER TABLE rfp_tickets ADD CONSTRAINT ck_rfp_ticket_status CHECK (
  status IN (
    'analizando','descartado','analisis_completo','generando_borrador',
    'en_evaluación','needs_human_review','esperando_aprobación','terminado'
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_response_handoff_run_ticket_hash
  ON rfp_response_handoffs(run_id,ticket_id,payload_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_response_draft_id_run
  ON rfp_response_drafts(draft_id,run_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_response_draft_id_run_scope
  ON rfp_response_drafts(draft_id,run_id,scope_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_response_evaluation_id_run_draft
  ON rfp_response_evaluations(evaluation_id,run_id,draft_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_response_evaluation_id_run_draft_scope
  ON rfp_response_evaluations(evaluation_id,run_id,draft_id,scope_key);

CREATE TABLE IF NOT EXISTS rfp_principals (
  principal_id uuid PRIMARY KEY,
  environment text NOT NULL CHECK(environment IN ('production','qa')),
  trackflow_user_id text NOT NULL CHECK(length(btrim(trackflow_user_id))>0),
  responsible_code text CHECK(
    responsible_code IS NULL OR responsible_code IN (
      'ana_whitfield','carlos_vega','sofia_ramos','miguel_torres'
    )
  ),
  synthetic boolean NOT NULL DEFAULT false,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  disabled_at timestamptz,
  CONSTRAINT ck_rfp_principal_synthetic_environment CHECK (
    NOT synthetic OR environment='qa'
  ),
  CONSTRAINT ck_rfp_principal_disabled_shape CHECK (
    (active AND disabled_at IS NULL) OR (NOT active AND disabled_at IS NOT NULL)
  ),
  CONSTRAINT uq_rfp_principal_id_environment UNIQUE(principal_id,environment),
  CONSTRAINT uq_rfp_principal_user_environment UNIQUE(environment,trackflow_user_id),
  CONSTRAINT uq_rfp_principal_responsible_environment UNIQUE(environment,responsible_code)
);

CREATE TABLE IF NOT EXISTS rfp_identity_bindings (
  binding_id uuid PRIMARY KEY,
  principal_id uuid NOT NULL,
  environment text NOT NULL CHECK(environment IN ('production','qa')),
  auth_source text NOT NULL CHECK(auth_source IN ('local','oidc')),
  issuer text NOT NULL CHECK(length(btrim(issuer))>0),
  subject text NOT NULL CHECK(length(btrim(subject))>0),
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  disabled_at timestamptz,
  CONSTRAINT fk_rfp_identity_principal_environment FOREIGN KEY(principal_id,environment)
    REFERENCES rfp_principals(principal_id,environment) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_identity_issuer_subject UNIQUE(environment,issuer,subject),
  CONSTRAINT uq_rfp_identity_binding_principal_environment
    UNIQUE(binding_id,principal_id,environment),
  CONSTRAINT ck_rfp_identity_disabled_shape CHECK (
    (active AND disabled_at IS NULL) OR (NOT active AND disabled_at IS NOT NULL)
  ),
  CONSTRAINT ck_rfp_identity_local_issuer CHECK (
    auth_source<>'local' OR issuer IN ('trackflow-local','urn:trackflow:qa:local')
  )
);

CREATE TABLE IF NOT EXISTS rfp_authorizations (
  authorization_id uuid PRIMARY KEY,
  principal_id uuid NOT NULL,
  environment text NOT NULL CHECK(environment IN ('production','qa')),
  permission text NOT NULL CHECK(permission IN (
    'start_approval_run','manage_approval_run','approve_department',
    'provide_business_input','arbitrate'
  )),
  department_scope text CHECK(
    department_scope IS NULL OR department_scope IN ('warehouse','lastmile','reverse')
  ),
  trigger_scope text CHECK(
    trigger_scope IS NULL OR trigger_scope IN (
      'volume-vs-capacity','returns-sla-breach','currency-mismatch'
    )
  ),
  target_policy text NOT NULL CHECK(target_policy IN (
    'run','department_exact','proposal_wide','cross_department','non_reverse_or_cross'
  )),
  scope_fingerprint text NOT NULL CHECK(scope_fingerprint ~ '^[a-f0-9]{64}$'),
  authorization_version integer NOT NULL DEFAULT 1 CHECK(authorization_version>=1),
  active_from timestamptz NOT NULL,
  expires_at timestamptz,
  revoked_at timestamptz,
  granted_by_principal_id uuid,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_authorization_principal_environment FOREIGN KEY(principal_id,environment)
    REFERENCES rfp_principals(principal_id,environment) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_authorization_grantor FOREIGN KEY(granted_by_principal_id)
    REFERENCES rfp_principals(principal_id) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_authorization_identity UNIQUE(
    authorization_id,principal_id,environment,authorization_version
  ),
  CONSTRAINT ck_rfp_authorization_time CHECK (
    (expires_at IS NULL OR expires_at>active_from)
    AND (revoked_at IS NULL OR revoked_at>=active_from)
  ),
  CONSTRAINT ck_rfp_authorization_scope_shape CHECK (
    (permission IN ('start_approval_run','manage_approval_run')
      AND department_scope IS NULL AND trigger_scope IS NULL AND target_policy='run')
    OR (permission='approve_department' AND department_scope IS NOT NULL
      AND trigger_scope IS NULL AND target_policy='department_exact')
    OR (permission='provide_business_input' AND trigger_scope IS NULL AND (
      (department_scope IS NOT NULL AND target_policy='department_exact')
      OR (department_scope IS NULL AND target_policy='proposal_wide')
    ))
    OR (permission='arbitrate' AND trigger_scope IS NOT NULL AND target_policy IN (
      'department_exact','proposal_wide','cross_department','non_reverse_or_cross'
    ))
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_active_authorization_scope
  ON rfp_authorizations(principal_id,permission,scope_fingerprint)
  WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_rfp_authorization_lookup
  ON rfp_authorizations(principal_id,permission,active_from,expires_at);

CREATE TABLE IF NOT EXISTS rfp_approval_runs (
  approval_run_id uuid PRIMARY KEY,
  ticket_id uuid NOT NULL REFERENCES rfp_tickets(ticket_id) ON DELETE RESTRICT,
  response_run_id uuid NOT NULL,
  contract_version text NOT NULL CHECK(contract_version='rfp-approval-start-v1'),
  response_handoff_version text NOT NULL CHECK(response_handoff_version='rfp-response-v1'),
  response_handoff_hash text NOT NULL CHECK(response_handoff_hash ~ '^[a-f0-9]{64}$'),
  active_departments jsonb NOT NULL CHECK(
    jsonb_typeof(active_departments)='array' AND jsonb_array_length(active_departments) BETWEEN 1 AND 3
  ),
  max_review_revisions integer NOT NULL DEFAULT 3 CHECK(max_review_revisions=3),
  status text NOT NULL CHECK(status IN ('active','blocked','finalizing','completed','failed')),
  state_version integer NOT NULL DEFAULT 0 CHECK(state_version>=0),
  start_idempotency_key uuid NOT NULL UNIQUE,
  start_request_hash text NOT NULL CHECK(start_request_hash ~ '^[a-f0-9]{64}$'),
  started_by_principal_id uuid NOT NULL,
  started_by_binding_id uuid NOT NULL,
  start_authorization_id uuid NOT NULL,
  authorization_version integer NOT NULL CHECK(authorization_version>=1),
  environment text NOT NULL CHECK(environment IN ('production','qa')),
  started_at timestamptz NOT NULL,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_approval_source_handoff FOREIGN KEY(
    response_run_id,ticket_id,response_handoff_hash
  ) REFERENCES rfp_response_handoffs(run_id,ticket_id,payload_hash) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_approval_starter_binding FOREIGN KEY(
    started_by_binding_id,started_by_principal_id,environment
  ) REFERENCES rfp_identity_bindings(binding_id,principal_id,environment) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_approval_start_authorization FOREIGN KEY(
    start_authorization_id,started_by_principal_id,environment,authorization_version
  ) REFERENCES rfp_authorizations(
    authorization_id,principal_id,environment,authorization_version
  ) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_approval_run_ticket_response
    UNIQUE(approval_run_id,ticket_id,response_run_id),
  CONSTRAINT uq_rfp_approval_run_ticket UNIQUE(approval_run_id,ticket_id),
  CONSTRAINT ck_rfp_approval_run_completion CHECK (
    (status='completed' AND completed_at IS NOT NULL)
    OR (status<>'completed' AND completed_at IS NULL)
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_approval_response_run
  ON rfp_approval_runs(response_run_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_approval_active_ticket
  ON rfp_approval_runs(ticket_id)
  WHERE status IN ('active','blocked','finalizing');
CREATE INDEX IF NOT EXISTS ix_rfp_approval_ticket_created
  ON rfp_approval_runs(ticket_id,created_at DESC);

CREATE TABLE IF NOT EXISTS rfp_approval_department_revisions (
  revision_id uuid PRIMARY KEY,
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  response_run_id uuid NOT NULL,
  department_id text NOT NULL CHECK(department_id IN ('warehouse','lastmile','reverse')),
  review_revision_no integer NOT NULL CHECK(review_revision_no BETWEEN 1 AND 3),
  source_part2_draft_id uuid NOT NULL,
  source_part2_draft_hash text NOT NULL CHECK(source_part2_draft_hash ~ '^[a-f0-9]{64}$'),
  source_part2_evaluation_id uuid NOT NULL,
  source_part2_evaluation_hash text NOT NULL CHECK(source_part2_evaluation_hash ~ '^[a-f0-9]{64}$'),
  source_part2_iteration integer NOT NULL CHECK(source_part2_iteration BETWEEN 1 AND 3),
  supersedes_revision_id uuid,
  superseded_by_revision_id uuid,
  content_payload jsonb NOT NULL CHECK(jsonb_typeof(content_payload)='object'),
  content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{64}$'),
  validation_payload jsonb NOT NULL CHECK(jsonb_typeof(validation_payload)='object'),
  validation_hash text NOT NULL CHECK(validation_hash ~ '^[a-f0-9]{64}$'),
  evaluated_content_hash text NOT NULL CHECK(evaluated_content_hash ~ '^[a-f0-9]{64}$'),
  status text NOT NULL CHECK(status IN (
    'blocked_input','blocked_compliance','pending_approval','approved','rejected',
    'changes_requested','superseded','limit_reached'
  )),
  is_current boolean NOT NULL DEFAULT true,
  state_version integer NOT NULL DEFAULT 0 CHECK(state_version>=0),
  applied_decision_id uuid,
  created_by_principal_id uuid,
  created_at timestamptz NOT NULL,
  CONSTRAINT fk_rfp_department_revision_run FOREIGN KEY(
    approval_run_id,ticket_id,response_run_id
  ) REFERENCES rfp_approval_runs(approval_run_id,ticket_id,response_run_id)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_department_revision_source_draft FOREIGN KEY(
    source_part2_draft_id,response_run_id,department_id
  ) REFERENCES rfp_response_drafts(draft_id,run_id,scope_key) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_department_revision_source_evaluation FOREIGN KEY(
    source_part2_evaluation_id,response_run_id,source_part2_draft_id,department_id
  ) REFERENCES rfp_response_evaluations(evaluation_id,run_id,draft_id,scope_key)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_department_revision_creator FOREIGN KEY(created_by_principal_id)
    REFERENCES rfp_principals(principal_id) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_department_revision_number
    UNIQUE(approval_run_id,department_id,review_revision_no),
  CONSTRAINT uq_rfp_department_revision_identity
    UNIQUE(revision_id,approval_run_id,ticket_id,department_id),
  CONSTRAINT uq_rfp_department_revision_run_identity
    UNIQUE(revision_id,approval_run_id,ticket_id),
  CONSTRAINT uq_rfp_department_revision_successor UNIQUE(supersedes_revision_id),
  CONSTRAINT fk_rfp_department_revision_predecessor FOREIGN KEY(
    supersedes_revision_id,approval_run_id,ticket_id,department_id
  ) REFERENCES rfp_approval_department_revisions(
    revision_id,approval_run_id,ticket_id,department_id
  ) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_department_revision_successor FOREIGN KEY(
    superseded_by_revision_id,approval_run_id,ticket_id,department_id
  ) REFERENCES rfp_approval_department_revisions(
    revision_id,approval_run_id,ticket_id,department_id
  ) ON DELETE RESTRICT,
  CONSTRAINT ck_rfp_department_revision_hash_binding CHECK(
    evaluated_content_hash=content_hash
  ),
  CONSTRAINT ck_rfp_department_revision_lineage CHECK(
    revision_id IS DISTINCT FROM supersedes_revision_id
    AND revision_id IS DISTINCT FROM superseded_by_revision_id
    AND ((review_revision_no=1 AND supersedes_revision_id IS NULL)
      OR (review_revision_no>1 AND supersedes_revision_id IS NOT NULL))
  ),
  CONSTRAINT ck_rfp_department_revision_current CHECK(
    (status='superseded' AND NOT is_current AND superseded_by_revision_id IS NOT NULL)
    OR (status<>'superseded' AND is_current AND superseded_by_revision_id IS NULL)
  ),
  CONSTRAINT ck_rfp_department_revision_limit CHECK(
    status<>'limit_reached' OR review_revision_no=3
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_department_current
  ON rfp_approval_department_revisions(approval_run_id,department_id)
  WHERE is_current;
CREATE INDEX IF NOT EXISTS ix_rfp_department_revision_run
  ON rfp_approval_department_revisions(approval_run_id,department_id,created_at DESC);

CREATE TABLE IF NOT EXISTS rfp_approval_proposal_revisions (
  proposal_revision_id uuid PRIMARY KEY,
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  response_run_id uuid NOT NULL,
  revision_no integer NOT NULL CHECK(revision_no BETWEEN 1 AND 3),
  source_global_draft_id uuid NOT NULL,
  source_global_draft_hash text NOT NULL CHECK(source_global_draft_hash ~ '^[a-f0-9]{64}$'),
  source_global_evaluation_id uuid NOT NULL,
  source_global_evaluation_hash text NOT NULL CHECK(source_global_evaluation_hash ~ '^[a-f0-9]{64}$'),
  source_scope_key text GENERATED ALWAYS AS ('proposal') STORED,
  supersedes_revision_id uuid,
  superseded_by_revision_id uuid,
  warehouse_revision_id uuid,
  warehouse_department_id text GENERATED ALWAYS AS ('warehouse') STORED,
  lastmile_revision_id uuid,
  lastmile_department_id text GENERATED ALWAYS AS ('lastmile') STORED,
  reverse_revision_id uuid,
  reverse_department_id text GENERATED ALWAYS AS ('reverse') STORED,
  content_payload jsonb NOT NULL CHECK(jsonb_typeof(content_payload)='object'),
  content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{64}$'),
  validation_payload jsonb NOT NULL CHECK(jsonb_typeof(validation_payload)='object'),
  validation_hash text NOT NULL CHECK(validation_hash ~ '^[a-f0-9]{64}$'),
  evaluated_content_hash text NOT NULL CHECK(evaluated_content_hash ~ '^[a-f0-9]{64}$'),
  eligible_for_synthesis boolean NOT NULL,
  is_current boolean NOT NULL DEFAULT true,
  state_version integer NOT NULL DEFAULT 0 CHECK(state_version>=0),
  created_at timestamptz NOT NULL,
  CONSTRAINT fk_rfp_proposal_run FOREIGN KEY(approval_run_id,ticket_id,response_run_id)
    REFERENCES rfp_approval_runs(approval_run_id,ticket_id,response_run_id)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_proposal_source_draft FOREIGN KEY(
    source_global_draft_id,response_run_id,source_scope_key
  ) REFERENCES rfp_response_drafts(draft_id,run_id,scope_key) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_proposal_source_evaluation FOREIGN KEY(
    source_global_evaluation_id,response_run_id,source_global_draft_id,source_scope_key
  ) REFERENCES rfp_response_evaluations(evaluation_id,run_id,draft_id,scope_key)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_proposal_warehouse_revision FOREIGN KEY(
    warehouse_revision_id,approval_run_id,ticket_id,warehouse_department_id
  ) REFERENCES rfp_approval_department_revisions(
    revision_id,approval_run_id,ticket_id,department_id
  )
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_proposal_lastmile_revision FOREIGN KEY(
    lastmile_revision_id,approval_run_id,ticket_id,lastmile_department_id
  ) REFERENCES rfp_approval_department_revisions(
    revision_id,approval_run_id,ticket_id,department_id
  )
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_proposal_reverse_revision FOREIGN KEY(
    reverse_revision_id,approval_run_id,ticket_id,reverse_department_id
  ) REFERENCES rfp_approval_department_revisions(
    revision_id,approval_run_id,ticket_id,department_id
  )
    ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_proposal_revision_number UNIQUE(approval_run_id,revision_no),
  CONSTRAINT uq_rfp_proposal_revision_identity
    UNIQUE(proposal_revision_id,approval_run_id,ticket_id),
  CONSTRAINT uq_rfp_proposal_successor UNIQUE(supersedes_revision_id),
  CONSTRAINT fk_rfp_proposal_predecessor FOREIGN KEY(
    supersedes_revision_id,approval_run_id,ticket_id
  ) REFERENCES rfp_approval_proposal_revisions(
    proposal_revision_id,approval_run_id,ticket_id
  ) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_proposal_successor FOREIGN KEY(
    superseded_by_revision_id,approval_run_id,ticket_id
  ) REFERENCES rfp_approval_proposal_revisions(
    proposal_revision_id,approval_run_id,ticket_id
  ) ON DELETE RESTRICT,
  CONSTRAINT ck_rfp_proposal_has_department CHECK(
    num_nonnulls(warehouse_revision_id,lastmile_revision_id,reverse_revision_id)>=1
  ),
  CONSTRAINT ck_rfp_proposal_hash_binding CHECK(evaluated_content_hash=content_hash),
  CONSTRAINT ck_rfp_proposal_lineage CHECK(
    proposal_revision_id IS DISTINCT FROM supersedes_revision_id
    AND proposal_revision_id IS DISTINCT FROM superseded_by_revision_id
    AND ((revision_no=1 AND supersedes_revision_id IS NULL)
      OR (revision_no>1 AND supersedes_revision_id IS NOT NULL))
  ),
  CONSTRAINT ck_rfp_proposal_current CHECK(
    (NOT is_current AND superseded_by_revision_id IS NOT NULL)
    OR (is_current AND superseded_by_revision_id IS NULL)
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_proposal_current
  ON rfp_approval_proposal_revisions(approval_run_id) WHERE is_current;

CREATE TABLE IF NOT EXISTS rfp_human_decisions (
  decision_id uuid PRIMARY KEY,
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  department_id text NOT NULL CHECK(department_id IN ('warehouse','lastmile','reverse')),
  idempotency_key uuid NOT NULL,
  application_key text NOT NULL CHECK(application_key ~ '^decision-apply:[a-f0-9]{64}$'),
  expected_content_hash text NOT NULL CHECK(expected_content_hash ~ '^[a-f0-9]{64}$'),
  action text NOT NULL CHECK(action IN ('approve','reject','request_changes')),
  reason text,
  processing_status text NOT NULL DEFAULT 'queued' CHECK(processing_status IN (
    'queued','claimed','applied','rejected_stale'
  )),
  actor_principal_id uuid NOT NULL,
  actor_binding_id uuid NOT NULL,
  authorization_id uuid NOT NULL,
  authorization_version integer NOT NULL CHECK(authorization_version>=1),
  environment text NOT NULL CHECK(environment IN ('production','qa')),
  claim_token uuid,
  claim_epoch integer NOT NULL DEFAULT 0 CHECK(claim_epoch>=0),
  claimed_at timestamptz,
  lease_until timestamptz,
  decided_at timestamptz NOT NULL,
  applied_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_decision_revision FOREIGN KEY(
    revision_id,approval_run_id,ticket_id,department_id
  ) REFERENCES rfp_approval_department_revisions(
    revision_id,approval_run_id,ticket_id,department_id
  ) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_decision_actor_binding FOREIGN KEY(
    actor_binding_id,actor_principal_id,environment
  ) REFERENCES rfp_identity_bindings(binding_id,principal_id,environment)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_decision_authorization FOREIGN KEY(
    authorization_id,actor_principal_id,environment,authorization_version
  ) REFERENCES rfp_authorizations(
    authorization_id,principal_id,environment,authorization_version
  ) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_decision_revision UNIQUE(revision_id),
  CONSTRAINT uq_rfp_decision_idempotency UNIQUE(approval_run_id,idempotency_key),
  CONSTRAINT uq_rfp_decision_application UNIQUE(application_key),
  CONSTRAINT uq_rfp_decision_revision_identity
    UNIQUE(decision_id,revision_id,approval_run_id,ticket_id),
  CONSTRAINT uq_rfp_decision_run_identity
    UNIQUE(decision_id,approval_run_id,ticket_id),
  CONSTRAINT ck_rfp_decision_reason CHECK(
    action='approve' OR (reason IS NOT NULL AND length(btrim(reason))>0)
  ),
  CONSTRAINT ck_rfp_decision_claim_shape CHECK(
    (processing_status='queued' AND claim_token IS NULL AND claimed_at IS NULL
      AND lease_until IS NULL AND applied_at IS NULL)
    OR (processing_status='claimed' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL
      AND lease_until>claimed_at AND applied_at IS NULL)
    OR (processing_status IN ('applied','rejected_stale') AND applied_at IS NOT NULL)
  )
);
CREATE INDEX IF NOT EXISTS ix_rfp_decision_queue
  ON rfp_human_decisions(processing_status,lease_until,created_at);

ALTER TABLE rfp_approval_department_revisions
  DROP CONSTRAINT IF EXISTS fk_rfp_department_applied_decision;
ALTER TABLE rfp_approval_department_revisions
  ADD CONSTRAINT fk_rfp_department_applied_decision FOREIGN KEY(
    applied_decision_id,revision_id,approval_run_id,ticket_id
  ) REFERENCES rfp_human_decisions(decision_id,revision_id,approval_run_id,ticket_id)
    ON DELETE RESTRICT;

CREATE TABLE IF NOT EXISTS rfp_authorized_business_inputs (
  input_id uuid PRIMARY KEY,
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  input_kind text NOT NULL CHECK(input_kind IN (
    'client_price_quote','warehouse_capacity','operational_scope','currency_quote',
    'currency_conversion','global_commercial_term','volume_discount'
  )),
  department_scope text CHECK(
    department_scope IS NULL OR department_scope IN ('warehouse','lastmile','reverse')
  ),
  payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
  provenance jsonb NOT NULL CHECK(jsonb_typeof(provenance)='object'),
  payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{64}$'),
  provenance_hash text NOT NULL CHECK(provenance_hash ~ '^[a-f0-9]{64}$'),
  evidence_hash text NOT NULL CHECK(evidence_hash ~ '^[a-f0-9]{64}$'),
  idempotency_key uuid NOT NULL,
  actor_principal_id uuid NOT NULL,
  actor_binding_id uuid NOT NULL,
  authorization_id uuid NOT NULL,
  authorization_version integer NOT NULL CHECK(authorization_version>=1),
  environment text NOT NULL CHECK(environment IN ('production','qa')),
  recorded_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_business_input_run FOREIGN KEY(approval_run_id,ticket_id)
    REFERENCES rfp_approval_runs(approval_run_id,ticket_id) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_business_input_actor FOREIGN KEY(
    actor_binding_id,actor_principal_id,environment
  ) REFERENCES rfp_identity_bindings(binding_id,principal_id,environment)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_business_input_authorization FOREIGN KEY(
    authorization_id,actor_principal_id,environment,authorization_version
  ) REFERENCES rfp_authorizations(
    authorization_id,principal_id,environment,authorization_version
  ) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_business_input_idempotency UNIQUE(approval_run_id,idempotency_key),
  CONSTRAINT uq_rfp_business_input_identity UNIQUE(input_id,approval_run_id,ticket_id)
);
CREATE INDEX IF NOT EXISTS ix_rfp_business_input_run_kind
  ON rfp_authorized_business_inputs(approval_run_id,input_kind,recorded_at);

CREATE TABLE IF NOT EXISTS rfp_conflict_assessments (
  assessment_id uuid PRIMARY KEY,
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  trigger_id text NOT NULL CHECK(trigger_id IN (
    'volume-vs-capacity','returns-sla-breach','currency-mismatch'
  )),
  outcome text NOT NULL CHECK(outcome IN (
    'demonstrated','insufficient_information','no_conflict'
  )),
  facts_payload jsonb NOT NULL CHECK(jsonb_typeof(facts_payload)='object'),
  missing_input_codes jsonb NOT NULL CHECK(jsonb_typeof(missing_input_codes)='array'),
  assessed_content_hashes jsonb NOT NULL CHECK(jsonb_typeof(assessed_content_hashes)='array'),
  assessment_hash text NOT NULL CHECK(assessment_hash ~ '^[a-f0-9]{64}$'),
  assessed_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_assessment_run FOREIGN KEY(approval_run_id,ticket_id)
    REFERENCES rfp_approval_runs(approval_run_id,ticket_id) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_assessment_hash UNIQUE(approval_run_id,assessment_hash),
  CONSTRAINT uq_rfp_assessment_identity
    UNIQUE(assessment_id,approval_run_id,ticket_id,trigger_id,outcome),
  CONSTRAINT ck_rfp_assessment_missing_shape CHECK(
    (outcome='insufficient_information' AND jsonb_array_length(missing_input_codes)>0)
    OR (outcome<>'insufficient_information' AND jsonb_array_length(missing_input_codes)=0)
  )
);

CREATE TABLE IF NOT EXISTS rfp_arbitration_conflicts (
  conflict_id uuid PRIMARY KEY,
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  assessment_id uuid NOT NULL,
  assessment_outcome text NOT NULL CHECK(
    assessment_outcome IN ('demonstrated','insufficient_information')
  ),
  trigger_id text NOT NULL CHECK(trigger_id IN (
    'volume-vs-capacity','returns-sla-breach','currency-mismatch'
  )),
  affected_targets jsonb NOT NULL CHECK(
    jsonb_typeof(affected_targets)='array' AND jsonb_array_length(affected_targets)>0
  ),
  fingerprint text NOT NULL CHECK(fingerprint ~ '^[a-f0-9]{64}$'),
  status text NOT NULL CHECK(status IN ('open','changes_requested','resolved','superseded')),
  state_version integer NOT NULL DEFAULT 0 CHECK(state_version>=0),
  detected_at timestamptz NOT NULL,
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_conflict_assessment FOREIGN KEY(
    assessment_id,approval_run_id,ticket_id,trigger_id,assessment_outcome
  ) REFERENCES rfp_conflict_assessments(
    assessment_id,approval_run_id,ticket_id,trigger_id,outcome
  ) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_conflict_fingerprint UNIQUE(approval_run_id,fingerprint),
  CONSTRAINT uq_rfp_conflict_identity
    UNIQUE(conflict_id,approval_run_id,ticket_id,trigger_id,fingerprint),
  CONSTRAINT uq_rfp_conflict_run_identity
    UNIQUE(conflict_id,approval_run_id,ticket_id),
  CONSTRAINT ck_rfp_conflict_resolution_shape CHECK(
    (status='resolved' AND resolved_at IS NOT NULL)
    OR (status<>'resolved' AND resolved_at IS NULL)
  )
);
CREATE INDEX IF NOT EXISTS ix_rfp_conflict_open
  ON rfp_arbitration_conflicts(approval_run_id,status,detected_at);

CREATE TABLE IF NOT EXISTS rfp_arbitration_resolutions (
  resolution_id uuid PRIMARY KEY,
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  conflict_id uuid NOT NULL,
  trigger_id text NOT NULL CHECK(trigger_id IN (
    'volume-vs-capacity','returns-sla-breach','currency-mismatch'
  )),
  conflict_fingerprint text NOT NULL CHECK(conflict_fingerprint ~ '^[a-f0-9]{64}$'),
  action text NOT NULL CHECK(action IN ('request_changes','accept_resolution')),
  targets jsonb NOT NULL CHECK(
    jsonb_typeof(targets)='array' AND jsonb_array_length(targets)>0
  ),
  authorized_input_ids jsonb NOT NULL CHECK(jsonb_typeof(authorized_input_ids)='array'),
  reason text NOT NULL CHECK(length(btrim(reason))>0),
  actor_principal_id uuid NOT NULL,
  actor_binding_id uuid NOT NULL,
  authorization_id uuid NOT NULL,
  authorization_version integer NOT NULL CHECK(authorization_version>=1),
  environment text NOT NULL CHECK(environment IN ('production','qa')),
  post_assessment_id uuid,
  post_assessment_outcome text,
  resolution_hash text NOT NULL CHECK(resolution_hash ~ '^[a-f0-9]{64}$'),
  decided_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_resolution_conflict FOREIGN KEY(
    conflict_id,approval_run_id,ticket_id,trigger_id,conflict_fingerprint
  ) REFERENCES rfp_arbitration_conflicts(
    conflict_id,approval_run_id,ticket_id,trigger_id,fingerprint
  ) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_resolution_actor FOREIGN KEY(
    actor_binding_id,actor_principal_id,environment
  ) REFERENCES rfp_identity_bindings(binding_id,principal_id,environment)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_resolution_authorization FOREIGN KEY(
    authorization_id,actor_principal_id,environment,authorization_version
  ) REFERENCES rfp_authorizations(
    authorization_id,principal_id,environment,authorization_version
  ) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_resolution_post_assessment FOREIGN KEY(
    post_assessment_id,approval_run_id,ticket_id,trigger_id,post_assessment_outcome
  ) REFERENCES rfp_conflict_assessments(
    assessment_id,approval_run_id,ticket_id,trigger_id,outcome
  ) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_resolution_conflict_hash UNIQUE(conflict_id,conflict_fingerprint),
  CONSTRAINT uq_rfp_resolution_identity UNIQUE(resolution_id,approval_run_id,ticket_id),
  CONSTRAINT ck_rfp_resolution_recheck_shape CHECK(
    (action='request_changes' AND post_assessment_id IS NULL
      AND post_assessment_outcome IS NULL)
    OR (action='accept_resolution' AND post_assessment_id IS NOT NULL
      AND post_assessment_outcome='no_conflict')
  )
);

CREATE TABLE IF NOT EXISTS rfp_arbitration_resolution_evidence (
  evidence_id uuid PRIMARY KEY,
  resolution_id uuid NOT NULL,
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  original_department_revision_id uuid,
  original_proposal_revision_id uuid,
  corrected_department_revision_id uuid,
  corrected_proposal_revision_id uuid,
  corrected_content_hash text NOT NULL CHECK(corrected_content_hash ~ '^[a-f0-9]{64}$'),
  revision_validation_artifact_id uuid NOT NULL,
  revision_validation_hash text NOT NULL CHECK(revision_validation_hash ~ '^[a-f0-9]{64}$'),
  conflict_recheck_artifact_id uuid NOT NULL,
  conflict_recheck_hash text NOT NULL CHECK(conflict_recheck_hash ~ '^[a-f0-9]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_resolution_evidence_resolution FOREIGN KEY(
    resolution_id,approval_run_id,ticket_id
  ) REFERENCES rfp_arbitration_resolutions(resolution_id,approval_run_id,ticket_id)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_resolution_original_department FOREIGN KEY(
    original_department_revision_id,approval_run_id,ticket_id
  ) REFERENCES rfp_approval_department_revisions(revision_id,approval_run_id,ticket_id)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_resolution_original_proposal FOREIGN KEY(
    original_proposal_revision_id,approval_run_id,ticket_id
  ) REFERENCES rfp_approval_proposal_revisions(
    proposal_revision_id,approval_run_id,ticket_id
  ) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_resolution_corrected_department FOREIGN KEY(
    corrected_department_revision_id,approval_run_id,ticket_id
  ) REFERENCES rfp_approval_department_revisions(revision_id,approval_run_id,ticket_id)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_resolution_corrected_proposal FOREIGN KEY(
    corrected_proposal_revision_id,approval_run_id,ticket_id
  ) REFERENCES rfp_approval_proposal_revisions(
    proposal_revision_id,approval_run_id,ticket_id
  ) ON DELETE RESTRICT,
  CONSTRAINT ck_rfp_resolution_evidence_kind CHECK(
    num_nonnulls(original_department_revision_id,original_proposal_revision_id)=1
    AND num_nonnulls(corrected_department_revision_id,corrected_proposal_revision_id)=1
    AND ((original_department_revision_id IS NOT NULL
      AND corrected_department_revision_id IS NOT NULL)
      OR (original_proposal_revision_id IS NOT NULL
      AND corrected_proposal_revision_id IS NOT NULL))
  ),
  CONSTRAINT ck_rfp_resolution_evidence_new_revision CHECK(
    (original_department_revision_id IS NOT NULL
      AND original_department_revision_id<>corrected_department_revision_id)
    OR (original_proposal_revision_id IS NOT NULL
      AND original_proposal_revision_id<>corrected_proposal_revision_id)
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_resolution_evidence_department
  ON rfp_arbitration_resolution_evidence(
    resolution_id,corrected_department_revision_id
  ) WHERE corrected_department_revision_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_resolution_evidence_proposal
  ON rfp_arbitration_resolution_evidence(
    resolution_id,corrected_proposal_revision_id
  ) WHERE corrected_proposal_revision_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS rfp_final_documents (
  final_document_id uuid PRIMARY KEY,
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  proposal_revision_id uuid NOT NULL,
  warehouse_revision_id uuid,
  warehouse_department_id text GENERATED ALWAYS AS ('warehouse') STORED,
  lastmile_revision_id uuid,
  lastmile_department_id text GENERATED ALWAYS AS ('lastmile') STORED,
  reverse_revision_id uuid,
  reverse_department_id text GENERATED ALWAYS AS ('reverse') STORED,
  contract_version text NOT NULL CHECK(contract_version='rfp-persisted-final-document-v1'),
  payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
  payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{64}$'),
  markdown text NOT NULL CHECK(length(markdown)>0),
  markdown_hash text NOT NULL CHECK(markdown_hash ~ '^[a-f0-9]{64}$'),
  storage_reference text NOT NULL CHECK(length(btrim(storage_reference))>0),
  generated_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_final_run FOREIGN KEY(approval_run_id,ticket_id)
    REFERENCES rfp_approval_runs(approval_run_id,ticket_id) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_final_proposal FOREIGN KEY(
    proposal_revision_id,approval_run_id,ticket_id
  ) REFERENCES rfp_approval_proposal_revisions(
    proposal_revision_id,approval_run_id,ticket_id
  ) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_final_warehouse FOREIGN KEY(
    warehouse_revision_id,approval_run_id,ticket_id,warehouse_department_id
  ) REFERENCES rfp_approval_department_revisions(
    revision_id,approval_run_id,ticket_id,department_id
  )
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_final_lastmile FOREIGN KEY(
    lastmile_revision_id,approval_run_id,ticket_id,lastmile_department_id
  ) REFERENCES rfp_approval_department_revisions(
    revision_id,approval_run_id,ticket_id,department_id
  )
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_final_reverse FOREIGN KEY(
    reverse_revision_id,approval_run_id,ticket_id,reverse_department_id
  ) REFERENCES rfp_approval_department_revisions(
    revision_id,approval_run_id,ticket_id,department_id
  )
    ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_final_run UNIQUE(approval_run_id),
  CONSTRAINT uq_rfp_final_ticket UNIQUE(ticket_id),
  CONSTRAINT ck_rfp_final_has_department CHECK(
    num_nonnulls(warehouse_revision_id,lastmile_revision_id,reverse_revision_id)>=1
  )
);

CREATE TABLE IF NOT EXISTS rfp_workflow_events (
  event_id uuid PRIMARY KEY,
  event_key text NOT NULL UNIQUE CHECK(length(btrim(event_key))>0),
  approval_run_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  thread_id text NOT NULL CHECK(length(btrim(thread_id))>0),
  execution_id uuid NOT NULL,
  sequence_no bigint NOT NULL CHECK(sequence_no>=1),
  node_name text NOT NULL CHECK(length(btrim(node_name))>0),
  agent_name text,
  event_type text NOT NULL CHECK(length(btrim(event_type))>0),
  safe_input_ref jsonb NOT NULL CHECK(jsonb_typeof(safe_input_ref)='object'),
  safe_output_ref jsonb NOT NULL CHECK(jsonb_typeof(safe_output_ref)='object'),
  result text NOT NULL CHECK(length(btrim(result))>0),
  from_state text,
  to_state text,
  department_revision_id uuid,
  proposal_revision_id uuid,
  decision_id uuid,
  conflict_id uuid,
  actor_principal_id uuid,
  actor_binding_id uuid,
  authorization_id uuid,
  authorization_version integer,
  environment text CHECK(environment IS NULL OR environment IN ('production','qa')),
  started_at timestamptz NOT NULL,
  finished_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_rfp_event_run FOREIGN KEY(approval_run_id,ticket_id)
    REFERENCES rfp_approval_runs(approval_run_id,ticket_id) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_event_department_revision FOREIGN KEY(
    department_revision_id,approval_run_id,ticket_id
  ) REFERENCES rfp_approval_department_revisions(revision_id,approval_run_id,ticket_id)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_event_proposal_revision FOREIGN KEY(
    proposal_revision_id,approval_run_id,ticket_id
  ) REFERENCES rfp_approval_proposal_revisions(
    proposal_revision_id,approval_run_id,ticket_id
  ) ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_event_decision FOREIGN KEY(
    decision_id,approval_run_id,ticket_id
  ) REFERENCES rfp_human_decisions(decision_id,approval_run_id,ticket_id)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_event_conflict FOREIGN KEY(
    conflict_id,approval_run_id,ticket_id
  ) REFERENCES rfp_arbitration_conflicts(conflict_id,approval_run_id,ticket_id)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_event_actor_binding FOREIGN KEY(
    actor_binding_id,actor_principal_id,environment
  ) REFERENCES rfp_identity_bindings(binding_id,principal_id,environment)
    ON DELETE RESTRICT,
  CONSTRAINT fk_rfp_event_authorization FOREIGN KEY(
    authorization_id,actor_principal_id,environment,authorization_version
  ) REFERENCES rfp_authorizations(
    authorization_id,principal_id,environment,authorization_version
  ) ON DELETE RESTRICT,
  CONSTRAINT uq_rfp_event_sequence UNIQUE(approval_run_id,thread_id,sequence_no),
  CONSTRAINT ck_rfp_event_time CHECK(finished_at>=started_at),
  CONSTRAINT ck_rfp_event_actor_shape CHECK(
    (actor_principal_id IS NULL AND actor_binding_id IS NULL
      AND authorization_id IS NULL AND authorization_version IS NULL
      AND environment IS NULL)
    OR (actor_principal_id IS NOT NULL AND actor_binding_id IS NOT NULL
      AND authorization_id IS NOT NULL AND authorization_version IS NOT NULL
      AND environment IS NOT NULL)
  ),
  CONSTRAINT ck_rfp_event_safe_input CHECK(
    NOT (safe_input_ref ?| ARRAY[
      'token','access_token','password','prompt','rate','tariff'
    ])
  ),
  CONSTRAINT ck_rfp_event_safe_output CHECK(
    NOT (safe_output_ref ?| ARRAY[
      'token','access_token','password','prompt','rate','tariff'
    ])
  )
);
CREATE INDEX IF NOT EXISTS ix_rfp_event_run_time
  ON rfp_workflow_events(approval_run_id,created_at,event_id);
CREATE INDEX IF NOT EXISTS ix_rfp_event_correlation
  ON rfp_workflow_events(ticket_id,execution_id,thread_id,sequence_no);

CREATE OR REPLACE FUNCTION reject_rfp_workflow_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'rfp_workflow_events is append-only';
END;
$$;
DROP TRIGGER IF EXISTS trg_rfp_workflow_events_append_only ON rfp_workflow_events;
CREATE TRIGGER trg_rfp_workflow_events_append_only
BEFORE UPDATE OR DELETE ON rfp_workflow_events
FOR EACH ROW EXECUTE FUNCTION reject_rfp_workflow_event_mutation();

COMMIT;
