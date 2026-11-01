-- DEV-53 nightly-job execution history and ownership.
--
-- Provision explicitly on the authorized operational PostgreSQL database.
-- This file is not invoked by application imports or SQLModel.metadata.create_all().
-- IF NOT EXISTS is repeatable but does not reconcile an incompatible existing
-- object; inspect the table and indexes before applying it outside disposable QA.

CREATE TABLE IF NOT EXISTS job_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_name text NOT NULL,
    target_date date NOT NULL,
    status text NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CONSTRAINT job_runs_job_name_valid CHECK (
        char_length(job_name) BETWEEN 1 AND 100
        AND job_name = btrim(job_name)
    ),
    CONSTRAINT job_runs_status_valid CHECK (
        status IN ('pending', 'processing', 'completed', 'failed')
    ),
    CONSTRAINT job_runs_state_shape_valid CHECK (
        (
            status = 'pending'
            AND started_at IS NULL
            AND finished_at IS NULL
            AND error_message IS NULL
        )
        OR (
            status = 'processing'
            AND started_at IS NOT NULL
            AND finished_at IS NULL
            AND error_message IS NULL
        )
        OR (
            status = 'completed'
            AND started_at IS NOT NULL
            AND finished_at IS NOT NULL
            AND error_message IS NULL
        )
        OR (
            status = 'failed'
            AND started_at IS NOT NULL
            AND finished_at IS NOT NULL
            AND error_message IS NOT NULL
        )
    ),
    CONSTRAINT job_runs_finished_after_started CHECK (
        finished_at IS NULL OR finished_at >= started_at
    ),
    CONSTRAINT job_runs_started_after_created CHECK (
        started_at IS NULL OR started_at >= created_at
    )
);

-- Reconcile the additive timestamp invariant when this repeatable DDL is
-- applied to a pre-existing DEV-53 test table created by an earlier revision.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'job_runs_started_after_created'
          AND conrelid = 'job_runs'::regclass
    ) THEN
        ALTER TABLE job_runs
            ADD CONSTRAINT job_runs_started_after_created
            CHECK (started_at IS NULL OR started_at >= created_at);
    END IF;
END
$$;

-- One nightly job may process only one target date at a time.
CREATE UNIQUE INDEX IF NOT EXISTS job_runs_one_processing_per_job_idx
    ON job_runs (job_name)
    WHERE status = 'processing';

-- Retain the date claim when processing becomes completed. Failed attempts are
-- deliberately excluded so their history remains and the date can be retried.
CREATE UNIQUE INDEX IF NOT EXISTS job_runs_target_claim_idx
    ON job_runs (job_name, target_date)
    WHERE status IN ('processing', 'completed');

CREATE INDEX IF NOT EXISTS job_runs_history_idx
    ON job_runs (job_name, target_date, created_at DESC, id DESC);
