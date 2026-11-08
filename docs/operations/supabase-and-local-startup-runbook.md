# TrackFlow Supabase and local startup runbook

## Purpose and verified architecture

TrackFlow uses one managed Supabase project and two PostgreSQL databases on the
same cluster:

- `postgres` is the operational database. It owns inventory, operational
  outbox/checkpoint state, telemetry and nightly-export state.
- `trackflow_reporting` is the analytical database. It owns the reporting
  schema, weekly pipeline history and published analytical results.

Local development uses the Supabase Session Pooler on port `5432`. This was
chosen because the direct endpoint was not reliably reachable from the original
Codespaces IPv4 environment. Both databases use the same PostgreSQL role,
project reference, pooler host and role password; only the database name differs.

Do not replace the project reference with the project display name or a literal
such as `PROJECT_ID`.

## Connection and component matrix

| Component | Variable | Database | Main objects and operations | Normal local state |
|---|---|---|---|---|
| FastAPI inventory | `DATABASE_URL` | `postgres` | `sku`, `stockentry`, `stockexit`, commercial orders, physical counts and threshold checkpoints | Active in `api` |
| API telemetry | `DATABASE_URL` | `postgres` | `telemetry_events`, append-only event persistence and reports | Active in `api` |
| Nightly export | `DATABASE_URL` | `postgres` | `telemetry_events`, `job_runs`, CSV export | Scheduler profile stopped by default |
| Reporting API | `TRACKFLOW_REPORTING_DATABASE_URL` | `trackflow_reporting` | `reporting.pipeline_publication_*`, weekly KPI reads and accepted pipeline runs | Router active; async execution stopped |
| Weekly pipeline destination | `TRACKFLOW_PIPELINE_DATABASE_URL` | `trackflow_reporting` | Relayed telemetry, run history, controls and KPI publication | Invoked only explicitly or by worker |
| Weekly pipeline source | `TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL` | `postgres` | Operational outbox, inventory and order evidence | Required for dual-engine CLI/worker behavior |
| Celery worker | Pipeline variables plus `TRACKFLOW_REPORTING_DATABASE_URL` | Both | Durable claim, pipeline execution and status | `async-tasks` profile stopped by default |
| Task relay / DLQ | `TRACKFLOW_REPORTING_DATABASE_URL` | `trackflow_reporting` | Async task outbox and dead letters | `async-tasks` profile stopped by default |

`TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL` is optional in the CLI contract. If it
is absent, the CLI uses its legacy single-database compatibility path. The
FastAPI reporting path instead falls back to `DATABASE_URL` as its operational
source. Keep the variable configured for the intended dual-engine architecture.

Database separation is verified from the live PostgreSQL identities: server,
port, `current_database()` and database OID. Textually different URLs alone are
not sufficient evidence.

## Current provisioning limits

The following state was verified on 2026-11-07 without running a pipeline:

- Operational inventory and telemetry tables exist and can be read.
- A telemetry insert inside an isolated transaction was visible before rollback
  and absent afterwards.
- The reporting database contains `public.telemetry_events` and the three core
  reporting tables.
- Two historical pipeline runs exist with `blocked_quality`; no valid weekly
  publication is available, so the weekly reporting endpoint correctly returns
  `404`.
- The optional async tables (`reporting.async_tasks`,
  `reporting.task_dispatch_outbox`, `reporting.task_dead_letters`) are not
  provisioned. Do not start the `async-tasks` profile until their authorized DDL
  has been reviewed and applied separately.

No scheduler, worker, relay or publication job was started during verification.

## Daily Windows startup

### 1. Open the local tools

1. Start Docker Desktop and wait until its engine reports that it is running.
2. Open the repository in VS Code.
3. Open PowerShell in the repository root:

```powershell
Set-Location 'C:\angel\ang553-latam-aie-01-final-project-trackflow'
git branch --show-current
docker info | Out-Null
```

The branch for the current milestone is `feature/rag-knowledge-base`.

### 2. Validate private configuration without printing it

Never run `docker compose config` without `--quiet`; expanded Compose output can
contain secrets.

```powershell
git check-ignore -q .env
docker compose --profile rag config --quiet
```

The four PostgreSQL URLs must share the project reference, Session Pooler host,
port `5432`, pooler user and password. Their database names must be:

- `DATABASE_URL` -> `postgres`
- `TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL` -> `postgres`
- `TRACKFLOW_PIPELINE_DATABASE_URL` -> `trackflow_reporting`
- `TRACKFLOW_REPORTING_DATABASE_URL` -> `trackflow_reporting`

`JWT_SECRET_KEY` must be one parseable assignment. Do not echo any of these
values.

### 3. Start only the daily services

```powershell
docker compose --profile rag up -d api interfaces qdrant
docker compose ps
```

Do not add `scheduler` or `async-tasks` to normal daily startup. Those profiles
can execute or enqueue real work and require separate authorization and
provisioning.

### 4. Verify the applications

```powershell
(Invoke-WebRequest http://localhost:8000/health -UseBasicParsing).StatusCode
(Invoke-WebRequest http://localhost:3000/ -UseBasicParsing).StatusCode
(Invoke-WebRequest http://localhost:3001/login -UseBasicParsing).StatusCode
(Invoke-WebRequest http://127.0.0.1:6333/collections/trackflow_knowledge -UseBasicParsing).StatusCode
```

All four checks should return `200`. Open the Backoffice at
`http://localhost:3001/login`, sign in with an existing authorized account, and
open `http://localhost:3001/backoffice/knowledge-base`.

Do not submit a Knowledge Base question when the purpose is only startup QA;
generation consumes the external gateway.

### 5. Verify PostgreSQL without exposing URLs

Use the project's SQLAlchemy URL normalizer and print only booleans or database
names. Never print the engine URL or an exception body containing connection
details. The safe checks are:

- `SELECT 1`
- `SELECT current_database()`
- server/port/database OID identity comparison

Expected result: both connections succeed, the server identity matches, the
database names differ and the OIDs differ.

### 6. Verify Qdrant persistence

The collection must remain:

- name: `trackflow_knowledge`
- points: `14`
- vector size: `1024`
- distance: `Cosine`

The Qdrant service uses a named Docker volume. `restart`, `stop` and ordinary
`up -d` preserve it. Never use `docker compose down -v` for routine operation.

## Restart and configuration changes

Restart the current containers without changing their stored environment:

```powershell
docker compose restart api interfaces qdrant
```

A restart does **not** reload changes made to `.env`. After an authorized `.env`
change, validate it and recreate only the consumers that need the new value:

```powershell
docker compose --profile rag config --quiet
docker compose up -d --force-recreate --no-deps api
```

Recreate `scheduler`, `celery-worker` or `task-relay` only when those optional
services are already in use and their execution is authorized. Never recreate
Qdrant merely to apply a PostgreSQL or JWT change.

## Port 8000 conflicts

Only Docker should serve the normal local API. Check listeners with:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object LocalAddress, OwningProcess
Get-CimInstance Win32_Process -Filter "ProcessId=<PID>" |
  Select-Object ProcessId, Name, CommandLine
```

If `127.0.0.1:8000` exposes fewer routes than `localhost:8000`, look for an old
host Uvicorn process. Stop it only after confirming its command is the obsolete
TrackFlow `services.api.main` process. During the 2026-11-07 recovery, an old
Python/Uvicorn listener owned IPv4 while Docker owned IPv6; stopping the old
process restored the same 31-route API on IPv4, IPv6 and `localhost`.

## Correct shutdown

```powershell
docker compose stop api interfaces qdrant
```

This preserves named volumes. Use `docker compose ps --all` to distinguish
stopped optional services from services that never existed locally.

## Password and JWT recovery

For a Supabase password rotation:

1. Copy the Session Pooler components from the private Supabase Connect panel.
2. Replace the password in all four URLs with the same current role password.
3. Percent-encode reserved password characters exactly once.
4. Preserve `postgres` versus `trackflow_reporting` as documented above.
5. Validate both databases before recreating any service.

Do not use the project display name where Supabase expects its project
reference. Do not leave `PROJECT_ID`, password placeholders or duplicate URL
assignments in `.env`.

For JWT failures, first validate that `JWT_SECRET_KEY` is one syntactically valid
assignment. Preserve the active secret whenever possible so existing sessions
remain valid. A secret representation containing quotes or newlines must be
escaped into one Compose-compatible assignment; never print it while repairing
the file.

## Security rules

- `.env` must remain ignored and untracked.
- Never print URLs, tokens, passwords, JWT values or expanded Compose config.
- Do not put private values in command arguments, documentation or temporary
  diagnostic scripts.
- Do not run migrations, DDL or production jobs as part of startup.
- Do not start optional profiles merely to prove that their containers start.
- Do not call the 4Geeks gateway during infrastructure-only verification.

## Verified limitations

- Optional async PostgreSQL tables are absent, so the async profile is not ready
  for functional execution.
- Reporting has historical blocked runs but no current valid publication.
- The broad offline QA run passed 486 tests and skipped two; one CORS preflight
  assertion failed because `Access-Control-Expose-Headers` was absent on the
  OPTIONS response.
- `tests/test_inventory_client_ids.py` fails during collection under Python
  3.13 because of its dynamic-module/dataclass loader, so it was not counted as
  executed QA.
- Qdrant's 14 IDs and payloads exactly match the chunks produced by the current
  corpus. A versioned per-document byte-hash baseline is not present in the
  repository, so historical byte-for-byte identity cannot be independently
  reconstructed from Git while the corpus remains untracked.
