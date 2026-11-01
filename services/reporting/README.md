# Weekly reporting service

This FastAPI router exposes published weekly KPI rows and pipeline run status.
It uses `TRACKFLOW_REPORTING_DATABASE_URL`, refuses to reuse `DATABASE_URL`, and
never reads or writes `telemetry_events`.

- `GET /reporting/weekly-warehouse-client-performance`: admin/manager. An
  omitted week falls back to the most recently valid publication.
- `GET /reporting/pipeline-runs/latest`: admin/manager.
- `POST /reporting/pipeline-runs`: admin only. It durably records a pending run
  plus its asynchronous task and outbox row in one transaction before
  returning `202`. The independent relay publishes later; `202` means accepted,
  never published.
- `GET /tasks/{task_id}`: admin only. It reads durable PostgreSQL state even
  after a Redis result expires and keeps Celery, TrackFlow and KPI publication
  states separate.

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

`TRACKFLOW_PIPELINE_DATABASE_URL` and source-database identity validation are
worker concerns. FastAPI accepts only into the reporting transaction and does
not contact Redis before returning `202`. Worker configuration failures are
sanitized and recorded through the durable task/DLQ contracts.
