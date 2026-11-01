# DEV-53 job runner state service

This package owns only persistent state and acquisition for TrackFlow
background jobs. It is independent of FastAPI, cron, CSV export, and the weekly
pipeline.

Callers provide a SQLAlchemy PostgreSQL engine. `acquire_job_run()` creates a
`pending` row and moves it to `processing` in one transaction; callers may run
work only when the returned result is acquired. Terminal changes use
compare-and-set from `processing`.

Rollback and connection close are best-effort cleanup operations: a secondary
cleanup failure is logged without exception contents and cannot replace the
primary database failure. Once `commit()` returns successfully, a later close
failure triggers best-effort invalidation and does not erase the confirmed
acquired result. An exception raised by `commit()` remains a persistence failure
because its result is not assumed.

Acquisition persists `created_at` and `started_at` from the same UTC instant.
Terminal transitions clamp a regressed application clock to `started_at`, so
normal service operations cannot persist reversed timestamps. Error diagnostics
with recognized credential-bearing forms are replaced by a conservative generic
message; arbitrary unlabelled secrets cannot be detected reliably.

Provision `infra/sql/job_runs.sql` explicitly on the authorized operational
database before use. The partial unique indexes in that file are the sole
ownership mechanism. Abrupt process termination can leave `processing`
persistent; this package deliberately implements no expiry or automatic
takeover.
