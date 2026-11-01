-- DEV-55 durable asynchronous-task contracts for the reporting database.
--
-- Prerequisite: infra/sql/reporting_pipeline_control.sql has been reviewed and
-- applied to the same authorized PostgreSQL database. This file is never
-- executed by FastAPI startup. Do not apply it to production/Supabase without
-- a separate human authorization.
--
-- IF NOT EXISTS is non-destructive, but it does not reconcile an existing
-- object. Compare the PostgreSQL catalog with this file before reapplying it.

CREATE SCHEMA IF NOT EXISTS reporting;

CREATE TABLE IF NOT EXISTS reporting.async_tasks (
    task_id uuid PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE,
    requested_by_user_id text NOT NULL,
    task_name text NOT NULL,
    week_start date NOT NULL,
    execution_state text NOT NULL DEFAULT 'accepted',
    dispatch_status text NOT NULL DEFAULT 'pending',
    celery_state text,
    attempt_count smallint NOT NULL DEFAULT 0,
    result_summary jsonb,
    error_code text,
    error_message text,
    pipeline_status text,
    published boolean,
    attempt_id uuid,
    attempt_number smallint,
    claim_token uuid,
    execution_epoch bigint NOT NULL DEFAULT 0,
    execution_phase text NOT NULL DEFAULT 'accepted',
    claim_expires_at timestamptz,
    heartbeat_at timestamptz,
    next_attempt_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    finished_at timestamptz,
    CONSTRAINT async_tasks_run_fk
        FOREIGN KEY (run_id)
        REFERENCES reporting.pipeline_publication_runs (run_id),
    CONSTRAINT async_tasks_task_name_ck
        CHECK (task_name = 'trackflow.run_weekly_pipeline'),
    CONSTRAINT async_tasks_execution_state_ck CHECK (execution_state IN (
        'accepted', 'queued', 'claimed', 'running', 'retry_wait', 'succeeded', 'failed',
        'dead_lettered', 'failure_pending_dlq', 'reconciliation_required'
    )),
    CONSTRAINT async_tasks_dispatch_status_ck CHECK (dispatch_status IN (
        'pending', 'publishing', 'retry_wait', 'published', 'publication_uncertain',
        'reconciliation_required'
    )),
    CONSTRAINT async_tasks_celery_state_ck CHECK (
        celery_state IS NULL OR celery_state IN (
            'PENDING', 'RECEIVED', 'STARTED', 'RETRY', 'SUCCESS', 'FAILURE', 'REVOKED'
        )
    ),
    CONSTRAINT async_tasks_attempts_ck CHECK (
        attempt_count BETWEEN 0 AND 3
        AND (attempt_number IS NULL OR attempt_number BETWEEN 1 AND 3)
    ),
    CONSTRAINT async_tasks_epoch_ck CHECK (execution_epoch >= 0),
    CONSTRAINT async_tasks_phase_ck CHECK (
        execution_phase IN ('none', 'accepted', 'preflight', 'prefect_started', 'commit', 'terminal')
    ),
    CONSTRAINT async_tasks_preflight_claim_ck CHECK (
        execution_phase <> 'preflight'
        OR (
            execution_state = 'claimed' AND attempt_id IS NOT NULL
            AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL
            AND heartbeat_at IS NOT NULL
        )
    ),
    CONSTRAINT async_tasks_running_claim_ck CHECK (
        execution_phase <> 'prefect_started'
        OR (
            execution_state = 'running' AND attempt_id IS NOT NULL
            AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL
            AND heartbeat_at IS NOT NULL
        )
    ),
    CONSTRAINT async_tasks_result_summary_ck CHECK (
        result_summary IS NULL OR jsonb_typeof(result_summary) = 'object'
    ),
    CONSTRAINT async_tasks_safe_error_ck CHECK (
        (error_code IS NULL OR length(error_code) <= 128)
        AND (error_message IS NULL OR length(error_message) <= 512)
    ),
    CONSTRAINT async_tasks_timestamps_ck CHECK (
        finished_at IS NULL OR finished_at >= created_at
    )
);

CREATE INDEX IF NOT EXISTS async_tasks_state_updated_idx
    ON reporting.async_tasks (execution_state, updated_at, task_id);

CREATE INDEX IF NOT EXISTS async_tasks_requester_created_idx
    ON reporting.async_tasks (requested_by_user_id, created_at DESC, task_id);

CREATE INDEX IF NOT EXISTS async_tasks_expired_claim_idx
    ON reporting.async_tasks (claim_expires_at, task_id)
    WHERE execution_state = 'claimed';

CREATE INDEX IF NOT EXISTS async_tasks_retry_ready_idx
    ON reporting.async_tasks (next_attempt_at, task_id)
    WHERE execution_state = 'retry_wait';

CREATE TABLE IF NOT EXISTS reporting.task_dispatch_outbox (
    task_id uuid PRIMARY KEY,
    payload jsonb NOT NULL,
    dispatch_status text NOT NULL DEFAULT 'pending',
    publish_attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    relay_owner text,
    relay_token uuid,
    relay_lease_expires_at timestamptz,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    published_at timestamptz,
    CONSTRAINT task_dispatch_outbox_task_fk
        FOREIGN KEY (task_id)
        REFERENCES reporting.async_tasks (task_id),
    CONSTRAINT task_dispatch_outbox_payload_ck
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT task_dispatch_outbox_status_ck CHECK (dispatch_status IN (
        'pending', 'publishing', 'retry_wait', 'published', 'publication_uncertain',
        'reconciliation_required'
    )),
    CONSTRAINT task_dispatch_outbox_attempts_ck CHECK (publish_attempts >= 0),
    CONSTRAINT task_dispatch_outbox_claim_tuple_ck CHECK (
        (
            relay_owner IS NULL AND relay_token IS NULL
            AND relay_lease_expires_at IS NULL
        )
        OR (
            relay_owner IS NOT NULL AND relay_token IS NOT NULL
            AND relay_lease_expires_at IS NOT NULL
        )
    ),
    CONSTRAINT task_dispatch_outbox_safe_error_ck CHECK (
        last_error_code IS NULL OR length(last_error_code) <= 128
    ),
    CONSTRAINT task_dispatch_outbox_published_ck CHECK (
        published_at IS NULL OR dispatch_status = 'published'
    )
);

CREATE INDEX IF NOT EXISTS task_dispatch_outbox_ready_idx
    ON reporting.task_dispatch_outbox (next_attempt_at, created_at, task_id)
    WHERE dispatch_status IN ('pending', 'retry_wait');

CREATE INDEX IF NOT EXISTS task_dispatch_outbox_expired_claim_idx
    ON reporting.task_dispatch_outbox (relay_lease_expires_at, task_id)
    WHERE dispatch_status = 'publishing';

CREATE TABLE IF NOT EXISTS reporting.task_dead_letters (
    task_id uuid PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE,
    attempt_number smallint NOT NULL,
    terminal_reason text NOT NULL,
    error_code text NOT NULL,
    error_message text NOT NULL,
    failed_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT task_dead_letters_task_fk
        FOREIGN KEY (task_id)
        REFERENCES reporting.async_tasks (task_id),
    CONSTRAINT task_dead_letters_run_fk
        FOREIGN KEY (run_id)
        REFERENCES reporting.pipeline_publication_runs (run_id),
    CONSTRAINT task_dead_letters_attempt_ck CHECK (attempt_number BETWEEN 1 AND 3),
    CONSTRAINT task_dead_letters_reason_ck CHECK (
        terminal_reason IN ('retries_exhausted', 'non_retryable', 'pipeline_terminal')
    ),
    CONSTRAINT task_dead_letters_safe_error_ck CHECK (
        length(error_code) BETWEEN 1 AND 128
        AND length(error_message) BETWEEN 1 AND 512
    )
);

CREATE INDEX IF NOT EXISTS task_dead_letters_failed_idx
    ON reporting.task_dead_letters (failed_at DESC, task_id);
