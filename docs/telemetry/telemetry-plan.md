# TrackFlow Telemetry Plan

## 1. Purpose and scope

This document defines the first design baseline for TrackFlow telemetry before instrumentation is implemented. Its purpose is to make inventory activity, operational controls, and business outcomes observable across the Los Angeles and Zaragoza warehouses, replacing fragmented operational visibility with consistent evidence for decisions.

The plan covers technical and business telemetry for the inventory domain. The five mandatory events in this phase are the minimum contract, not the complete future catalog. An expanded catalog of technical and business opportunities will be designed in the next phase.

This document is a design artifact only. It does not implement event producers, a telemetry pipeline, storage, or changes to the current backend and frontend.

## 2. Domain and business constraints

TrackFlow operates logistics and warehouse services for e-commerce brands in two warehouses: Los Angeles and Zaragoza. Inventory is client-owned and must remain separated by client and warehouse.

The inventory rules relevant to telemetry are:

- `stock = total inbound quantities - total outbound quantities` for a product and warehouse.
- Stock has no legitimate direct-edit operation.
- Every stock change must happen through an operation equivalent to `InboundOrder` or `OutboundOrder`.
- Every stock movement must be attributable to an authenticated user.
- Each SKU belongs to exactly one client; inventory must not be mixed between clients.
- The system must not allow negative stock.
- Inventory events must not contain personally identifiable information (PII) about the final consumer.
- Inventory events must not include last-mile carrier or final-recipient information, which belongs to another domain.

The current implementation represents these concepts with `SKU`, `StockEntry`, and `StockExit`. The telemetry contract retains the required domain vocabulary (`Product`, `InboundOrder`, and `OutboundOrder`) and documents mappings to the current implementation where useful.

## 3. Canonical telemetry conventions

### `warehouse`

The canonical telemetry values are exactly:

- `los_angeles`
- `zaragoza`

The current backend uses short warehouse codes, while some frontend structures use presentation labels. Future instrumentation must map them as follows:

| Current value | Canonical telemetry value |
| --- | --- |
| `LA` | `los_angeles` |
| `ZGZ` | `zaragoza` |
| `Los Angeles` | `los_angeles` |
| `Zaragoza` | `zaragoza` |

`Los Angeles` and `Zaragoza` are presentation values, not values of the telemetry contract. No other warehouse value is valid for these TrackFlow inventory events.

### Identifiers and product dimensions

- `product_id` is the canonical telemetry identifier for the product/SKU involved in the event. In the current implementation, the relevant concepts are `SKU.id` and `sku_id`; the definitive mapping must be selected during instrumentation.
- `product_category` is derived from the category of that SKU. The current backend field is `category`.
- `client_id` must be a stable identifier for the client that owns the SKU. The current system exposes `client_name`, but it does not currently expose a stable `client_id` field in the inventory model.
- Future instrumentation must resolve `client_id` from an authoritative client source. It must not invent an identifier and must not silently use `client_name` as a substitute.

The required inventory properties for every mandatory event are:

- `warehouse`
- `client_id`
- `product_id`
- `product_category`
- `quantity`

This phase defines the required property names and meanings only. It does not define the complete event envelope or detailed JSON types; those belong to Phase 2.

## 4. Inventory flow and instrumentation points

The conceptual flow begins with an authenticated warehouse user and ends with an accepted or rejected inventory operation:

1. An authenticated user requests an inventory operation.
2. The system resolves the product/SKU and its owning client.
3. The system validates the warehouse and operation payload.
4. For outbound operations, the system computes available stock before persistence.
5. The system either rejects the operation or persists the inbound/outbound movement.
6. The resulting inventory state can activate a minimum-stock condition or later be compared with a physical count.

The current implementation provides these relevant foundations:

- Inbound creation at `POST /inventory/orders/inbound`: validates the SKU and warehouse, creates a `StockEntry`, takes `user_uuid` from the authenticated user, and invalidates cache after persistence.
- Outbound creation at `POST /inventory/orders/outbound`: validates the SKU and warehouse, calculates available stock, rejects negative stock, validates `exit_type` and `tracking_number`, creates a `StockExit`, takes `user_uuid` from the authenticated user, and invalidates cache after persistence.
- Validation/rejection: the service rejects missing SKUs, warehouse mismatches, invalid outbound conditions, and insufficient stock before a movement is persisted.

The conceptual instrumentation points for the mandatory contract are:

1. **Inbound movement committed:** after an inbound order is successfully persisted.
2. **Outbound movement committed:** after an outbound order is successfully persisted.
3. **Inventory operation rejected:** at validation or business-rule rejection, before a movement is persisted.
4. **Direct stock edit rejected:** at an authorization or command boundary that receives an attempt to alter stock outside inbound/outbound operations.
5. **Minimum-stock threshold activated:** when an authoritative inventory evaluation determines that a product has reached its configured threshold.
6. **Inventory discrepancy detected:** when a warehouse audit or physical count compares observed stock with system stock and identifies a discrepancy.

The last three points are conceptual future instrumentation points. The current repository does not yet provide a clearly implemented producer for them.

## 5. Mandatory TrackFlow event catalog

Only the following five event types are mandatory in this phase. The catalog is intentionally limited to these events; additional technical and business events belong to the next phase.

### 5.1 `inbound_order_created`

| Field | Definition |
| --- | --- |
| `event_type` | `inbound_order_created` |
| `classification` | `mandatory` |
| `category` | inventory / inbound operations |
| `trigger` | An inbound order equivalent to the current `POST /inventory/orders/inbound` is validated and the resulting `StockEntry` is successfully persisted. |
| `business hypothesis` | We need to know how much volume enters, by client and warehouse. |
| `decision enabled` | Plan warehouse capacity and staffing according to inbound volume. |
| `conceptual producer / instrumentation point` | Inventory service immediately after successful inbound persistence and before/around the post-persistence cache invalidation boundary. |
| `required inventory properties` | `warehouse`, `client_id`, `product_id`, `product_category`, `quantity` |

**Decision statement:** We capture `inbound_order_created` because we need to know how much volume enters by client and warehouse, which allows us to plan warehouse capacity and staffing according to inbound volume.

### 5.2 `outbound_order_created`

| Field | Definition |
| --- | --- |
| `event_type` | `outbound_order_created` |
| `classification` | `mandatory` |
| `category` | inventory / outbound operations |
| `trigger` | The warehouse completes picking and dispatch of an outbound order. |
| `business hypothesis` | We need to know how many orders are processed, by client and warehouse, and at what rate. |
| `decision enabled` | Detect operational bottlenecks before they affect the SLA. |
| `conceptual producer / instrumentation point` | The current technical boundary closest to this business event is `POST /inventory/orders/outbound` and successful `StockExit` persistence. During instrumentation, this boundary must be validated to represent actual completion of picking and dispatch. |
| `required inventory properties` | `warehouse`, `client_id`, `product_id`, `product_category`, `quantity` |

**Decision statement:** We capture `outbound_order_created` because we need to know how many orders are processed by client and warehouse, and at what rate, which allows us to detect operational bottlenecks before they affect the SLA.

### 5.3 `stock_threshold_triggered`

| Field | Definition |
| --- | --- |
| `event_type` | `stock_threshold_triggered` |
| `classification` | `mandatory` |
| `category` | inventory / stock health |
| `trigger` | The stock of an SKU falls below the configured minimum-stock threshold for that client. |
| `business hypothesis` | We need to know how frequently a client runs out of available stock for an SKU. |
| `decision enabled` | Alert the client and the commercial team before a stockout. |
| `conceptual producer / instrumentation point` | Future centralized stock-threshold evaluation in the inventory domain, after a reliable stock calculation determines that the stock has fallen below the configured client threshold. The current repository has no clearly implemented backend producer for this event. |
| `required inventory properties` | `warehouse`, `client_id`, `product_id`, `product_category`, `quantity` |

**Decision statement:** We capture `stock_threshold_triggered` because we need to know how frequently a client runs out of available stock for an SKU, which allows us to alert the client and commercial team before a stockout.

### 5.4 `direct_stock_edit_rejected`

| Field | Definition |
| --- | --- |
| `event_type` | `direct_stock_edit_rejected` |
| `classification` | `mandatory` |
| `category` | inventory / governance |
| `trigger` | An authenticated actor or system attempts to modify stock outside an inbound or outbound operation and the attempt is rejected. |
| `business hypothesis` | We need to know whether staff attempts to bypass traceability controls. |
| `decision enabled` | Reinforce training or permissions in the warehouse where this occurs most frequently. |
| `conceptual producer / instrumentation point` | Future authorization or command boundary for direct stock-edit attempts, before the rejected request exits the system. The current repository has no explicit direct-stock-edit endpoint that acts as a producer. |
| `required inventory properties` | `warehouse`, `client_id`, `product_id`, `product_category`, `quantity` |

**Decision statement:** We capture `direct_stock_edit_rejected` because we need to know whether staff attempts to bypass traceability controls, which allows us to reinforce training or permissions in the warehouse where this occurs most frequently.

### 5.5 `inventory_discrepancy_detected`

| Field | Definition |
| --- | --- |
| `event_type` | `inventory_discrepancy_detected` |
| `classification` | `mandatory` |
| `category` | inventory / reconciliation |
| `trigger` | A warehouse audit or physical count compares observed inventory with system-calculated inventory and detects a difference for a product. |
| `business hypothesis` | We need to know which SKUs and warehouses have the most discrepancies. |
| `decision enabled` | Prioritize audits for SKUs with the highest discrepancy rate or frequency. |
| `conceptual producer / instrumentation point` | Future warehouse audit/reconciliation workflow at the moment the counted quantity is compared with system stock and a discrepancy is confirmed. The current repository has no implemented audit or discrepancy-detection flow. |
| `required inventory properties` | `warehouse`, `client_id`, `product_id`, `product_category`, `quantity` |

**Decision statement:** We capture `inventory_discrepancy_detected` because we need to know which SKUs and warehouses have the most discrepancies, which allows us to prioritize audits for SKUs with the highest discrepancy rate or frequency.

For all five events, the required inventory properties must describe TrackFlow inventory only. They must not include final-consumer PII, final-recipient information, or carrier information from the last-mile domain.

## 6. Additional telemetry opportunities

The following opportunities are grounded in implemented API routes, backend middleware, and real backoffice screens. They are not additional mandatory contract events. Each opportunity has a concrete hypothesis and decision so that instrumentation can be prioritized by operational value rather than by event volume.

### 6.1 `auth_login_succeeded`

| Field | Definition |
| --- | --- |
| `event_type` | `auth_login_succeeded` |
| `classification` | `identified opportunity` |
| `category` | authentication |
| `trigger` | `POST /auth/login` validates credentials and returns an access-token response. |
| `hypothesis` | We need to know when operators successfully enter the backoffice so that active usage can be distinguished from registered accounts. |
| `decision enabled` | Assess adoption of the authenticated backoffice and investigate access patterns by operational team or environment. |
| `conceptual producer / instrumentation point` | Authentication route after successful credential verification and before returning the response. |
| `relevant properties` | `role` |

**Decision statement:** We capture `auth_login_succeeded` because we need to know when operators successfully enter the backoffice, which allows us to assess authenticated operational usage.

### 6.2 `auth_login_failed`

| Field | Definition |
| --- | --- |
| `event_type` | `auth_login_failed` |
| `classification` | `identified opportunity` |
| `category` | authentication / security |
| `trigger` | `POST /auth/login` rejects an authentication attempt because the account is unknown, the password is invalid, or the account is inactive. |
| `hypothesis` | We need to know whether authentication failures are isolated user mistakes or a recurring access or security signal. |
| `decision enabled` | Improve operator access support or investigate suspicious failure concentrations without exposing credential values. |
| `conceptual producer / instrumentation point` | Authentication route at each rejected login branch, using a normalized failure reason rather than submitted credentials. |
| `relevant properties` | `failure_reason`, `role_if_known`, `status` |

**Decision statement:** We capture `auth_login_failed` because we need to know whether authentication failures are isolated mistakes or recurring access/security signals, which allows us to improve access support or investigate suspicious concentrations.

### 6.3 `auth_password_reset_requested`

| Field | Definition |
| --- | --- |
| `event_type` | `auth_password_reset_requested` |
| `classification` | `identified opportunity` |
| `category` | authentication / account recovery |
| `trigger` | `POST /auth/forgot-password` receives a reset request and completes its generic response flow. |
| `hypothesis` | We need to know how often operators require account recovery and whether recovery demand is concentrated in a workflow or period. |
| `decision enabled` | Improve account-recovery guidance and operational support while preserving the endpoint's anti-enumeration behavior. |
| `conceptual producer / instrumentation point` | Password-recovery route after the request outcome is classified internally; instrumentation must not distinguish account existence in the client-facing response. |
| `relevant properties` | `request_outcome_class`, `status` |

**Decision statement:** We capture `auth_password_reset_requested` because we need to know how often operators require account recovery, which allows us to improve recovery guidance and support.

### 6.4 `auth_password_reset_completed`

| Field | Definition |
| --- | --- |
| `event_type` | `auth_password_reset_completed` |
| `classification` | `identified opportunity` |
| `category` | authentication / account recovery |
| `trigger` | `POST /auth/reset-password` successfully changes the account password using a valid reset flow. |
| `hypothesis` | We need to know whether reset requests result in completed recovery or fail before account access is restored. |
| `decision enabled` | Reduce recovery friction and investigate abnormal reset completion rates. |
| `conceptual producer / instrumentation point` | Reset-password route after the password update succeeds. |
| `relevant properties` | `status`, `reset_outcome` |

**Decision statement:** We capture `auth_password_reset_completed` because we need to know whether recovery requests restore account access, which allows us to reduce recovery friction.

### 6.5 `inventory_validation_failed`

| Field | Definition |
| --- | --- |
| `event_type` | `inventory_validation_failed` |
| `classification` | `identified opportunity` |
| `category` | errors / validation |
| `trigger` | An inventory request is rejected by a domain validation such as an unknown SKU, warehouse mismatch, invalid exit payload, or another request-level inventory rule before a movement is persisted. Insufficient stock is explicitly excluded because that condition is captured exclusively by `inventory_stock_insufficient`. |
| `hypothesis` | We need to know which inventory validation classes, excluding insufficient stock, block warehouse work most often. |
| `decision enabled` | Improve operator guidance, source-data quality, or API validation rules according to the dominant failure class. |
| `conceptual producer / instrumentation point` | Inventory route/service exception boundary before persistence, normalized into a bounded `failure_reason` taxonomy. The taxonomy must exclude insufficient-stock rejections, which belong exclusively to `inventory_stock_insufficient`. |
| `relevant properties` | `operation`, `failure_reason`, `warehouse`, `product_id_if_known`, `status_code` |

**Decision statement:** We capture `inventory_validation_failed` because we need to know which inventory validation classes, excluding insufficient stock, block warehouse work most often, which allows us to improve guidance, source data, or validation rules.

### 6.6 `inventory_stock_insufficient`

| Field | Definition |
| --- | --- |
| `event_type` | `inventory_stock_insufficient` |
| `classification` | `identified opportunity` |
| `category` | errors / inventory operations |
| `trigger` | An outbound request is rejected because the requested quantity exceeds the available stock calculated for the SKU and warehouse. |
| `hypothesis` | We need to know where demand or inventory accuracy repeatedly prevents outbound fulfillment. |
| `decision enabled` | Prioritize replenishment, inventory reconciliation, or outbound-capacity intervention before service levels are affected. |
| `conceptual producer / instrumentation point` | `create_stock_exit()` at the insufficient-stock guard, before the rejected `StockExit` is persisted. |
| `relevant properties` | `warehouse`, `product_id`, `requested_quantity`, `available_quantity`, `product_category` |

**Decision statement:** We capture `inventory_stock_insufficient` because we need to know where available stock repeatedly prevents outbound fulfillment, which allows us to prioritize replenishment or reconciliation.

### 6.7 `api_request_slow`

| Field | Definition |
| --- | --- |
| `event_type` | `api_request_slow` |
| `classification` | `identified opportunity` |
| `category` | performance / technical health |
| `trigger` | A request measured by the existing `log_request_timing` middleware exceeds the agreed latency threshold for its method and path. |
| `hypothesis` | We need to know which API surfaces degrade in latency so that technical investigation can focus on user-impacting operations. |
| `decision enabled` | Prioritize performance work on the routes with the greatest latency impact or operational frequency. |
| `conceptual producer / instrumentation point` | `log_request_timing` middleware after the response is available, applying a documented threshold without duplicating ordinary request logs. |
| `relevant properties` | `method`, `path_template`, `status_code`, `duration_ms` |

**Decision statement:** We capture `api_request_slow` because we need to know which API surfaces degrade in latency, which allows us to prioritize performance work where users are affected.

### 6.8 `api_request_failed`

| Field | Definition |
| --- | --- |
| `event_type` | `api_request_failed` |
| `classification` | `identified opportunity` |
| `category` | performance / technical health / errors |
| `trigger` | The existing request-timing middleware observes an API response with an HTTP 5xx status, representing a technical server failure. Expected 4xx business or user errors are excluded because they have semantic events of their own. |
| `hypothesis` | We need to know which routes generate technical server failures so that reliability work can target real operational impact. |
| `decision enabled` | Prioritize fixes, alerting, and support investigation for routes with recurring technical server failures. |
| `conceptual producer / instrumentation point` | `log_request_timing` middleware after the response status is known, emitting this event only for HTTP 5xx responses and retaining the route template. |
| `relevant properties` | `method`, `path_template`, `status_code`, `duration_ms` |

**Decision statement:** We capture `api_request_failed` because we need to know which routes generate technical server failures, which allows us to prioritize reliability fixes and support investigation.

### 6.9 `backoffice_section_entered`

| Field | Definition |
| --- | --- |
| `event_type` | `backoffice_section_entered` |
| `classification` | `identified opportunity` |
| `category` | navigation / workflow usage |
| `trigger` | An authenticated operator enters a supported backoffice section such as inventory products, inventory orders, suppliers, or incidents. |
| `hypothesis` | We need to know which operational sections are used most by operators. |
| `decision enabled` | Prioritize navigation, training, and UI improvements for the sections with the highest operational use. |
| `conceptual producer / instrumentation point` | Backoffice App Router page entry or a shared route-level navigation boundary, emitting a normalized section name rather than individual clicks. |
| `relevant properties` | `section` |

**Decision statement:** We capture `backoffice_section_entered` because we need to know which operational sections operators use most, which allows us to prioritize navigation, training, and UI improvements.

### 6.10 `inventory_workflow_started`

| Field | Definition |
| --- | --- |
| `event_type` | `inventory_workflow_started` |
| `classification` | `identified opportunity` |
| `category` | navigation / workflow usage / inventory |
| `trigger` | An operator opens the inbound or outbound inventory-order flow from the corresponding backoffice route. |
| `hypothesis` | We need to know how often operators begin inbound and outbound workflows in order to compare demand with completed movements. |
| `decision enabled` | Prioritize workflow usability and staffing analysis for the inventory operation with the greatest demand. |
| `conceptual producer / instrumentation point` | The inbound and outbound order page entry boundary, using a bounded `workflow` value. |
| `relevant properties` | `workflow` |

**Decision statement:** We capture `inventory_workflow_started` because we need to know how often operators begin inbound and outbound workflows, which allows us to prioritize usability and staffing analysis.

### 6.11 `inventory_workflow_abandoned`

| Field | Definition |
| --- | --- |
| `event_type` | `inventory_workflow_abandoned` |
| `classification` | `identified opportunity` |
| `category` | navigation / workflow usage / inventory |
| `trigger` | An operator starts an inbound or outbound order flow but leaves the page, navigates away, or remains inactive until the documented workflow timeout without a successful create response. |
| `hypothesis` | We need to know where inventory workflows are abandoned before persistence so that friction can be separated from backend validation failures. |
| `decision enabled` | Improve the affected form or workflow and investigate whether operators need clearer guidance or support. |
| `conceptual producer / instrumentation point` | Client-side workflow lifecycle boundary in `InboundStockForm` or `OutboundStockForm`, correlated with the later inventory create response when available. |
| `relevant properties` | `workflow`, `abandonment_stage`, `had_validation_error`, `duration_ms` |

**Decision statement:** We capture `inventory_workflow_abandoned` because we need to know where inventory workflows are abandoned before persistence, which allows us to improve the affected form and guidance.

### 6.12 `inventory_product_created`

| Field | Definition |
| --- | --- |
| `event_type` | `inventory_product_created` |
| `classification` | `identified opportunity` |
| `category` | business / inventory master data |
| `trigger` | `POST /inventory/products` successfully creates a new SKU. |
| `hypothesis` | We need to know how quickly and where inventory master data is added so that product onboarding load can be planned. |
| `decision enabled` | Plan master-data support and identify unusual product-creation patterns that could affect inventory quality. |
| `conceptual producer / instrumentation point` | Inventory product route after the new `SKU` is committed successfully. |
| `relevant properties` | `warehouse`, `client_id`, `product_id`, `product_category`, `actor_role` |

**Decision statement:** We capture `inventory_product_created` because we need to know how quickly inventory master data is added, which allows us to plan support and protect data quality.

The additional opportunities above intentionally avoid capturing passwords, complete access tokens, reset tokens, secrets, final-recipient data, consumer PII, or unrelated last-mile information. User and session identifiers, if required, should be defined once in the future event envelope rather than duplicated unnecessarily in each event's properties.

## 7. Event Envelope

All TrackFlow telemetry events, including the five mandatory events and the twelve identified opportunities, must use the same top-level Event Envelope. The envelope is a common contract around event-specific properties; it is not itself a new event and does not change the catalog's 17 `event_type` values.

### 7.1 Conceptual structure

The following is a documentation example only. It is not yet a JSON Schema and does not define a complete event payload for any particular event:

```json
{
	"eventId": "01f2c3d4-5678-4abc-8def-0123456789ab",
	"timestamp": "2026-09-15T03:00:00Z",
	"sessionId": "session-opaque-value",
	"userId": "user-opaque-value",
	"event_type": "inbound_order_created",
	"schemaVersion": "1.0",
	"requestId": "request-opaque-value",
	"properties": {}
}
```

The identifiers in this example are illustrative opaque values. They must not be replaced with email addresses, credentials, tokens, or other personal data.

### 7.2 Field definitions

| Field | Purpose and expected format | Origin / producer | Requiredness and rules |
| --- | --- | --- | --- |
| `eventId` | Identifies one logical event instance. Use a UUID v4 string (or an equivalent collision-resistant opaque identifier if the future implementation standardizes one). | Generated exactly once when the logical event instance is created, before any delivery attempt. | Always required and unique across distinct events. Retries of the same logical event must reuse exactly the same `eventId` so downstream deduplication/idempotency works; a new logical event receives a new `eventId`. It must remain different from `requestId`, and `requestId` must not be copied into this field. |
| `timestamp` | Records event time: when the business or technical event occurred, not when it was queued, processed, or exported. Use UTC ISO 8601 with an explicit `Z`, for example `2026-09-15T03:00:00Z`. | The producer that observes the event occurrence. | Always required. Do not use a later processing timestamp or a future pipeline `computed_at` value as a substitute. |
| `sessionId` | Correlates actions belonging to one interactive session. It is an opaque, non-personal identifier; it is not an authentication identity. | Future frontend/session instrumentation for interactive activity; backend instrumentation propagates it when the request context carries it. | The envelope key is always present, but it may be `null` when no human session exists. The current repository has no formal centralized `sessionId`; instrumentation must establish and propagate one without using personal data. Internal jobs and system events without a human session use `null`, not an invented human session. |
| `userId` | Identifies the authenticated internal actor responsible for the action when one exists. | The authenticated backend identity, mapped to the current `user_uuid` only because the audited inventory architecture sets movement `user_uuid` from the authenticated user's ID. Frontend events should use the authenticated identity supplied by the application context, not a client-submitted identity. | The envelope key is always present. It contains the opaque internal user identifier when an authenticated actor exists and is `null` for legitimate system/process events without a human actor. Never use email, name, password, JWT, access token, or reset token; never invent a real user. |
| `event_type` | Gives the event its stable semantic meaning using the `entidad_accion` taxonomy. | The catalog-selected producer for the event. | Always required and must exactly match one of the 17 event types already defined in this document. A version must not silently change an existing event's meaning; a semantic change requires a new design/version decision rather than reusing the name invisibly. The spelling remains `event_type` in snake_case. |
| `schemaVersion` | Identifies the version of the common envelope plus the applicable event payload contract. | The producer declares the version of the contract it emits. | Always required and a string using simple `major.minor` notation, initially `"1.0"`. Increment the major number for incompatible changes (including changed meaning, removed/renamed fields, or changed requiredness); increment the minor number for backward-compatible additions or clarifications. |
| `requestId` | Correlates frontend-originated work through backend handling, logs, and events derived from one HTTP request. | Accept and propagate an existing trusted request ID when available; otherwise generate one at the backend edge. All logs and events derived from that request reuse it. | The envelope key is always present. It may be `null` for work that is not related to HTTP. It is not an event identity and must never replace `eventId`. The current repository has no centralized correlation/request ID, so this behavior is a future instrumentation requirement. |
| `properties` | Contains only event-specific attributes and bounded business/technical dimensions needed for analysis. | The event producer, using the event's catalog definition and authoritative domain context. | Always required and represented as an object, possibly empty when an event has no additional attributes. Do not duplicate envelope fields unless an explicit event-level analytical need is documented. Never include passwords, JWTs, access/reset tokens, API keys, secrets, consumer PII, final-recipient data, or unrelated last-mile information. |

### 7.3 Required versus nullable

Every envelope must contain all eight top-level keys. No key may be silently omitted because it does not apply. The always-required fields are `eventId`, `timestamp`, `event_type`, `schemaVersion`, and `properties`; their values must not be null. `sessionId`, `userId`, and `requestId` are also structurally required keys, but their values may be `null` when their semantic context does not exist:

- `sessionId` is non-null for an interactive session that has been established and propagated; it is `null` for system/process events without a human session.
- `userId` is non-null when an authenticated internal actor is responsible; it is `null` for a legitimate internal process with no human actor.
- `requestId` is non-null for an event originating from or derived from an HTTP request, using the request ID propagated across the request boundary; it is `null` for non-HTTP work.

This keeps one predictable object shape across frontend events, backend events, and future internal processes while distinguishing “not applicable” from an accidentally missing field. A producer must not use a fake session, fake user, or fake HTTP request ID to avoid `null`.

### 7.4 Correlation semantics

These identifiers answer different questions and must not be substituted for one another:

- **`eventId`** → Which individual emitted event is this? It is unique per event and supports deduplication.
- **`requestId`** → Which HTTP request and its derived logs/events produced this work? Multiple events may share it.
- **`sessionId`** → Which interactive session does this activity belong to? Multiple requests and events may share it; internal non-interactive work has `null`.
- **`userId`** → Which authenticated internal actor is responsible? Multiple sessions and events may belong to that actor; system work has `null`.

### 7.5 Event time

`timestamp` is event time: the UTC instant at which the event occurred or was observed at its authoritative producer. It is not processing time. A future pipeline's `computed_at` records when that pipeline computed a result and must not replace the envelope timestamp. These concepts must remain separate; this phase does not design the weekly pipeline.

### 7.6 Envelope implementation gaps

The envelope currently requires future instrumentation in the following focused areas:

- There is no formal centralized `sessionId`; frontend session context and backend propagation still need to be defined during implementation.
- There is no centralized `requestId`/correlation ID; the backend edge must add generation and propagation before HTTP-derived telemetry can satisfy the correlation policy.
- `client_id` remains unresolved as an authoritative stable client identifier, as documented in the canonical conventions and catalog.
- Producers for `stock_threshold_triggered`, `direct_stock_edit_rejected`, and `inventory_discrepancy_detected` remain incomplete or absent, as documented in the mandatory catalog.

These gaps are documented requirements, not claims that the current application already emits the envelope.

## 8. Event property contracts

The Event Envelope defines the eight global fields. This section defines the event-specific `properties` object for every catalog event. Property names use `snake_case`; envelope fields are never copied into `properties`. Unless a table says otherwise, an omitted optional property means that the producer legitimately could not know it at the event boundary; it must not be replaced with a fabricated value or a raw error message.

### 8.1 Shared property conventions

These conventions are reused by the event tables below.

| Property | JSON type and contract | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- |
| `warehouse` | `string` | Exactly `los_angeles` or `zaragoza`. `LA`, `ZGZ`, and presentation labels such as `Los Angeles` are not valid final telemetry values. | Warehouse on the authoritative SKU or movement, normalized from the current backend code at the producer boundary. |
| `client_id` | `string` | Required where specified; non-empty, stable, opaque identifier. No format or value is invented in this document. | Identifier of the client owning the SKU. The current model exposes `client_name`, not an authoritative `client_id`; instrumentation must resolve the source before emission. |
| `product_id` | `string` | Required where specified; non-empty stable identifier. Must not be a product name. | Identifier of the SKU/Product. Mapping between telemetry `product_id` and current `SKU.id`/request `sku_id` remains to be selected during instrumentation. |
| `product_category` | `string` | Normalized domain value. The current code authoritatively validates `fashion`, `electronics`, and `cosmetics`; these are the bounded values for inventory events. | SKU category from the authoritative SKU record or validated payload. |
| `quantity` | `integer` | Event-specific constraints are stated in each table; quantities of physical inventory are never fractional. | The quantity defined by the individual event contract, not a generic envelope quantity. |
| `duration_ms` | `number` | `>= 0`; milliseconds elapsed at the producer. | Request or workflow duration measured by the relevant instrumentation boundary. |
| `status_code` | `integer` | HTTP status code in `100..599`; event-specific narrower ranges apply below. | Response status observed by the request-timing middleware or route boundary. |
| `path_template` | `string` | Normalized route template, not a full URL and not a query string; for example `/inventory/products/{id}`. | Framework/router route pattern. The producer must not use a path containing concrete IDs, query parameters, or request bodies. |

`product_category` is therefore bounded by the current `Category` enum. `warehouse` is deliberately normalized to the telemetry vocabulary rather than copying the current `Warehouse` enum. `client_id` and the product identifier mapping remain unresolved source decisions, not permission to use `client_name` or a product name.

### 8.2 Mandatory event contracts

The five mandatory events retain all five required inventory properties: `warehouse`, `client_id`, `product_id`, `product_category`, and `quantity`.

#### `inbound_order_created`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `warehouse` | `string` | yes | no | `los_angeles`, `zaragoza` | Normalized warehouse of the persisted `StockEntry` and its SKU. |
| `client_id` | `string` | yes | no | Non-empty opaque stable identifier; authoritative source unresolved | Client owning the SKU. |
| `product_id` | `string` | yes | no | Non-empty stable SKU/Product identifier; mapping to `SKU.id`/`sku_id` pending | SKU receiving the movement. |
| `product_category` | `string` | yes | no | `fashion`, `electronics`, `cosmetics` | Category from the SKU record. |
| `quantity` | `integer` | yes | no | `> 0` | Number of units received by this inbound order/movement; sourced from validated `StockEntry.quantity`. |
| `movement_id` | `string` | optional | no | Non-empty opaque persisted movement identifier | Optional identifier of the persisted `StockEntry` (`id`) if the producer needs operational traceability. It is not an envelope identifier. |

`reference` is not included: it is currently free text and is not needed for the mandatory analytical contract.

#### `outbound_order_created`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `warehouse` | `string` | yes | no | `los_angeles`, `zaragoza` | Normalized warehouse of the persisted `StockExit` and its SKU. |
| `client_id` | `string` | yes | no | Non-empty opaque stable identifier; authoritative source unresolved | Client owning the SKU. |
| `product_id` | `string` | yes | no | Non-empty stable SKU/Product identifier; mapping pending | SKU leaving inventory. |
| `product_category` | `string` | yes | no | `fashion`, `electronics`, `cosmetics` | Category from the SKU record. |
| `quantity` | `integer` | yes | no | `> 0` | Number of units effectively prepared/dispatched for this outbound movement, sourced from validated `StockExit.quantity`. The current `StockExit` persistence boundary is not yet proven to equal real warehouse dispatch; that remains an instrumentation gap. |
| `movement_id` | `string` | optional | no | Non-empty opaque persisted movement identifier | Optional persisted `StockExit.id` for operational traceability. |

Only an outbound operation equivalent to a completed `dispatch` may produce this event. A `StockExit` with `exit_type=loss` does not produce the mandatory `outbound_order_created` event. No separate loss event is introduced here.

#### `stock_threshold_triggered`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `warehouse` | `string` | yes | no | `los_angeles`, `zaragoza` | Warehouse where the threshold condition is observed. |
| `client_id` | `string` | yes | no | Non-empty opaque stable identifier; authoritative source unresolved | Client owning the SKU. |
| `product_id` | `string` | yes | no | Non-empty stable SKU/Product identifier; mapping pending | SKU whose stock crossed the threshold. |
| `product_category` | `string` | yes | no | `fashion`, `electronics`, `cosmetics` | Category from the authoritative SKU. |
| `quantity` | `integer` | yes | no | `>= 0` | Current stock observed immediately after falling below the minimum. The system's stock rule does not permit negative stock. |
| `threshold_quantity` | `integer` | yes | no | `>= 0`; emission requires `quantity < threshold_quantity` | Minimum configured stock that was crossed and the value used by the producer to detect the event. |

#### `direct_stock_edit_rejected`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `warehouse` | `string` | yes | no | `los_angeles`, `zaragoza` | Warehouse targeted by the rejected attempt. |
| `client_id` | `string` | yes | no | Non-empty opaque stable identifier; authoritative source unresolved | Client owning the targeted SKU. |
| `product_id` | `string` | yes | no | Non-empty stable SKU/Product identifier; mapping pending | Targeted SKU. |
| `product_category` | `string` | yes | no | `fashion`, `electronics`, `cosmetics` | Category from the authoritative SKU. |
| `quantity` | `integer` | yes | no | `> 0` | Absolute magnitude, in units, of the stock change that the rejected direct edit would have produced. This meaning is independent of whether a future command represents the attempt as a set or a delta; the future producer must calculate the magnitude before emitting the event. The repository has no direct-stock-edit producer. |

#### `inventory_discrepancy_detected`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `warehouse` | `string` | yes | no | `los_angeles`, `zaragoza` | Warehouse where the physical count is reconciled. |
| `client_id` | `string` | yes | no | Non-empty opaque stable identifier; authoritative source unresolved | Client owning the SKU. |
| `product_id` | `string` | yes | no | Non-empty stable SKU/Product identifier; mapping pending | SKU audited. |
| `product_category` | `string` | yes | no | `fashion`, `electronics`, `cosmetics` | Category from the authoritative SKU. |
| `quantity` | `integer` | yes | no | `> 0` | Absolute magnitude of the detected discrepancy in units. |
| `system_quantity` | `integer` | yes | no | `>= 0` | System-calculated stock at the time of the count. |
| `counted_quantity` | `integer` | yes | no | `>= 0` | Quantity physically counted by the future audit process. |
| `discrepancy_delta` | `integer` | yes | no | Signed; exactly `counted_quantity - system_quantity` and non-zero | Signed reconciliation difference. The required relationship is `quantity = abs(discrepancy_delta)`. |

The audit producer must supply the three quantities from the same reconciliation observation; no current audit flow exists yet.

### 8.3 Additional event contracts

#### `auth_login_succeeded`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `role` | `string` | yes | no | `admin`, `manager`, `user` | Authenticated user's current `Role` enum. |

`status` is omitted because successful event type already conveys success.

#### `auth_login_failed`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `failure_reason` | `string` | yes | no | `account_not_found`, `invalid_password`, `inactive_account` | Normalized branch of the current login route; never the submitted password, email, or raw exception. |
| `role_if_known` | `string` | optional | no | `admin`, `manager`, `user` | Role only when a safely resolved inactive/known account permits it; absent for unknown accounts and invalid-password attempts. |

#### `auth_password_reset_requested`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `request_outcome_class` | `string` | yes | no | `account_not_found`, `reset_email_sent`, `delivery_failed` | Internal bounded classification of the route outcome. It must not be exposed in the generic client response or reveal account existence; no email or reset token is stored. |

`status` is omitted because it would add no meaning to the bounded outcome classification.

#### `auth_password_reset_completed`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| *(none)* | object | no | n/a | `properties` may be `{}` | The event is emitted only after the password update succeeds. No non-redundant analytical dimension is currently justified; tokens and password values are prohibited. |

#### `inventory_validation_failed`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `operation` | `string` | yes | no | `inbound`, `outbound`, `product_create` | Inventory route/operation being validated, based on current product and movement endpoints. |
| `failure_reason` | `string` | yes | no | `unknown_product`, `warehouse_mismatch`, `invalid_payload` | Bounded normalized validation class supported by current route/schema behavior. Insufficient stock is excluded and belongs only to `inventory_stock_insufficient`. |
| `warehouse` | `string` | optional | no | `los_angeles`, `zaragoza` | Normalized warehouse when present and known at rejection time. |
| `product_id` | `string` | optional | no | Non-empty stable identifier; mapping pending | SKU/Product identifier when present and safely known. |
| `status_code` | `integer` | optional | no | `400..499` for these client/domain validation failures | HTTP status observed at the validation boundary. |

Raw error messages and request payloads are not properties.

#### `inventory_stock_insufficient`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `warehouse` | `string` | yes | no | `los_angeles`, `zaragoza` | Normalized warehouse checked by `create_stock_exit`. |
| `client_id` | `string` | yes | no | Non-empty opaque stable identifier; authoritative source unresolved | Client owning the SKU. |
| `product_id` | `string` | yes | no | Non-empty stable SKU/Product identifier; mapping pending | SKU for the rejected outbound request. |
| `product_category` | `string` | yes | no | `fashion`, `electronics`, `cosmetics` | Category from the SKU. |
| `requested_quantity` | `integer` | yes | no | `> 0` | Quantity requested by the outbound payload. |
| `available_quantity` | `integer` | yes | no | `>= 0` | Stock calculated immediately before rejection. The rejection condition is exactly `requested_quantity > available_quantity`. |

#### `api_request_slow`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `method` | `string` | yes | no | Uppercase HTTP method token, such as `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` | Normalized from `request.method` by the existing timing middleware. |
| `path_template` | `string` | yes | no | Normalized route template; no concrete IDs or query strings | Route measured by the timing middleware. |
| `status_code` | `integer` | yes | no | `100..599` | HTTP response status. |
| `duration_ms` | `number` | yes | no | `>= 0` | Total request duration measured in milliseconds. |
| `threshold_ms` | `number` | yes | no | `>= 0`; emission requires `duration_ms > threshold_ms` | Configured threshold that justified this event. |

#### `api_request_failed`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `method` | `string` | yes | no | Uppercase HTTP method token | Normalized request method. |
| `path_template` | `string` | yes | no | Normalized route template; no concrete IDs or query strings | Route observed by the timing middleware. |
| `status_code` | `integer` | yes | no | `500..599` | Technical server-failure response status; this event excludes 4xx responses. |
| `duration_ms` | `number` | yes | no | `>= 0` | Total request duration in milliseconds. |

`status_class` is omitted because it would always be `5xx` and is redundant with the constrained `status_code`. Stack traces, raw exceptions, request bodies, URLs, and query strings are excluded.

#### `backoffice_section_entered`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `section` | `string` | yes | no | `inventory_products`, `inventory_orders`, `suppliers`, `incidents` | Small bounded taxonomy derived from the current backoffice route groups, not individual URLs. |
`route_group` and `entry_source` are omitted from the v1 contract. `route_group` would duplicate the bounded `section` value, while `entry_source` has no taxonomy that can currently be defined authoritatively.

#### `inventory_workflow_started`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `workflow` | `string` | yes | no | `inbound`, `outbound` | Current two inventory order flows, based on the two real backoffice routes. |
`section` is omitted because the workflow already identifies the inventory order section.

#### `inventory_workflow_abandoned`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `workflow` | `string` | yes | no | `inbound`, `outbound` | Same bounded workflow taxonomy as `inventory_workflow_started`. |
| `abandonment_stage` | `string` | yes | no | `before_submit`, `after_submit` | `before_submit` means the workflow ends without any attempt to create the operation. `after_submit` means at least one creation attempt occurred, but no creation succeeded before abandonment. |
| `had_validation_error` | `boolean` | yes | no | `true` or `false` | Whether a client-side validation error had occurred before abandonment, if observed by the form. |
| `duration_ms` | `number` | yes | no | `>= 0` | Time from workflow start to the abandonment observation. |

These two stages are the complete v1 abandonment vocabulary; no additional stages are defined.

#### `inventory_product_created`

| Property | JSON type | Required | Nullable | Allowed values / constraints | Meaning / source |
| --- | --- | --- | --- | --- | --- |
| `warehouse` | `string` | yes | no | `los_angeles`, `zaragoza` | Normalized warehouse of the newly persisted SKU. |
| `client_id` | `string` | yes | no | Non-empty opaque stable identifier; authoritative source unresolved | Client owning the new SKU. |
| `product_id` | `string` | yes | no | Non-empty stable SKU/Product identifier; mapping pending | Identifier of the newly persisted SKU. |
| `product_category` | `string` | yes | no | `fashion`, `electronics`, `cosmetics` | Validated category of the new SKU. |

`actor_role` is omitted. Although the authentication model has a real role enum, the current product route does not use the authenticated user as a route parameter and the event contract does not need to duplicate actor identity or add a role dimension without an explicit analytical requirement.

### 8.4 Bounded taxonomies

| Property / dimension | Taxonomy | Status / source |
| --- | --- | --- |
| `warehouse` | `los_angeles`, `zaragoza` | Defined by this telemetry contract; normalized from current `LA`/`ZGZ` values. |
| `product_category` | `fashion`, `electronics`, `cosmetics` | Defined by the current `Category` enum in `inventory_schemas.py`. |
| `role`, `role_if_known` | `admin`, `manager`, `user` | Defined by the current authentication `Role` enum. |
| `workflow` | `inbound`, `outbound` | Defined by the two current inventory order routes/forms. |
| `failure_reason` (auth) | `account_not_found`, `invalid_password`, `inactive_account` | Derived from the three real login rejection branches; internal only. |
| `request_outcome_class` | `account_not_found`, `reset_email_sent`, `delivery_failed` | Bounded internal classification of current reset flow; must not affect anti-enumeration client behavior. |
| `operation` | `inbound`, `outbound`, `product_create` | Derived from current inventory routes. |
| `failure_reason` (inventory validation) | `unknown_product`, `warehouse_mismatch`, `invalid_payload` | Normalized classes supported by current validation behavior; insufficient stock is separate. |
| `method` | Uppercase HTTP method token | Defined by HTTP and normalized from the current middleware. |
| `section` | `inventory_products`, `inventory_orders`, `suppliers`, `incidents` | Derived from current backoffice route groups. |
| `abandonment_stage` | `before_submit`, `after_submit` | Defined by whether any creation attempt occurred before abandonment. |
| `status_code` | HTTP `100..599`, with event-specific restrictions | Defined by HTTP; `api_request_failed` is restricted to `500..599`, validation failures to `400..499`. |

No unresolved taxonomy may be emitted as arbitrary free text. It must be closed from an authoritative producer/source before instrumentation.

## Delivery strategy

### Stream vs batch

The strategy below describes downstream processing, not the moment at which an
event is emitted. All events may be captured at the instrumentation point
described in the catalog. A `batch` decision means that periodic processing is
adequate for the current operational or business decision; it does not require
the producer to wait before recording the event. A `stream` decision means that
the event's primary value depends on making it available for an operational
decision within seconds to minutes. Where an event has both uses, the table
identifies the primary strategy and names the secondary use explicitly.

| event_type | strategy | latency requirement | rationale | consumer/use case |
| --- | --- | --- | --- | --- |
| `inbound_order_created` | batch | weekly / periodic | The current hypothesis is about inbound volume by client and warehouse for capacity and staffing planning. There is no explicit need to react within seconds to each receipt; the event can be emitted immediately and aggregated later. | Primary: future weekly reporting pipeline for capacity planning; secondary: future analytical job for inbound trends. |
| `outbound_order_created` | batch | periodic / weekly | Throughput and bottleneck analysis do not have a specified seconds-level SLA in the approved hypothesis. Frequent operational aggregation is sufficient until a concrete rapid-response requirement is established. | Future analytical job for throughput and bottleneck trends; future weekly reporting pipeline. |
| `stock_threshold_triggered` | stream | seconds to minutes | A low-stock condition loses operational value if notification and intervention are delayed until a periodic report. Rapid availability supports action before a stockout. | Future warehouse operations monitoring and future low-stock operational monitor; secondary consumption by the weekly reporting pipeline. |
| `direct_stock_edit_rejected` | stream | seconds to minutes | An individual rejected attempt may indicate an immediate governance or permission concern. The individual event should be available promptly for investigation, while frequency by warehouse is a separate analytical aggregation. | Future security/governance monitoring for individual attempts; secondary future batch analysis of frequency by warehouse. |
| `inventory_discrepancy_detected` | batch | periodic / weekly | The event comes from a discrete audit or physical-count process. The current decision is to prioritize audits by discrepancy frequency or rate, so seconds-level delivery is not established as necessary. | Future inventory reconciliation analytical job and weekly reporting pipeline for discrepancy trends. |
| `auth_login_succeeded` | batch | periodic / daily | Successful login is primarily an adoption and access-pattern signal. No decision in the current hypothesis loses material value when processed after the login event. | Account support analytics and future daily usage/adoption analytical job. |
| `auth_login_failed` | stream | seconds to minutes | A single failure is useful for support, but the security value comes from detecting a concentration while it is still actionable. Prompt availability supports conceptual security/governance monitoring; later aggregation remains useful for support. | Future security/governance monitoring for suspicious concentrations; secondary account support analytics. |
| `auth_password_reset_requested` | batch | periodic / daily | Reset demand is used to understand recovery friction and support volume. The current hypothesis does not identify a decision that loses value if processed after the request. | Account support analytics and future daily recovery-demand analytical job. |
| `auth_password_reset_completed` | batch | periodic / daily | Completion rates and recovery friction are historical funnel measures. They can be compared with requests in a periodic job without requiring immediate operational reaction. | Account support analytics and future daily recovery-funnel job. |
| `inventory_validation_failed` | batch | periodic / daily | The approved use is to analyze validation friction and data quality by failure class. Individual failures should be captured immediately, but downstream aggregation does not require seconds-level processing. | Product/workflow analytics and future daily validation-quality job. |
| `inventory_stock_insufficient` | stream | seconds to minutes | A rejected outbound operation can affect fulfillment immediately. Prompt visibility supports replenishment, reconciliation, or operational intervention before additional outbound work is affected. | Warehouse operations monitoring and future stock-availability operational monitor; secondary periodic fulfillment analysis. |
| `inventory_product_created` | batch | periodic / daily | Product creation is master-data activity used for onboarding load and data-quality analysis, not an event requiring immediate operational reaction under the current hypothesis. | Product/workflow analytics and future daily master-data analytical job. |
| `api_request_slow` | stream | seconds to minutes | The event is valuable for detecting technical degradation while it can still affect operators. A historical performance report alone would delay operational investigation, although the same events may later feed trend reporting. | Future technical reliability monitoring for degraded routes; secondary performance reporting. |
| `api_request_failed` | stream | seconds to minutes | Technical 5xx failures can represent an active service problem. Prompt detection supports operational reliability investigation; periodic reporting remains a secondary use, not the primary delivery strategy. | Future technical reliability monitoring for server failures; secondary reliability trend reporting. |
| `backoffice_section_entered` | batch | periodic / daily | Section entry measures navigation and feature usage. Capturing the event immediately is compatible with processing it later because no approved decision requires intervention during the session. | Product/workflow analytics and future daily navigation-usage job. |
| `inventory_workflow_started` | batch | periodic / daily | Starts are used to compare demand for inbound and outbound workflows and to guide usability or staffing analysis. The current hypothesis does not require immediate reaction to an individual start. | Product/workflow analytics and future daily workflow-funnel job. |
| `inventory_workflow_abandoned` | batch | periodic / daily | Abandonment is a funnel and friction signal. Immediate capture preserves the event, while daily aggregation is sufficient to identify affected forms and guidance needs. | Product/workflow analytics and future daily workflow-funnel job. |

The four events used by the future weekly pipeline remain identifiable even when
their primary delivery strategy differs: `inbound_order_created`,
`outbound_order_created`, `stock_threshold_triggered`, and
`inventory_discrepancy_detected`. In particular, a stream-delivered
`stock_threshold_triggered` event can be consumed promptly by an operational
monitor and also retained for a later weekly aggregation. Delivery strategy and
downstream consumption strategy are separate decisions; the weekly pipeline
does not require every contributing event to have `batch` as its primary
strategy.

### Volume controls

Volume controls apply to emission or downstream processing policy; they do not
change the semantic definition of an event. The default for TrackFlow business,
security, audit, and workflow facts is `none`: every valid occurrence is
preserved. A retry of one logical event is handled by `eventId`-based downstream
deduplication, not by throttle, debounce, or sampling.

| event_type | control | policy | rationale |
| --- | --- | --- | --- |
| `inbound_order_created` | none | Preserve every valid inbound order/movement occurrence. Do not sample. | Exact inbound volume is needed for capacity and staffing analysis; losing occurrences would distort counts and rates. |
| `outbound_order_created` | none | Preserve every completed outbound order occurrence. Do not sample. | Exact completed throughput is needed to analyze rates and bottlenecks. |
| `stock_threshold_triggered` | none | The producer must emit only the transition into the below-threshold state (edge-triggering). A later recovery above the threshold followed by another fall is a new valid occurrence. | Throttle must not hide an incorrectly repetitive producer. Each legitimate threshold transition is operationally meaningful and should be retained. |
| `direct_stock_edit_rejected` | none | Preserve every rejected direct-edit attempt. Do not sample; any future alert grouping is separate from the event record. | Individual attempts may be evidence for governance or security investigation, even when repeated attempts create higher volume. |
| `inventory_discrepancy_detected` | none | Preserve every confirmed discrepancy from an audit or physical count. Do not sample. | Exact discrepancy counts and traceability are needed for reconciliation and audit prioritization. |
| `auth_login_succeeded` | none | Preserve every successful login occurrence. Do not sample. | Adoption, access, and session-volume measures require real counts rather than estimates. |
| `auth_login_failed` | none | Preserve every failed login occurrence. Future alerts may group or rate-limit notifications, but not the underlying event record. | Individual failures support security investigation and support; retention of the base event must not be confused with alert throttling. |
| `auth_password_reset_requested` | none | Preserve every reset request occurrence. Do not sample. | Each request contributes to the recovery funnel and support-volume analysis. |
| `auth_password_reset_completed` | none | Preserve every completed reset occurrence. Do not sample. | Exact completions are needed to measure recovery conversion and friction. |
| `inventory_validation_failed` | none | Preserve every validation failure with its bounded failure class. Do not sample. | Sampling could distort which validation classes block warehouse work most frequently. |
| `inventory_stock_insufficient` | none | Preserve every insufficient-stock rejection. Do not sample. | Exact rejection counts are needed for fulfillment rates and replenishment or reconciliation analysis. |
| `inventory_product_created` | none | Preserve every successful product-creation occurrence. Do not sample. | Each product creation is a business/master-data fact used for onboarding and data-quality analysis. |
| `api_request_slow` | throttle | For each bounded `method + path_template` key, emit at most one degradation signal per configured operational window while the condition continues. The window is configuration for future instrumentation; do not use debounce. Complete latency metrics belong to a separate future measurement system. | A sustained degradation can generate high volume, while one signal per route and operational window preserves evidence that it continues. Throttle limits signal volume without delaying the initial detection. |
| `api_request_failed` | none | Preserve every HTTP 5xx event. Do not sample or throttle because this event supports exact error-rate analysis; any future alert grouping is separate. | Every server failure may contribute to failure rates and reliability investigation, so reducing occurrences would weaken the technical-health signal. |
| `backoffice_section_entered` | none | Preserve every supported section-entry occurrence. Do not add debounce or throttle without evidence of accidental duplicate navigation. | Current hypotheses require usage counts, and the frontend does not establish a demonstrated repetition problem that justifies losing entries. |
| `inventory_workflow_started` | none | Preserve every valid workflow start. Do not sample. | Each start is a funnel denominator for started-to-completed or abandoned analysis. |
| `inventory_workflow_abandoned` | none | Preserve every finalized abandonment occurrence. Do not debounce; the event already represents the terminal outcome rather than intermediate interaction. | Each abandonment contributes to the workflow funnel and friction analysis. |

No v1 event uses `sampling`. The catalog contains events for exact business
counts, rates, audit/governance evidence, security signals, or workflow funnels;
sampling would either lose meaningful occurrences or distort the approved
hypotheses. No v1 event uses `debounce`: none is defined as a repeated signal
whose intermediate occurrences have no value, and debouncing would delay or
erase facts such as failures, threshold transitions, or workflow outcomes.

### Deduplication versus volume control

These concepts address different situations:

- **Duplicate delivery:** a retry or redelivery of the same logical event keeps
	the same `eventId` and must be deduplicated downstream. This is not throttle,
	debounce, or sampling.
- **Repeated legitimate event:** two valid occurrences have different
	`eventId` values and must both be retained, even when their properties are
	identical. For example, two distinct rejected attempts are not duplicates.
- **Volume control:** throttle, debounce, or sampling changes which signals are
	emitted or processed according to a policy. It must not be used to conceal a
	producer that emits the same logical event repeatedly or to replace
	`eventId`-based deduplication.

For `stock_threshold_triggered`, edge detection belongs to the producer: the
producer must establish the transition into the below-threshold state. This is
semantic correctness, not a volume-control mechanism. For `api_request_slow`,
the throttle is intentionally limited to the bounded route key and configured
operational window; it is not a claim that the underlying latency measurements
are sampled or that every slow request is counted by this event.

### Risks and exclusions

#### Design risks

| Risk | Impact | Mitigation / design decision |
| --- | --- | --- |
| Duplicate delivery | Retries or redelivery can cause double counts, incorrect rates, and inaccurate weekly metrics. | `eventId` identifies one logical event instance; retries reuse the same `eventId`; downstream consumers must deduplicate. This does not remove legitimate distinct events with different `eventId` values. |
| Event loss | A failure between a business operation and event capture can produce a confirmed business action with missing telemetry, causing undercounting and incomplete, non-reconcilable decisions. | Future instrumentation must address atomicity and reliability between the business commit and event capture. This phase does not design a transactional outbox or other implementation. |
| Semantic producer mismatch | Emitting `outbound_order_created` at `StockExit` persistence when picking and dispatch are not complete could inflate throughput. | During instrumentation, validate that the producer represents completed picking and dispatch before emitting. The current event contract is unchanged. |
| Missing authoritative `client_id` | Using unstable client values can create inconsistent joins, cardinality, and incorrect client aggregations. | Do not emit mandatory inventory events until `client_id` can be resolved from a stable authoritative source. Do not invent IDs or silently substitute `client_name`. |
| Warehouse normalization | `LA`, `ZGZ`, `Los Angeles`, and `Zaragoza` can fragment metrics when telemetry requires `los_angeles` and `zaragoza`. | Normalize warehouse values to the canonical telemetry values before emission. |
| High-cardinality dimensions | Raw paths, exception messages, full URLs, query strings, free-form referrers, and error text make analysis expensive and difficult. | Use `path_template`, bounded failure reasons, and bounded section/workflow values. Do not emit raw exception or error messages. |
| Schema drift | Independently evolving producers and consumers can cause rejected events, incorrect interpretation, and inconsistent reporting. | Use `schemaVersion`, treat `event-schemas.json` as the contract, apply the defined major/minor policy, and validate changes before instrumentation. No schema registry is designed here. |
| Cross-field semantic rules | A structurally valid payload can still be inconsistent, such as invalid quantity/threshold, discrepancy, stock, or duration relationships. | Validate domain relationships in the producer/domain before emission. Do not add custom JSON Schema keywords in this phase. |
| Missing session/request correlation | Without centralized `sessionId` and `requestId`, frontend, backend, and logs are harder to correlate, funnels may be incomplete, and technical diagnosis is harder. | Future instrumentation must establish and propagate correlation according to the approved Event Envelope. |
| Volume spikes | Sustained degradation can generate a large volume of `api_request_slow` signals. | Use the approved throttle keyed by `method + path_template` during a configured operational window. Other events remain unlimited unless later evidence justifies a change. |
| Sampling bias | Sampling can alter counts, rates, security evidence, auditability, and funnel measures. | v1 uses no sampling. |
| Privacy leakage | Accidental capture of passwords, JWTs/access tokens, reset tokens, analytical email values, API keys/secrets, consumer PII, recipient data, carrier information, or raw request bodies can expose sensitive data. | Enforce the closed property allowlist with `additionalProperties: false` and the documented exclusions. |

#### Exclusions

The following are explicitly outside the TrackFlow telemetry plan v1. Some may
be addressed in future projects, but they are not part of the current contract:

- final-consumer personal data;
- recipient or shipping-destination information;
- carrier and last-mile tracking;
- passwords, tokens, and secrets;
- raw request or response bodies;
- raw stack traces and exception messages;
- a concrete transport infrastructure;
- Kafka, SQS, Redis, or an event bus;
- retention periods;
- data warehouse implementation;
- alert implementation;
- dashboard implementation;
- weekly reporting pipeline implementation;
- producer implementation;
- reconciliation or audit workflow implementation.

#### Considered but not included in v1

These signals or events were considered but are deliberately not catalog events
in v1:

| Considered signal/event | Decision and reason |
| --- | --- |
| `generic button_clicked` | Excluded because it adds cardinality and noise with weak semantics. The existing navigation and workflow events answer the relevant questions more directly. |
| `generic page_viewed` | Excluded because `backoffice_section_entered` provides a bounded semantic dimension; instrumenting every URL is unnecessary. |
| `generic error_occurred` | Excluded because it mixes technical and business errors. Specific events already cover `inventory_validation_failed`, `inventory_stock_insufficient`, `api_request_failed`, and `auth_login_failed`. |
| `stock_updated` | Excluded as a generic event because direct stock editing is prohibited. Legitimate changes are represented by `inbound_order_created` and `outbound_order_created`, preserving traceability. |
| outbound loss event | Not added merely because the current code has `exit_type=loss`. It was not required by the approved context, is not part of `outbound_order_created`, and lacks an approved hypothesis and decision. |
| `raw request event` | Excluded because of high cardinality and privacy risk. Technical middleware events use bounded dimensions instead. |
| every slow request | Not retained as an unlimited individual `api_request_slow` event. The approved signal uses throttle by `method + path_template`; complete latency metrics remain outside this plan. |

## 9. Known implementation gaps

The mandatory contract is intentionally ahead of the current implementation in several areas:

- `client_id` does not yet exist as a stable field in the current inventory model. The system currently provides `client_name`; future instrumentation must resolve `client_id` from an authoritative source without inventing one or silently substituting the name.
- The backend uses `LA` and `ZGZ`, while telemetry requires `los_angeles` and `zaragoza`. Instrumentation must apply the documented mapping.
- `stock_threshold_triggered` does not yet have a centralized backend producer.
- `inventory_discrepancy_detected` does not yet have an implemented warehouse-audit or reconciliation flow.
- `direct_stock_edit_rejected` does not currently have an explicit direct-stock-edit endpoint acting as a producer.

These are future instrumentation limitations. They do not relax, rename, or remove any of the five mandatory event types or their required inventory properties.
