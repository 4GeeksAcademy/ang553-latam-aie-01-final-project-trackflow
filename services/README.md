# `services` folder

This folder contains **all the backend services** (APIs and background workers) related to the company for the cross-functional AI Engineering project.

Each subfolder inside `services/` must correspond to **one specific service** (for example: `admin-api`, `data-processor-worker`) and include its own technical and functional documentation.

- **Main purpose**: to centralize all the backend logic, APIs, and queue consumers that support the company's use cases.
- **Recommendation**: document in this file (or in sub-READMEs) the services you add, their objective, the technology used, and how to run them.

> _Spanish version: [README.es.md](./README.es.md)._

## Inventory business commands

The inventory API supports persisted SKU thresholds, idempotent physical
counts, and idempotent commercial orders with one or more stable dispatch
identifiers. Their required telemetry is written to the transactional outbox
in the same database transaction as the business record.

The legacy `POST /inventory/orders/outbound` route remains a standalone stock
movement. It may produce a threshold-crossing event, but it does **not** create
a commercial order and does **not** produce `outbound_order_created`.
