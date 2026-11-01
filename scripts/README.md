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
