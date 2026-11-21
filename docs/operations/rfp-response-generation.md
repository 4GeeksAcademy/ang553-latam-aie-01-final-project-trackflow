# RFP response generation runbook (Hito 9 / Parte 2)

This runbook describes the supported operating boundary for TrackFlow RFP
response generation. It does not authorize Parte 3 approval, signature or final
document production.

## Architecture and contract boundary

Parte 1 receives and analyzes the PDF, then persists the immutable
`rfp-intake-v1` handoff. Parte 2 never reparses the PDF. An administrator starts
one response run and explicitly confirms the carrier model as `unknown`,
`trackflow_managed` or `customer_managed`.

FastAPI atomically creates the response run, job and outbox record. The existing
relay routes `response_generation` to `trackflow.process_rfp_response`; the
existing Redis broker and Celery worker execute the LangGraph workflow. Each
department generates and evaluates independently. PostgreSQL persists drafts,
individual evaluator results, reduced evaluations, previews, operational
failures and safe LLM telemetry. The final durable contract is
`rfp-response-v1`.

The handoff stores identifiers for every final department draft and evaluation,
plus the global draft and evaluation. Parte 3 can therefore recover the exact
artifacts; a terminal technical failure never produces a valid handoff.

## Configuration

Keep secrets in a private environment file or secret manager. Never commit or
print `FOUR_GEEKS_API_KEY`, database credentials, JWT secrets or administrative
passwords.

Common runtime variables:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL database containing Parte 1 and Parte 2 tables. |
| `REDIS_URL` | Existing Celery broker/result backend. |
| `RFP_RESPONSE_LLM_MODE` | `deterministic` or `fourgeeks`. |
| `FOUR_GEEKS_BASE_URL` | Credential-free HTTPS Gateway base URL. |
| `FOUR_GEEKS_API_KEY` | Required only in `fourgeeks` mode. |
| `RAG_GENERATION_MODEL` | Selected generation model. |
| `RFP_RESPONSE_LLM_MAX_CALLS` | Global call budget shared by all branches. Default: `18`. |
| `RFP_RESPONSE_LLM_MAX_INPUT_TOKENS` | Maximum estimated input per request. Default: `2500`. |
| `RFP_RESPONSE_LLM_MAX_OUTPUT_TOKENS` | Maximum output budget per request. Default: `3000`. |
| `RFP_RESPONSE_LLM_MAX_CONCURRENCY` | Maximum concurrent Gateway calls. Default: `2`. |
| `RFP_RESPONSE_LLM_TIMEOUT_SECONDS` | HTTP timeout per call. Default: `30`. |
| `RFP_RESPONSE_LLM_REQUEST_INTERVAL_SECONDS` | Minimum pacing interval. Default: `2`. |
| `RFP_LEASE_SECONDS` | Response-run execution lease. |
| `RFP_HEARTBEAT_SECONDS` | Lease heartbeat interval; must be shorter than the lease. |
| `RFP_MAX_ATTEMPTS` | Infrastructure attempt limit; it does not count business generations. |

`RFP_RESPONSE_LLM_MAX_OUTPUT_TOKENS=3000` is the validated **máximo por
solicitud** for the selected FourGeeks model. It is a ceiling passed to the
Gateway, not a requirement that a response consume that many tokens. Do not
change the model, schema or budgets without a separately reviewed validation.

### Provider modes

- `deterministic` is the default offline QA provider. It performs no external
  LLM calls and must never be presented as FourGeeks validation.
- `fourgeeks` requires the HTTPS endpoint, API key and model. It performs
  structured generation and semantic relevance calls. Missing configuration,
  invalid output, budget exhaustion or upstream failure is technical. There is
  no fallback to the deterministic provider.

Commercial compliance, grounding, hashing, preview safety and pending-data
rules remain deterministic in both modes.

## Infrastructure prerequisites

Use one authorized PostgreSQL database and the existing TrackFlow Redis,
Celery, relay, API and Backoffice. Do not create a second broker, relay or
backend. Before changing a database:

1. Confirm the URL belongs to the intended environment and is not production or
   shared with unrelated work.
2. Record `current_database()`, `current_user` and server identity without
   printing the password.
3. Confirm a backup or recoverable snapshot exists for a persistent database.
4. Confirm Parte 1 tables exist before migrating an existing database.
5. Stop if the identity is ambiguous.

Example read-only preflight, using a task-specific variable rather than
`DATABASE_URL`:

```powershell
$env:RFP_RESPONSE_MIGRATION_DATABASE_URL = "<authorized PostgreSQL URL>"
psql "$env:RFP_RESPONSE_MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 `
  -c "select current_database(), current_user, inet_server_addr(), inet_server_port();"
psql "$env:RFP_RESPONSE_MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 `
  -c "select to_regclass('public.rfp_tickets'), to_regclass('public.rfp_jobs'), to_regclass('public.rfp_handoffs');"
```

Do not paste the URL into reports or shell history intended for sharing.

## DDL order

The application never applies DDL at startup.

### New disposable database

Apply the current Parte 1 schema first, then the forward Parte 2 migration:

```powershell
psql "$env:RFP_RESPONSE_MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 `
  -f infra/sql/rfp_intake.sql
psql "$env:RFP_RESPONSE_MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 `
  -f infra/sql/rfp_response_generation.sql
```

### Existing Parte 1 database

Do **not** replay the original schema over an existing environment. After the
identity, backup and baseline checks above, apply only:

```powershell
psql "$env:RFP_RESPONSE_MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 `
  -f infra/sql/rfp_response_generation.sql
```

The migration is forward-only and repeatable. It preserves historical intake
rows, assigns existing jobs `job_kind='intake'`, and gives legacy response runs
the compatible carrier snapshot defaults. Never use `DROP DATABASE`, recursive
deletion, `docker compose down -v` or volume removal as a migration step.

After migration, verify tables and historical counts rather than mutating data:

```powershell
psql "$env:RFP_RESPONSE_MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 `
  -c "select to_regclass('public.rfp_response_runs'), to_regclass('public.rfp_response_handoffs');"
psql "$env:RFP_RESPONSE_MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 `
  -c "select job_kind, count(*) from rfp_jobs group by job_kind order by job_kind;"
```

## Starting the services

Populate a private `.env` from `.env.example`. For offline QA, set
`RFP_RESPONSE_LLM_MODE=deterministic` and omit the FourGeeks key. Configure the
required task timing values documented in
`services/task_queue/README.md`, then start the existing services:

```powershell
docker compose --profile async-tasks up -d --build `
  api interfaces redis celery-worker task-relay
docker compose --profile async-tasks ps
docker compose --profile async-tasks logs --tail 100 api celery-worker task-relay
```

The Backoffice is served by `interfaces` on its configured loopback port. The
administrator must select a carrier model before starting generation. HTTP only
accepts the work; generation occurs asynchronously through outbox, relay and
Celery.

## Operational flow

1. Parte 1 reaches `analisis_completo` and has a valid `rfp-intake-v1` handoff.
2. An authenticated administrator posts the carrier confirmation to
   `POST /rfps/{ticket_id}/response`.
3. The transaction creates or reuses the compatible run, job and outbox row.
4. Relay publishes `trackflow.process_rfp_response`.
5. Celery acquires the durable lease and executes LangGraph.
6. Departments generate and evaluate independently; infrastructure retries do
   not increment the maximum of three real generations.
7. Global currency, SLA and volume discounts are generated and evaluated.
8. The synthesizer persists `rfp-response-v1` only after every business branch
   is terminal and all referenced artifacts are recoverable.
9. FastAPI exposes only allowlisted projections and previews that passed the
   safety gate. Backoffice displays technical, departmental, global and
   consolidated results separately.

Terminal interpretation:

- `ready_for_part3`: every automatic evaluation passed. It is not human
  approval and does not create any Parte 3 state.
- `needs_human_review`: the durable business result requires commercial or
  human input, or reached the business-generation limit.
- `technical_failure`: the workflow could not complete operationally. Preserve
  diagnostics and artifacts; there is no valid Parte 3 handoff if required
  drafts or evaluations are missing.

## Reproducible QA

### Offline regression

```powershell
$env:RFP_RESPONSE_LLM_MODE = "deterministic"
Remove-Item Env:FOUR_GEEKS_API_KEY -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m pytest `
  tests/pipelines/test_rfp_response.py `
  tests/pipelines/test_rfp_response_phase3.py `
  tests/pipelines/test_rfp_response_graph.py `
  tests/pipelines/test_rfp_response_llm.py `
  tests/test_rfp_response_api.py `
  tests/test_rfp_response_backoffice_contract.py `
  tests/test_rfp_response_persistence.py `
  tests/test_rfp_response_phase4.py -q
```

### PostgreSQL opt-in

Use only a verified disposable PostgreSQL database on loopback. The test suite
refuses an ambiguous, shared or production-like target.

```powershell
$env:TRACKFLOW_RFP_RESPONSE_TEST_DATABASE_URL = "<verified disposable loopback PostgreSQL URL>"
.\.venv\Scripts\python.exe -m pytest `
  tests/test_rfp_response_postgres_integration.py -q -rs
```

Remove the variable after the isolated database is stopped:

```powershell
Remove-Item Env:TRACKFLOW_RFP_RESPONSE_TEST_DATABASE_URL -ErrorAction SilentlyContinue
```

### Integrated deterministic E2E

Start an exclusive QA stack first and provide only its loopback API and database
URLs. Never point these variables at shared services.

```powershell
$env:TRACKFLOW_PHASE6_API_URL = "<isolated loopback API URL>"
$env:TRACKFLOW_PHASE6_DATABASE_URL = "<matching isolated PostgreSQL URL>"
.\.venv\Scripts\python.exe -m pytest tests/test_rfp_response_phase6_e2e.py -q -rs
```

Phase 6.1 carrier confirmation uses the equivalent
`TRACKFLOW_PHASE61_API_URL` and `TRACKFLOW_PHASE61_DATABASE_URL` variables with
`tests/test_rfp_response_phase61_e2e.py`.

Real FourGeeks validation is never part of routine regression. It requires an
explicit consumption authorization and a separately declared global request
budget. Do not run it merely because a private key is present.

## Security rules

- Inject the API key only into the authorized worker process; never into browser
  code, images, Git, screenshots or shared logs.
- Do not log prompts, responses, reasoning, authorization headers or full
  internal documents. Persist only the approved content-free invocation
  metadata.
- Treat `blocked`, `indeterminate` and `technical_error` previews as containing
  no public content. Safety checks fail closed.
- Never convert a technical error into an EvaluationResult or a commercial
  pending decision.
- Contract/schema errors are deterministic and must not be retried blindly.
  Transient infrastructure retries reuse the same business iteration.
- Do not invent rates, discounts, SLA values or carrier commitments.
- Preserve existing PostgreSQL rows and volumes during diagnosis.

## Diagnosis and recovery

1. Read the run, job, outbox and section status using authenticated API or
   read-only SQL.
2. Compare `draft_id`, canonical hash and `evaluation_id` before resuming.
3. If relay publication is pending, repair broker connectivity and allow the
   existing outbox record to publish; do not create a duplicate job.
4. If a lease expired, let the same run reacquire it. Fencing rejects stale
   workers.
5. If a persisted draft lacks an evaluation after interruption, resume that
   draft rather than generating a new business iteration.
6. If the run is `technical_failure`, retain its diagnosis. Do not fabricate a
   handoff or rewrite it as `needs_human_review`.
7. Before any container replacement, record container identity and mounts.
   Replace API/UI/worker only when necessary; do not recreate PostgreSQL or
   delete its volume.

## Known limitations

- FourGeeks availability, quota, model behavior and cost remain external
  dependencies. The configured ceilings bound requests but do not predict
  actual consumption.
- Semantic relevance is conservative: insufficient evidence can require human
  review even when prose appears plausible.
- Unknown or TrackFlow-managed last-mile rates remain commercial pending data;
  the system never guesses them.
- PostgreSQL and E2E tests are opt-in because they require verifiably isolated
  infrastructure.
- Repository-wide Backoffice lint retains historical findings outside the RFP
  scope. The RFP files, TypeScript validation and production build are checked
  independently.
- Optional MCP tests require their own package environment and are not part of
  the Parte 2 runtime.
