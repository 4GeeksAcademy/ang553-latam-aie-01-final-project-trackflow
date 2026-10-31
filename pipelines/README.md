# TrackFlow weekly pipeline

The supported local/operator entry point is:

```powershell
python -m pipelines.pipeline --week-start 2026-01-05
```

Omit `--week-start` to select the latest fully closed ISO week in UTC. The
weekly schedule is intended to run after the previous week closes, normally on
Monday morning. Explicit dates must be Mondays and must precede the current ISO
week.

## Required configuration

- `TRACKFLOW_PIPELINE_DATABASE_URL`: dedicated PostgreSQL connection for this
  pipeline. The CLI refuses to reuse `DATABASE_URL` and never prints the URL.
- `TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL` (optional): distinct operational
  PostgreSQL source. When present, the pipeline relays pending inventory outbox
  events idempotently into the reporting database and reads commercial-order
  plus dispatch evidence from this source. This selects the dual-engine
  operational-coverage runtime. When absent, the legacy single-database runtime
  is retained.
- `TRACKFLOW_PIPELINE_DEPENDENCY_FINGERPRINT`: the authorized dependency
  fingerprint for telemetry, business identities, order evidence and rules.
- `TRACKFLOW_PIPELINE_RULES_VERSION`: the approved transformation rules version.
- `TRACKFLOW_COVERAGE_EVIDENCE_FILE`: required only by the legacy single-engine
  runtime; path to an authorized synthetic JSON evidence file.

The dual-engine CLI and HTTP runtimes require the dependency fingerprint and
rules version, but do **not** require `TRACKFLOW_COVERAGE_EVIDENCE_FILE`. After
relaying the outbox, they derive coverage by auditing the operational source and
reporting database through `pipelines.operational_coverage`. Incomplete,
unexpected or conflicting source, outbox, telemetry or extraction evidence
keeps the quality gate blocked.

The legacy single-engine evidence file has this shape:

```json
{
  "confirmed_event_types": [
    "stock_threshold_triggered",
    "inventory_discrepancy_detected"
  ],
  "dispatch_order_ids": [["movement-id", "order-id"]],
  "lookup_complete": true,
  "source": "synthetic_fixture"
}
```

The legacy single-engine path recognizes `synthetic_fixture` as technical
coverage evidence. Controlled tests may also inject the separate
`technical_coverage` attestation. Both mechanisms are compatibility/test-only
paths: neither certifies business or production coverage. The evidence file must
come from an authorized process; the legacy CLI does not infer or manufacture
it. Missing or invalid configuration blocks execution clearly.

There are no flags to bypass quality checks, coverage validation, concurrency
leases or commit recovery. Output is one JSON object and never contains a
connection string or credential. Exit codes are stable:

- `0`: succeeded or idempotent no-op;
- `1`: invalid request or operational error;
- `2`: configuration or quality block;
- `3`: superseded or already in progress;
- `4`: commit result uncertain.

The relay locks pending source rows with `FOR UPDATE SKIP LOCKED`, inserts the
stable event UUID at the destination with conflict-safe idempotency, and only
then acknowledges the source row. A destination failure leaves the event
pending with a bounded diagnostic; credentials and exception messages are not
stored. An existing UUID is acknowledged only when its stored timestamp, type,
service, level, value, message and filtered tags match the source event exactly;
a semantic conflict remains pending. Before dual-engine work begins, both live
PostgreSQL connections are identified by effective server, port, database name
and database OID, so URL aliases cannot bypass isolation while two distinct
databases on one server remain valid. This transport does not certify threshold,
discrepancy, order identity, or production coverage.

## Operational business-event model

Inventory uses three explicit durable producer contracts:

- `SKU.minimum_stock_threshold` is nullable (`NULL` disables alerts) and must
  otherwise be a non-negative integer. The producer emits only on the
  crossing `before > threshold` and `after <= threshold`; returning above the
  threshold rearms it implicitly.
- `inventory_physical_count` records one confirmed observation with stable
  `count_id`, expected and observed quantities, SKU/client/warehouse, timestamp
  and user. The command requires the caller-supplied UUID for retry stability;
  expected stock is derived by the service. Recording a count never adjusts
  computed stock.
- `commercial_order` owns stable `order_id`. A commercial order may reference
  multiple `StockExit` dispatch rows through `commercial_order_id`; losses may
  not reference an order. Creation requires at least one initial dispatch, and
  later partial dispatches reuse the same `order_id` without emitting another
  order-created event. The FK remains nullable during the producer migration.

The outbox admits only explicit source/event pairs. In particular,
`outbound_order_created` belongs to `commercial_order`, never to an individual
dispatch. Its properties are exactly `order_id`, `warehouse` and `client_id`;
product, quantity and movement fields are not applicable because one order may
own heterogeneous partial dispatches. The legacy StockExit endpoint therefore
does not claim an outbound-order event. Threshold and physical-count events
carry their originating `movement_id` and `count_id`, respectively.

The extractor treats outbound telemetry as order-shaped: it requires `order_id`
and verifies the operational `commercial_order` identity plus at least one
linked `StockExit` dispatch. It counts distinct verified orders, never dispatch
quantities. Historical movement-shaped outbound telemetry without `order_id` is
rejected conservatively and does not certify the new producer contract.

Every authentic StockEntry and StockExit also writes an atomic
`inventory_threshold_evaluation` checkpoint containing the before/after stock,
threshold, crossing decision and optional threshold event UUID. In a dual-engine
runtime, the pipeline relays first and then audits the closed week across the
operational and reporting databases. The audit requires complete movement,
order/dispatch, physical-count, outbox and canonical telemetry evidence before
clearing `coverage_pending`. Missing or conflicting evidence keeps publication
blocked. This operational coverage enables the normal technical publication
path but does not set `production_certified`, which remains `False`.

Dual-engine HTTP and CLI runtimes derive coverage from this audit and do not
require a synthetic JSON coverage file. The legacy single-engine/test path
retains the conservative synthetic-evidence and `technical_coverage`
compatibility contracts. Operational or technical coverage can permit a
technical publication, but `production_certified` remains `False`.

## Location constraint and RQ-15

The active implementation is exclusively `pipelines/pipeline.py`; no wrapper or
copy exists under `data/`. This differs from the historical path in A1. Formal
evaluator acceptance of the active path and command remains pending under
RQ-15.
