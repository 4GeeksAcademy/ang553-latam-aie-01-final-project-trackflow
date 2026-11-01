# TrackFlow asynchronous task service

This service runs Celery tasks independently of FastAPI. DEV-55 Block A uses
Redis as both broker and result backend and wraps the existing weekly Prefect
pipeline without copying its business logic.

## Configuration

- `REDIS_URL`: Redis broker/result URL. Compose supplies
  `redis://redis:6379/0`; local commands default to `redis://localhost:6379/0`.
- The weekly pipeline task reconstructs its normal CLI runtime from
  `TRACKFLOW_PIPELINE_DATABASE_URL`, optional
  `TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL`,
  `TRACKFLOW_PIPELINE_DEPENDENCY_FINGERPRINT`,
  `TRACKFLOW_PIPELINE_RULES_VERSION`, and the legacy evidence variable when
  that runtime requires it. Values are never included in task messages.
- `FLOWER_BASIC_AUTH`: optional `user:password` protection. Compose binds
  Flower and Redis to loopback. Set authentication before using Flower in a
  shared environment.

## Start and inspect

The infrastructure is opt-in and does not change ordinary `docker compose up`
or the DEV-53 scheduler profile.

```powershell
docker compose --profile async-tasks up -d --build redis celery-worker flower
docker compose --profile async-tasks ps
docker compose --profile async-tasks logs celery-worker flower
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
docker compose --profile async-tasks stop flower celery-worker redis
```

`stop` preserves the named Redis volume. Do not use `down -v` when historical
QA data must be retained.

## Block A safety boundary

`trackflow.run_weekly_pipeline` accepts only `run_id` and `week_start`. It
reconstructs engines in the worker and invokes `pipelines.pipeline.run_weekly_pipeline`.
Do not enqueue it against persistent business data until Block B adds the
approved durable registry, outbox, claims, fencing, retry policy, and DLQ.

The Compose `api` service does not receive a queue URL in Block A because it
does not produce or inspect Celery tasks yet. Before enabling the Block B HTTP
producer, Compose must provide the API with the same internal
`REDIS_URL=redis://redis:6379/0` used by the worker and must order API queue
operations after the Redis healthcheck without making the ordinary non-queue
development profile start Redis unexpectedly.
