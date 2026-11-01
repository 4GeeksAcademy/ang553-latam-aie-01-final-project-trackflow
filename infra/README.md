# `infra` folder

This folder contains **infrastructure configurations** for the monorepo (for example: Dockerfiles, Terraform scripts, deployment manifests, Nginx configs, etc.).

- **Main purpose**: to centralize the definition of the infrastructure required to run the company's applications and services.
- **Recommendation**: document how to provision and deploy the infrastructure.

> _Spanish version: [README.es.md](./README.es.md)._

## DEV-53 scheduler

`infra/scheduler/Dockerfile` defines a scheduler image independent of the
FastAPI container. It installs the Python project declared by `pyproject.toml`,
including Prefect, and copies the active `pipelines/`, `services/`, and
`scripts/` runtime packages.

The non-root container starts only Supercronic. Its single schedule is
`15 0 * * *` in UTC and invokes `python -m scripts.nightly_export`. The job is
not run on container startup or by a healthcheck. Supercronic forwards job
stdout and stderr to the container logs.

Runtime configuration is supplied through Compose's `.env` file; no secret is
copied into the image. The named volume `telemetry-raw` is mounted at
`/app/data/raw`, preserving complete CSV artifacts across container
replacement. The scheduler has no startup dependency on the API service.

The service is opt-in through the `scheduler` Compose profile. Normal
`docker compose up` does not start it. Enable it deliberately only after
reviewing `.env` and the operational database target:

```powershell
docker compose --profile scheduler up -d scheduler
```

## DEV-53 manual reconciliation runbook

Use this only for a `nightly_export` row that remains `processing`. Abrupt
`SIGKILL`, host loss, power loss, or unconfirmed process-group cleanup cannot be
resolved safely from row age alone.

1. Stop future scheduling without deleting the container or volume:

   ```powershell
   docker compose --profile scheduler stop scheduler
   ```

2. Record the exact `job_run_id`, `target_date`, `started_at`, container name
   and UTC investigation time. Never reconcile by `job_name` or date alone.
3. Search container/runtime logs for that `job_run_id`. Inspect `docker ps`,
   `docker top <container>` and the host process list. Confirm that neither the
   nightly Python process nor its Prefect descendants are active. If this
   cannot be proven, leave the row `processing` and escalate.
4. Inspect `data/raw/telemetry_YYYY-MM-DD.csv` from the named volume. Validate
   header, row parsing and target-day timestamps with
   `scripts.nightly_export.validate_existing_csv`; do not edit or replace the
   file manually.
5. Reconcile the pipeline independently. Review logs and the matching
   `reporting.pipeline_publication_runs` evidence, including `status`,
   `commit_result` and run identifiers. A code 4 or `commit_uncertain` result
   must be resolved from durable reporting rows before changing `job_runs` or
   retrying. If commit outcome is still uncertain, leave `processing`.
6. Only after proving no active process and no uncertain commit, perform one
   compare-and-set transaction for the exact UUID:

   ```sql
   BEGIN;
   UPDATE job_runs
   SET status = 'failed',
       finished_at = GREATEST(statement_timestamp(), started_at),
       error_message = 'manual reconciliation: no active process; commit outcome confirmed'
   WHERE id = '<job_run_id>'::uuid
     AND status = 'processing'
   RETURNING id, status;
   ```

   The `RETURNING` result must contain exactly one row. Zero or multiple rows
   means issue `ROLLBACK;`, do not retry and investigate the current state.
   Issue `COMMIT;` only after observing exactly one returned row. Never update
   by age and never rewrite a terminal row.
7. Retry exactly once with the same target date. The validated CSV will be
   reused:

   ```powershell
   docker compose --profile scheduler run --rm `
     -e TARGET_DATE=YYYY-MM-DD scheduler python -m scripts.nightly_export
   ```

8. Confirm a new job-run row, its terminal result, unchanged CSV identity, and
   idempotent pipeline publication before re-enabling the scheduler profile.
