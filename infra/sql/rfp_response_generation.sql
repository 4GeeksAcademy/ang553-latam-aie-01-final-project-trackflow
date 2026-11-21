-- Forward-only Hito 9 Parte 2 migration. Apply only to an authorized database.
BEGIN;

ALTER TABLE rfp_tickets DROP CONSTRAINT IF EXISTS rfp_tickets_status_check;
ALTER TABLE rfp_tickets DROP CONSTRAINT IF EXISTS ck_rfp_ticket_status;
ALTER TABLE rfp_tickets ADD CONSTRAINT ck_rfp_ticket_status CHECK (
  status IN ('analizando','descartado','analisis_completo','generando_borrador','en_evaluación','needs_human_review')
);

ALTER TABLE rfp_jobs ADD COLUMN IF NOT EXISTS job_kind text NOT NULL DEFAULT 'intake';
ALTER TABLE rfp_jobs DROP CONSTRAINT IF EXISTS ck_rfp_job_kind;
ALTER TABLE rfp_jobs ADD CONSTRAINT ck_rfp_job_kind CHECK (job_kind IN ('intake','response_generation'));
DROP INDEX IF EXISTS uq_rfp_active_job_per_ticket;
CREATE UNIQUE INDEX uq_rfp_active_job_per_ticket
  ON rfp_jobs(ticket_id,job_kind) WHERE status IN ('queued','running','retry_wait');

CREATE TABLE IF NOT EXISTS rfp_response_runs (
  run_id uuid PRIMARY KEY,
  ticket_id uuid NOT NULL REFERENCES rfp_tickets(ticket_id) ON DELETE RESTRICT,
  source_handoff_version text NOT NULL CHECK(source_handoff_version='rfp-intake-v1'),
  source_handoff_hash text NOT NULL CHECK(source_handoff_hash ~ '^[a-f0-9]{64}$'),
  carrier_model text NOT NULL DEFAULT 'unknown',
  carrier_confirmation_actor text,
  carrier_confirmation_source text NOT NULL DEFAULT 'legacy_migration',
  carrier_confirmed_at timestamptz,
  status text NOT NULL CHECK(status IN ('pending','generando_borrador','en_evaluación','ready_for_part3','needs_human_review','technical_failure')),
  max_business_iterations integer NOT NULL DEFAULT 3 CHECK(max_business_iterations=3),
  global_iteration_count integer NOT NULL DEFAULT 0 CHECK(global_iteration_count BETWEEN 0 AND 3),
  state_version integer NOT NULL DEFAULT 0 CHECK(state_version>=0),
  claim_token uuid,
  execution_epoch integer NOT NULL DEFAULT 0 CHECK(execution_epoch>=0),
  lease_until timestamptz,
  heartbeat_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(ticket_id,source_handoff_hash)
);
ALTER TABLE rfp_response_runs ADD COLUMN IF NOT EXISTS carrier_model text;
ALTER TABLE rfp_response_runs ADD COLUMN IF NOT EXISTS carrier_confirmation_actor text;
ALTER TABLE rfp_response_runs ADD COLUMN IF NOT EXISTS carrier_confirmation_source text;
ALTER TABLE rfp_response_runs ADD COLUMN IF NOT EXISTS carrier_confirmed_at timestamptz;
UPDATE rfp_response_runs SET
  carrier_model=COALESCE(carrier_model,'unknown'),
  carrier_confirmation_source=COALESCE(carrier_confirmation_source,'legacy_migration')
WHERE carrier_model IS NULL OR carrier_confirmation_source IS NULL;
ALTER TABLE rfp_response_runs ALTER COLUMN carrier_model SET DEFAULT 'unknown';
ALTER TABLE rfp_response_runs ALTER COLUMN carrier_model SET NOT NULL;
ALTER TABLE rfp_response_runs ALTER COLUMN carrier_confirmation_source SET DEFAULT 'legacy_migration';
ALTER TABLE rfp_response_runs ALTER COLUMN carrier_confirmation_source SET NOT NULL;
ALTER TABLE rfp_response_runs DROP CONSTRAINT IF EXISTS ck_rfp_response_carrier_model;
ALTER TABLE rfp_response_runs ADD CONSTRAINT ck_rfp_response_carrier_model CHECK (
  carrier_model IN ('unknown','trackflow_managed','customer_managed')
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_response_active_run
  ON rfp_response_runs(ticket_id)
  WHERE status IN ('pending','generando_borrador','en_evaluación');

CREATE TABLE IF NOT EXISTS rfp_response_sections (
  run_id uuid NOT NULL REFERENCES rfp_response_runs(run_id) ON DELETE RESTRICT,
  department_id text NOT NULL CHECK(department_id IN ('warehouse','lastmile','reverse')),
  status text NOT NULL CHECK(status IN ('pending','generando_borrador','en_evaluación','passed','needs_human_input','iteration_limit_reached','technical_failure')),
  iteration_count integer NOT NULL DEFAULT 0 CHECK(iteration_count BETWEEN 0 AND 3),
  final_draft_id uuid,
  final_evaluation_id uuid,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(run_id,department_id),
  CONSTRAINT rfp_response_section_terminal_shape CHECK (
    status NOT IN ('passed','needs_human_input','iteration_limit_reached') OR
    (iteration_count>=1 AND final_draft_id IS NOT NULL AND final_evaluation_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS rfp_response_drafts (
  draft_id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES rfp_response_runs(run_id) ON DELETE RESTRICT,
  scope_key text NOT NULL CHECK(scope_key IN ('warehouse','lastmile','reverse','proposal')),
  iteration_count integer NOT NULL CHECK(iteration_count BETWEEN 1 AND 3),
  contract_version text NOT NULL CHECK(contract_version IN ('rfp-department-draft-v1','rfp-global-commercial-terms-v1')),
  payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
  payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(run_id,scope_key,iteration_count),
  UNIQUE(run_id,draft_id)
);

CREATE TABLE IF NOT EXISTS rfp_response_previews (
  preview_id uuid PRIMARY KEY,
  draft_id uuid NOT NULL UNIQUE REFERENCES rfp_response_drafts(draft_id) ON DELETE RESTRICT,
  gate_status text NOT NULL CHECK(gate_status IN ('passed','blocked','indeterminate','technical_error')),
  safe_content text,
  reason_codes jsonb NOT NULL CHECK(jsonb_typeof(reason_codes)='array'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT rfp_response_preview_shape CHECK (
    (gate_status='passed' AND safe_content IS NOT NULL AND jsonb_array_length(reason_codes)=0) OR
    (gate_status<>'passed' AND safe_content IS NULL AND jsonb_array_length(reason_codes)>0)
  )
);

CREATE TABLE IF NOT EXISTS rfp_response_evaluator_results (
  evaluator_result_id uuid PRIMARY KEY,
  draft_id uuid NOT NULL REFERENCES rfp_response_drafts(draft_id) ON DELETE RESTRICT,
  evaluator text NOT NULL CHECK(evaluator IN ('readability','relevance','section_compliance','global_compliance')),
  contract_version text NOT NULL,
  payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
  payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(draft_id,evaluator)
);

CREATE TABLE IF NOT EXISTS rfp_response_evaluations (
  evaluation_id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES rfp_response_runs(run_id) ON DELETE RESTRICT,
  draft_id uuid NOT NULL UNIQUE REFERENCES rfp_response_drafts(draft_id) ON DELETE RESTRICT,
  scope_key text NOT NULL CHECK(scope_key IN ('warehouse','lastmile','reverse','proposal')),
  contract_version text NOT NULL CHECK(contract_version IN ('section-evaluation-v1','proposal-evaluation-v1')),
  overall_pass boolean NOT NULL,
  terminal_status text CHECK(terminal_status IS NULL OR terminal_status IN ('passed','needs_human_input','iteration_limit_reached','ready_for_part3','needs_human_review')),
  payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
  payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS rfp_response_execution_failures (
  failure_id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES rfp_response_runs(run_id) ON DELETE RESTRICT,
  ticket_id uuid NOT NULL REFERENCES rfp_tickets(ticket_id) ON DELETE RESTRICT,
  draft_id uuid REFERENCES rfp_response_drafts(draft_id) ON DELETE RESTRICT,
  evaluator text NOT NULL CHECK(evaluator IN ('department_generator','readability','relevance','section_compliance','global_compliance')),
  department text CHECK(department IS NULL OR department IN ('warehouse','lastmile','reverse')),
  infrastructure_attempts integer NOT NULL CHECK(infrastructure_attempts>=1),
  error_code text NOT NULL,
  diagnostic_reference text NOT NULL,
  terminal boolean NOT NULL,
  occurred_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE rfp_response_execution_failures
  ADD COLUMN IF NOT EXISTS department text;
ALTER TABLE rfp_response_execution_failures
  DROP CONSTRAINT IF EXISTS rfp_response_execution_failures_evaluator_check;
ALTER TABLE rfp_response_execution_failures
  DROP CONSTRAINT IF EXISTS ck_rfp_response_failure_evaluator;
ALTER TABLE rfp_response_execution_failures
  ADD CONSTRAINT ck_rfp_response_failure_evaluator CHECK (
    evaluator IN ('department_generator','readability','relevance','section_compliance','global_compliance')
  );
ALTER TABLE rfp_response_execution_failures
  DROP CONSTRAINT IF EXISTS ck_rfp_response_failure_department;
ALTER TABLE rfp_response_execution_failures
  ADD CONSTRAINT ck_rfp_response_failure_department CHECK (
    department IS NULL OR department IN ('warehouse','lastmile','reverse')
  );

CREATE TABLE IF NOT EXISTS rfp_response_llm_invocations (
  invocation_id uuid PRIMARY KEY,
  ticket_id uuid NOT NULL REFERENCES rfp_tickets(ticket_id) ON DELETE RESTRICT,
  run_id uuid NOT NULL REFERENCES rfp_response_runs(run_id) ON DELETE RESTRICT,
  artifact_id uuid NOT NULL,
  provider text NOT NULL CHECK(provider='fourgeeks_gateway'),
  model text NOT NULL,
  stage text NOT NULL CHECK(stage IN ('department_generation','relevance_evaluation')),
  department text NOT NULL CHECK(department IN ('warehouse','lastmile','reverse')),
  call_index integer NOT NULL CHECK(call_index>=1),
  provider_request_id text,
  latency_ms double precision NOT NULL CHECK(latency_ms>=0),
  estimated_input_tokens integer NOT NULL CHECK(estimated_input_tokens>=0),
  prompt_tokens integer CHECK(prompt_tokens IS NULL OR prompt_tokens>=0),
  completion_tokens integer CHECK(completion_tokens IS NULL OR completion_tokens>=0),
  total_tokens integer CHECK(total_tokens IS NULL OR total_tokens>=0),
  outcome text NOT NULL CHECK(outcome IN ('succeeded','technical_error','invalid_output')),
  error_category text,
  validation_path text,
  validation_error_type text,
  validation_received_type text,
  violated_constraint text,
  started_at timestamptz NOT NULL,
  finished_at timestamptz NOT NULL CHECK(finished_at>=started_at),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT rfp_response_llm_error_shape CHECK (
    (outcome='succeeded' AND error_category IS NULL) OR
    (outcome<>'succeeded' AND error_category IS NOT NULL)
  )
);

ALTER TABLE rfp_response_llm_invocations
  ADD COLUMN IF NOT EXISTS validation_path text,
  ADD COLUMN IF NOT EXISTS validation_error_type text,
  ADD COLUMN IF NOT EXISTS validation_received_type text,
  ADD COLUMN IF NOT EXISTS violated_constraint text;

CREATE TABLE IF NOT EXISTS rfp_response_handoffs (
  run_id uuid PRIMARY KEY REFERENCES rfp_response_runs(run_id) ON DELETE RESTRICT,
  ticket_id uuid NOT NULL REFERENCES rfp_tickets(ticket_id) ON DELETE RESTRICT,
  contract_version text NOT NULL CHECK(contract_version='rfp-response-v1'),
  outcome text NOT NULL CHECK(outcome IN ('ready_for_part3','needs_human_review')),
  payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
  payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(ticket_id)
);

CREATE INDEX IF NOT EXISTS ix_rfp_response_run_ticket ON rfp_response_runs(ticket_id,created_at DESC);
CREATE INDEX IF NOT EXISTS ix_rfp_response_draft_run ON rfp_response_drafts(run_id,scope_key,iteration_count);
CREATE INDEX IF NOT EXISTS ix_rfp_response_failure_run ON rfp_response_execution_failures(run_id,created_at);
CREATE INDEX IF NOT EXISTS ix_rfp_response_llm_run ON rfp_response_llm_invocations(run_id,created_at);

COMMIT;
