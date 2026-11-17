-- Explicit TrackFlow RFP schema. Apply only to an authorized isolated operational database.
BEGIN;
CREATE TABLE IF NOT EXISTS rfp_tickets (
 ticket_id uuid PRIMARY KEY, rfp_id text,
 status text NOT NULL CHECK(status IN ('analizando','descartado','analisis_completo')),
 raw_pdf_path text NOT NULL, markdown_path text, discard_reason_code text, discard_summary text,
 uploaded_by_subject text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CONSTRAINT rfp_ticket_discard_shape_ck CHECK ((status='descartado' AND discard_reason_code IS NOT NULL) OR (status<>'descartado' AND discard_reason_code IS NULL AND discard_summary IS NULL)));
CREATE TABLE IF NOT EXISTS rfp_metadata (
 ticket_id uuid PRIMARY KEY REFERENCES rfp_tickets(ticket_id) ON DELETE RESTRICT, client_name text,
 client_country_code text CHECK(client_country_code IS NULL OR client_country_code IN ('ES','US')), client_country_original text,
 operation_country_codes jsonb, currency text CHECK(currency IS NULL OR currency IN ('EUR','USD')),
 services_requested jsonb NOT NULL CHECK(jsonb_typeof(services_requested)='array'), monthly_volume_value numeric,
 monthly_volume_unit text, monthly_volume_source_ref jsonb, deadline date, onboarding_target text, budget_range jsonb,
 departments_needed jsonb NOT NULL CHECK(jsonb_typeof(departments_needed)='array'),
 readability_metrics jsonb NOT NULL CHECK(jsonb_typeof(readability_metrics)='object'), conversion_metrics jsonb NOT NULL CHECK(jsonb_typeof(conversion_metrics)='object'));
CREATE TABLE IF NOT EXISTS rfp_department_sections (
 ticket_id uuid NOT NULL REFERENCES rfp_tickets(ticket_id) ON DELETE RESTRICT,
 department_id text NOT NULL CHECK(department_id IN ('warehouse','lastmile','reverse')),
 key_aspects jsonb NOT NULL CHECK(jsonb_typeof(key_aspects)='array'), open_questions jsonb NOT NULL CHECK(jsonb_typeof(open_questions)='array'),
 source_references jsonb NOT NULL CHECK(jsonb_typeof(source_references)='array'), detected_conflicts jsonb NOT NULL CHECK(jsonb_typeof(detected_conflicts)='array'), PRIMARY KEY(ticket_id,department_id));
CREATE TABLE IF NOT EXISTS rfp_handoffs (
 ticket_id uuid PRIMARY KEY REFERENCES rfp_tickets(ticket_id) ON DELETE RESTRICT,
 contract_version text NOT NULL CHECK(contract_version='rfp-intake-v1'), summary_for_sales text NOT NULL,
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'), created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS rfp_jobs (
 job_id uuid PRIMARY KEY, ticket_id uuid NOT NULL REFERENCES rfp_tickets(ticket_id) ON DELETE RESTRICT,
 status text NOT NULL CHECK(status IN ('queued','running','retry_wait','succeeded','failed','dead_letter')),
 attempt_count integer NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 3), max_attempts integer NOT NULL DEFAULT 3 CHECK(max_attempts=3),
 next_attempt_at timestamptz, claim_token uuid, execution_epoch integer NOT NULL DEFAULT 0 CHECK(execution_epoch>=0),
 lease_until timestamptz, heartbeat_at timestamptz, last_error_code text, last_error_summary text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE UNIQUE INDEX IF NOT EXISTS uq_rfp_active_job_per_ticket ON rfp_jobs(ticket_id) WHERE status IN ('queued','running','retry_wait');
CREATE INDEX IF NOT EXISTS ix_rfp_job_ticket ON rfp_jobs(ticket_id,created_at DESC);
CREATE TABLE IF NOT EXISTS rfp_outbox (
 outbox_id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES rfp_jobs(job_id) ON DELETE RESTRICT,
 attempt_no integer NOT NULL CHECK(attempt_no BETWEEN 1 AND 3), status text NOT NULL CHECK(status IN ('pending','published','publish_failed')),
 publish_attempts integer NOT NULL DEFAULT 0 CHECK(publish_attempts BETWEEN 0 AND 3), next_publish_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 lease_token uuid, lease_until timestamptz, last_error_code text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(job_id,attempt_no));
CREATE INDEX IF NOT EXISTS ix_rfp_outbox_ready ON rfp_outbox(status,next_publish_at);
COMMIT;
