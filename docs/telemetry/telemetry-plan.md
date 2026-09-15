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
| `relevant properties` | `auth_method`, `role`, `status` |

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
| `relevant properties` | `section`, `route_group`, `entry_source` |

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
| `relevant properties` | `workflow`, `section`, `entry_source` |

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

## 7. Known implementation gaps

The mandatory contract is intentionally ahead of the current implementation in several areas:

- `client_id` does not yet exist as a stable field in the current inventory model. The system currently provides `client_name`; future instrumentation must resolve `client_id` from an authoritative source without inventing one or silently substituting the name.
- The backend uses `LA` and `ZGZ`, while telemetry requires `los_angeles` and `zaragoza`. Instrumentation must apply the documented mapping.
- `stock_threshold_triggered` does not yet have a centralized backend producer.
- `inventory_discrepancy_detected` does not yet have an implemented warehouse-audit or reconciliation flow.
- `direct_stock_edit_rejected` does not currently have an explicit direct-stock-edit endpoint acting as a producer.

These are future instrumentation limitations. They do not relax, rename, or remove any of the five mandatory event types or their required inventory properties.
