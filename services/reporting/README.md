# Weekly reporting service

This FastAPI router exposes published weekly KPI rows and pipeline run status.
It uses `TRACKFLOW_REPORTING_DATABASE_URL`, refuses to reuse `DATABASE_URL`, and
never reads or writes `telemetry_events`.

- `GET /reporting/weekly-warehouse-client-performance`: admin/manager. An
  omitted week falls back to the most recently valid publication.
- `GET /reporting/pipeline-runs/latest`: admin/manager.
- `POST /reporting/pipeline-runs`: admin only. It durably records a pending run
  before returning `202`, then schedules Prefect outside the request. `202`
  means accepted, never published.

The scheduler reuses the reporting engine already validated by this service. It
uses `DATABASE_URL` (or an explicitly injected `app.state.pipeline_source_engine`)
as a distinct operational source for the inventory outbox and StockExit evidence;
the source must not resolve to the reporting engine. It relays before extraction
and then derives coverage through the source+reporting audit in
`pipelines.operational_coverage`. The dual-engine HTTP runtime requires the
dependency fingerprint and rules version, but does **not** require
`TRACKFLOW_COVERAGE_EVIDENCE_FILE`. The synthetic JSON `CoverageEvidence` and
separate `technical_coverage` attestation remain limited to the legacy
single-engine/test-only compatibility paths. Neither operational nor technical
coverage sets `production_certified`; it remains `False`.

`TRACKFLOW_PIPELINE_DATABASE_URL` is CLI-only. A confirmed failure before
enqueue returns `503`, closes the accepted run as `failed_retryable`, and
permits a later durable retry. Configuration failures identify only the missing
or invalid setting, never its value. If even the status write is uncertain, the
run remains guarded and operators must reconcile it through
`GET /reporting/pipeline-runs/latest` before retrying.
