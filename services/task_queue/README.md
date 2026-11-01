# TrackFlow asynchronous task service

This service runs Celery tasks independently of FastAPI. DEV-55 uses Redis as
broker/result backend, PostgreSQL for durable acceptance and recovery, and the
existing weekly Prefect pipeline without copying its business logic.

## Configuration

- `REDIS_URL`: Redis broker/result URL. Compose supplies
  `redis://redis:6379/0`; local commands default to `redis://localhost:6379/0`.
- The weekly pipeline task reconstructs its normal CLI runtime from
  `TRACKFLOW_PIPELINE_DATABASE_URL`, optional
  `TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL`,
  `TRACKFLOW_PIPELINE_DEPENDENCY_FINGERPRINT`,
  `TRACKFLOW_PIPELINE_RULES_VERSION`, and the legacy evidence variable when
  that runtime requires it. Values are never included in task messages.
- `FLOWER_BASIC_AUTH`: required `user:password` protection. Compose binds
  Flower and Redis to loopback. The Compose Flower command refuses to start
  when this value is empty.
- `TRACKFLOW_TASK_SOFT_TIME_LIMIT_SECONDS`,
  `TRACKFLOW_TASK_TIME_LIMIT_SECONDS`,
  `TRACKFLOW_VISIBILITY_TIMEOUT_SECONDS`, and
  `TRACKFLOW_RESULT_EXPIRES_SECONDS`: positive integer limits derived from a
  measured isolated-pipeline run. Compose deliberately supplies no guessed
  defaults and refuses to start the worker while any value is empty. The hard
  limit must exceed the soft limit, the visibility timeout must exceed the
  hard limit, and result retention must exceed the hard limit.
- `TRACKFLOW_TASK_CLAIM_LEASE_SECONDS`, `TRACKFLOW_TASK_HEARTBEAT_SECONDS`, and
  `TRACKFLOW_TASK_RETRY_MAX_BACKOFF_SECONDS`: positive, operator-approved
  claim/heartbeat and retry bounds. Heartbeat must be shorter than the lease;
  an expired lease after Prefect starts requires reconciliation and never
  authorizes a replacement execution.
- `TRACKFLOW_WORKER_SHUTDOWN_GRACE_SECONDS`: required by the Compose worker
  command and must be at least the configured hard time limit. The Compose
  interpolation fallback exists only so profile-free configuration can be
  parsed; the worker refuses to run when the variable is absent. QA measured a
  71.106-second cold positive execution and used 180 seconds, matching its QA
  hard limit. Production requires its own duration measurements.
- `TRACKFLOW_RELAY_POLL_SECONDS`, `TRACKFLOW_RELAY_LEASE_SECONDS`,
  `TRACKFLOW_RELAY_MAX_BACKOFF_SECONDS`, and
  `TRACKFLOW_RELAY_MAX_PUBLISH_ATTEMPTS`: positive relay controls. Exhaustion
  moves the durable dispatch to `reconciliation_required`.

The API receives the same internal `REDIS_URL` for configuration coherence,
but request acceptance is a PostgreSQL transaction and does not contact Redis.
The independent relay is the only HTTP-to-broker delivery path.

## PostgreSQL prerequisite

Review and explicitly apply `infra/sql/reporting_async_tasks.sql` to an
authorized reporting database after `infra/sql/reporting_pipeline_control.sql`.
FastAPI and the relay never create these tables at startup. Do not apply this
DDL to Supabase/production as part of application startup or automated tests.

## Start and inspect

The infrastructure is opt-in and does not change ordinary `docker compose up`
or the DEV-53 scheduler profile.

```powershell
$env:FLOWER_BASIC_AUTH = "operator:<secret>"
$env:TRACKFLOW_TASK_SOFT_TIME_LIMIT_SECONDS = "<measured-soft-limit>"
$env:TRACKFLOW_TASK_TIME_LIMIT_SECONDS = "<measured-hard-limit>"
$env:TRACKFLOW_VISIBILITY_TIMEOUT_SECONDS = "<validated-visibility-timeout>"
$env:TRACKFLOW_RESULT_EXPIRES_SECONDS = "<validated-result-retention>"
$env:TRACKFLOW_TASK_CLAIM_LEASE_SECONDS = "<validated-preflight-lease>"
$env:TRACKFLOW_TASK_HEARTBEAT_SECONDS = "<validated-heartbeat-interval>"
$env:TRACKFLOW_TASK_RETRY_MAX_BACKOFF_SECONDS = "<validated-retry-cap>"
$env:TRACKFLOW_WORKER_SHUTDOWN_GRACE_SECONDS = "<at-least-hard-time-limit>"
$env:TRACKFLOW_RELAY_POLL_SECONDS = "<validated-poll-interval>"
$env:TRACKFLOW_RELAY_LEASE_SECONDS = "<validated-relay-lease>"
$env:TRACKFLOW_RELAY_MAX_BACKOFF_SECONDS = "<validated-relay-backoff-cap>"
$env:TRACKFLOW_RELAY_MAX_PUBLISH_ATTEMPTS = "<approved-attempt-limit>"
docker compose --profile async-tasks up -d --build redis celery-worker task-relay flower
docker compose --profile async-tasks ps
docker compose --profile async-tasks logs celery-worker task-relay flower
```

- Redis: `127.0.0.1:6379`
- Flower: `http://127.0.0.1:5555`

Verify the Redis policy and AOF without printing configuration secrets:

```powershell
docker compose --profile async-tasks exec redis redis-cli CONFIG GET maxmemory-policy
docker compose --profile async-tasks exec redis redis-cli CONFIG GET appendonly
```

## Stop

```powershell
docker compose --profile async-tasks stop flower task-relay celery-worker redis
```

`stop` preserves the named Redis volume. Do not use `down -v` when historical
QA data must be retained.

## Durable relay and execution boundary

`trackflow.run_weekly_pipeline` accepts only `run_id` and `week_start`. It
reconstructs engines in the worker and invokes `pipelines.pipeline.run_weekly_pipeline`.
`python -m services.task_queue.relay` runs independently of FastAPI. It claims
pending outbox rows, publishes the lightweight payload with the stable
`task_id`, and records the outcome with its relay token. A Redis failure after
the PostgreSQL acceptance therefore leaves recoverable durable evidence.

The worker must acquire its PostgreSQL claim before entering Prefect. Celery
redelivery is expected and is not an exactly-once guarantee; stale workers are
fenced by the persisted claim token and execution epoch. Ambiguous publication
or business effects require reconciliation instead of blind re-execution.

The relay, worker and Flower share `REDIS_URL=redis://redis:6379/0`. Redis and
the three consumers remain opt-in under `async-tasks`; ordinary
`docker compose up` does not start Redis unexpectedly. The API has no
`depends_on` relationship with Redis, preserving durable acceptance while the
broker is stopped.
