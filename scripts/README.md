# `scripts` folder

This folder contains **helper scripts** for the monorepo: development automation, maintenance utilities, repetitive tasks (setup, lint, migrations, data generation, etc.), and internal tooling.

- **Main purpose**: group support tools that do not belong to a specific app, agent, or pipeline but make the team’s work easier.
- **Recommendation**: document each script (what it does, parameters, requirements, usage examples) and keep them reproducible (and safe) across environments.

> _Spanish version: [README.es.md](./README.es.md)._

## DEV-53 nightly telemetry export

`python -m scripts.nightly_export` acquires the persistent `nightly_export`
job claim, exports `telemetry_events` for one UTC day to
`data/raw/telemetry_YYYY-MM-DD.csv`, and then invokes exactly
`python -m pipelines.pipeline` with the current interpreter and no week flag.

Configuration:

- `DATABASE_URL` is required and owns both telemetry reads and `job_runs` state.
- `TARGET_DATE` is optional and must be exactly `YYYY-MM-DD`; the default is
  yesterday in UTC.

An existing CSV is validated and reused, never replaced. New artifacts are
written to a unique temporary file in the destination directory, flushed,
fsynced, closed, and published through an atomic hard link. A failed pipeline
keeps the complete CSV for diagnosis or retry. A `failed` attempt does not
retain the date claim: a later invocation acquires a new `job_runs` row,
validates and reuses the existing CSV without querying telemetry or replacing
the file, and runs the pipeline again. A `completed` date remains idempotently
skipped. Controlled interrupts terminate the pipeline process group and wait
for confirmed exit before the job is released as failed. If child/group cleanup
cannot be confirmed, the durable row remains `processing`, a
reconciliation-required event is logged, and no terminal transition is
attempted.

Abrupt termination such as `SIGKILL`, host failure, or power loss cannot run
cleanup and may leave a `processing` job. Fase 2 deliberately provides no lease,
expiry, or automatic takeover; manual reconciliation remains required.

## Sales forecasting experiment

From the repository root run:

```powershell
uv run python scripts/train_sales_forecast.py
```

The script resolves the repository and canonical CSV independently of the
caller's working directory. It validates the data, trains the Random Forest,
produces a leakage-safe recursive forecast for 2024-2025, compares it with the
seasonal baseline, and regenerates the documented files in
`artifacts/sales_forecast/`. Dependencies come from
`pyproject.toml`/`uv.lock`; do not use `pip install`.

## MEM-092 agent-memory cleanup

Run one explicit maintenance pass from the repository root with the reviewed
PostgreSQL `DATABASE_URL` already present in the process environment:

```powershell
uv run --python 3.12 python -m scripts.agent_memory_cleanup --execute
```

The command refuses to run without `--execute`, rejects non-PostgreSQL targets,
does not provision tables, and prints only content-free expiration counts. It
reuses `MemoryStore.expire_and_clean`, whose default capacity is 100 active
entries per scope/client/category bucket. An operator may deliberately use a
smaller positive bound with `--max-active-per-scope-category COUNT`.

Schedule the command through the deployment's existing maintenance facility if
periodic cleanup is required. MEM-092 does not add another scheduler or service.
The command is transactional and idempotent, so a successful no-op run reports
zero counts. A nonzero exit code means the pass did not complete and must not be
reported as successful.

Inject `DATABASE_URL` from the deployment's secret store instead of placing it
in the task definition or command line. Run from the repository root, preserve
the process exit code, alert on nonzero results, and avoid deliberately
overlapping invocations. An hourly cadence is recommended when the 24-hour
proposal TTL should have no more than about one hour of operational delay; using
a daily cadence requires accepting retention until the next pass. Validate the
job first with one supervised execution followed by an idempotent second pass.
