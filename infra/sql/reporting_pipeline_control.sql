-- TrackFlow operational persistence for weekly publication runs.
-- This script does not alter reporting.weekly_warehouse_client_performance.
-- It is not invoked by application imports or SQLModel.metadata.create_all().
-- IF NOT EXISTS is repeatable and non-destructive. It does not reconcile an
-- existing object. Inspect columns, types, keys, and checks before use.
-- Apply only to an authorized non-production PostgreSQL server.
-- Do not DROP, truncate, or treat this script as a concurrency proof.

CREATE SCHEMA IF NOT EXISTS reporting;

CREATE TABLE IF NOT EXISTS reporting.pipeline_publication_runs (
    run_id uuid PRIMARY KEY,
    pipeline_name text NOT NULL,
    target_week_start date NOT NULL,
    trigger_type text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    finished_at timestamptz,
    status text NOT NULL,
    stage_reached text,
    records_processed integer,
    aggregates_calculated integer,
    rows_inserted integer,
    rows_updated integer,
    rows_unchanged integer,
    error_code text,
    error_message text,
    block_reason text,
    commit_result text NOT NULL DEFAULT 'not_attempted',
    prefect_flow_run_id text,
    prefect_task_run_id text,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (status IN (
        'pending',
        'running',
        'succeeded',
        'succeeded_noop',
        'failed_retryable',
        'blocked_quality',
        'superseded',
        'skipped_in_progress'
    )),
    CHECK (commit_result IN (
        'not_attempted',
        'uncommitted',
        'uncertain',
        'rolled_back',
        'technically_observed'
    )),
    CHECK (records_processed IS NULL OR records_processed >= 0),
    CHECK (aggregates_calculated IS NULL OR aggregates_calculated >= 0),
    CHECK (rows_inserted IS NULL OR rows_inserted >= 0),
    CHECK (rows_updated IS NULL OR rows_updated >= 0),
    CHECK (rows_unchanged IS NULL OR rows_unchanged >= 0),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE INDEX IF NOT EXISTS pipeline_publication_runs_week_started_idx
    ON reporting.pipeline_publication_runs (pipeline_name, target_week_start, started_at DESC, run_id DESC);

CREATE TABLE IF NOT EXISTS reporting.pipeline_publication_control (
    week_start date PRIMARY KEY,
    owner_run_id uuid,
    lease_acquired_at timestamptz,
    lease_expires_at timestamptz,
    fencing_token bigint NOT NULL DEFAULT 0,
    last_valid_publication_run_id uuid,
    last_valid_published_at timestamptz,
    dependency_fingerprint text,
    published_row_count integer,
    rules_version text,
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK (fencing_token >= 0),
    CHECK (published_row_count IS NULL OR published_row_count >= 0),
    CHECK (
        lease_expires_at IS NULL
        OR lease_acquired_at IS NULL
        OR lease_expires_at > lease_acquired_at
    ),
    CHECK (
        (
            owner_run_id IS NULL
            AND lease_acquired_at IS NULL
            AND lease_expires_at IS NULL
        )
        OR (
            owner_run_id IS NOT NULL
            AND lease_acquired_at IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
    ),
    CHECK (
        (last_valid_publication_run_id IS NULL AND last_valid_published_at IS NULL)
        OR (last_valid_publication_run_id IS NOT NULL AND last_valid_published_at IS NOT NULL)
    ),
    FOREIGN KEY (owner_run_id)
        REFERENCES reporting.pipeline_publication_runs (run_id),
    FOREIGN KEY (last_valid_publication_run_id)
        REFERENCES reporting.pipeline_publication_runs (run_id)
);
