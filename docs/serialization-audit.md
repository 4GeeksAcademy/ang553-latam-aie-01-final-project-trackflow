# Backend Serialization Audit

## 1. Scope and methodology

This audit classifies the FastAPI backend's output contracts using only the evidence gathered in the preceding inventory, implicit-contract, inventory-order integrity, consumer-tracing, and ambiguity-correction phases. The backend uses Pydantic v2, TinyDB, and SQLModel where applicable.

The evidence base combines static inspection of routers, schemas, persistence and frontend API consumers with inspection of runtime `APIRoute` registrations and OpenAPI output. The inventory identified **33 method+path registrations**: **27 OpenAPI-visible routes** and **6 hidden supplier aliases**. The initial baseline was **136 tests**; after the inventory-integrity fix it was **138 tests**.

Aliases count as real registrations and therefore receive their own rows below. Routes without a confirmed internal runtime consumer are preserved rather than optimized solely on that basis, because external clients may exist. `204 No Content` and CSV endpoints are evaluated as HTTP contracts; this audit does not propose artificial JSON serializers for them.

## 2. Classification legend

- **✅ COMPLIANT** — The endpoint has an explicit, safe output contract appropriate to the known consumer, with no objective evidence of relevant over-fetching.
- **⚠️ OPTIMIZE / CONTRACT ADJUSTMENT** — A contract exists or is partly explicit, but the known consumer receives unnecessary fields, a relationship is oversized, a write response is broader than needed, or the special HTTP contract should be made more explicit.
- **❌ MISSING EXPLICIT RESPONSE CONTRACT** — The stable JSON output depends on a generic dict, inferred `dict[str, str]`, non-nominal shape, or incorrect/missing OpenAPI response contract.

## 3. Executive summary

The counts below are derived from the 33 rows in the complete endpoint table.

| Measure | Count |
|---|---:|
| Total method+path registrations | 33 |
| ✅ Compliant | 23 |
| ⚠️ Optimize / contract adjustment | 10 |
| ❌ Missing explicit response contract | 0 |
| Non-JSON/no-content special contracts | 4 |
| Registrations with no internal consumer found | 13 |

The four special contracts are the three `204 No Content` registrations (one user delete and two supplier-delete aliases) and the CSV export. They are included in the classification counts above and are not expected to receive JSON/Pydantic content models.

## 4. Complete endpoint audit

| Method | Path | Current response contract | Consumer evidence | Status | Problem | Target output contract |
|---|---|---|---|---|---|---|
| POST | `/auth/login` | `TokenResponse` | Backoffice login consumes the token response | ✅ COMPLIANT | None identified | `TokenResponse`: `access_token`, `token_type` |
| GET | `/auth/me` | `AuthMeResponse` | Uses `id`, `email`, `is_active`, `role`; does not read `created_at` | ✅ COMPLIANT | Implemented: response is limited to the confirmed identity fields | `AuthMeResponse`: `id`, `email`, `is_active`, `role` |
| POST | `/auth/forgot-password` | `MessageResponse` | UI uses `message` | ✅ COMPLIANT | None identified | Keep `MessageResponse`: `message` |
| POST | `/auth/reset-password` | `MessageResponse` | Current consumer ignores `message`; stable generic confirmation remains safe | ✅ COMPLIANT | No objective issue; removing one message field is not justified | Keep `MessageResponse`: `message` |
| POST | `/auth/change-password` | `MessageResponse` | UI uses `message` | ✅ COMPLIANT | None identified | Keep `MessageResponse`: `message` |
| POST | `/users` | `RegistrationResponse` | Parses JSON but reads no field; performs a separate login afterward | ✅ COMPLIANT | Implemented: registration returns only a nominal acknowledgement | `RegistrationResponse`: `message` |
| GET | `/users` | `list[UserResponse]` with safe projection | No internal runtime consumer confirmed | ✅ COMPLIANT | No evidence sufficient to reduce an administrative/external contract | Preserve explicit `UserResponse` safe projection |
| GET | `/users/{user_id}` | `UserResponse` with safe projection | No internal runtime consumer confirmed | ✅ COMPLIANT | No evidence sufficient to reduce an administrative/external contract | Preserve explicit `UserResponse` safe projection |
| PUT | `/users/{user_id}` | `UserResponse` with safe projection | No internal runtime consumer confirmed | ✅ COMPLIANT | No evidence sufficient to reduce an administrative/external contract | Preserve explicit `UserResponse` safe projection |
| DELETE | `/users/{user_id}` | HTTP 204 without body | No JSON consumer; deletion is status-based | ⚠️ OPTIMIZE / CONTRACT ADJUSTMENT | No-content contract can be made more explicit | Explicit `204 No Content` using `Response`/`response_class` and status documentation; no body |
| GET | `/profiles/me` | `ProfileMeResponse` | Uses `name`, `phone`, `address`; does not use `id`, `user_id` | ✅ COMPLIANT | Implemented: response is limited to the UI profile fields | `ProfileMeResponse`: `name`, `phone`, `address`, preserving current nullability |
| PUT | `/profiles/me` | `ProfileMeResponse` | Uses `name`, `phone`, `address`; does not use `id`, `user_id` | ✅ COMPLIANT | Implemented: response is limited to the UI profile fields | `ProfileMeResponse`: `name`, `phone`, `address`, preserving current nullability |
| GET | `/inventory/products` | `list[SKUResponse]` | Uses `id`, `name`, `sku`, `client_name`, `category`, `warehouse`, `current_stock` | ✅ COMPLIANT | All response fields are used | Keep `list[SKUResponse]` |
| GET | `/inventory/products/{id}` | `SKUResponse` | Wrapper exists, but executed consumer is not confirmed | ✅ COMPLIANT | No internal consumer evidence for a reduction | Preserve current explicit safe contract |
| POST | `/inventory/products` | `SKUResponse` | No runtime consumer confirmed | ✅ COMPLIANT | Absence of a consumer is not evidence of unnecessary fields | Preserve request-specific input and explicit `SKUResponse` |
| POST | `/inventory/orders/inbound` | `MovementCreatedResponse` | No response field used; consumer only observes success/failure | ✅ COMPLIANT | Implemented minimal creation response | `MovementCreatedResponse`: `id` |
| POST | `/inventory/orders/outbound` | `MovementCreatedResponse` | No response field used; consumer only observes success/failure | ✅ COMPLIANT | Implemented minimal creation response | `MovementCreatedResponse`: `id` |
| GET | `/inventory/orders` | `list[InventoryOrderListItem]` | Uses all projected movement fields plus `sku_name` and `sku_code` | ✅ COMPLIANT | Implemented flat HTTP projection; integrity lookup remains internal | `InventoryOrderListItem`: exact flat movement and SKU name/code fields |
| GET | `/api/suppliers` | `list[SupplierResponse]` | Supplier list uses every `SupplierResponse` field | ✅ COMPLIANT | None identified | Keep `list[SupplierResponse]` |
| GET | `/suppliers` | Same handler/`list[SupplierResponse]` as `/api/suppliers` | No internal consumer confirmed for canonical path | ✅ COMPLIANT | Preserve canonical alias for compatibility | Keep `list[SupplierResponse]` |
| GET | `/api/suppliers/{supplier_id}` | `SupplierResponse` | No internal consumer confirmed | ✅ COMPLIANT | Explicit and safe; no reduction based only on missing consumer | Preserve `SupplierResponse` |
| GET | `/suppliers/{supplier_id}` | Same handler/`SupplierResponse` as `/api/suppliers/{supplier_id}` | No internal consumer confirmed | ✅ COMPLIANT | Explicit and safe; preserve alias | Preserve `SupplierResponse` |
| POST | `/api/suppliers` | `SupplierResponse` | Ignores returned object and immediately calls `fetchSuppliers()` | ⚠️ OPTIMIZE / CONTRACT ADJUSTMENT | Mutation returns 11 fields that the consumer discards | `SupplierMutationResponse`: `id` or equivalent minimal nominal acknowledgement |
| POST | `/suppliers` | Same handler/`SupplierResponse` as `/api/suppliers` | No internal consumer confirmed for canonical path; `/api` consumer refetches | ⚠️ OPTIMIZE / CONTRACT ADJUSTMENT | Shared handler has an oversized write response | Same `SupplierMutationResponse` for both aliases: `id` |
| PATCH | `/api/suppliers/{supplier_id}/rate` | `SupplierResponse` | Response ignored; list is refetched | ⚠️ OPTIMIZE / CONTRACT ADJUSTMENT | Mutation returns fields the consumer discards | `SupplierMutationResponse`: `id`, `updated_at` |
| PATCH | `/suppliers/{supplier_id}/rate` | Same handler/`SupplierResponse` as `/api` rate alias | No internal consumer confirmed for canonical path; `/api` consumer refetches | ⚠️ OPTIMIZE / CONTRACT ADJUSTMENT | Shared handler has an oversized write response | Same `SupplierMutationResponse`: `id`, `updated_at` |
| PATCH | `/api/suppliers/{supplier_id}/status` | `SupplierResponse` | Response ignored; list is refetched | ⚠️ OPTIMIZE / CONTRACT ADJUSTMENT | Mutation returns fields the consumer discards | `SupplierMutationResponse`: `id`, `updated_at` |
| PATCH | `/suppliers/{supplier_id}/status` | Same handler/`SupplierResponse` as `/api` status alias | No internal consumer confirmed for canonical path; `/api` consumer refetches | ⚠️ OPTIMIZE / CONTRACT ADJUSTMENT | Shared handler has an oversized write response | Same `SupplierMutationResponse`: `id`, `updated_at` |
| DELETE | `/api/suppliers/{supplier_id}` | HTTP 204 without body | No JSON consumer; deletion is status-based | ⚠️ OPTIMIZE / CONTRACT ADJUSTMENT | No-content contract can be made more explicit | Explicit `204 No Content`; no JSON/Pydantic body |
| DELETE | `/suppliers/{supplier_id}` | Same handler/HTTP 204 as `/api` alias | No internal consumer confirmed for canonical path | ⚠️ OPTIMIZE / CONTRACT ADJUSTMENT | Shared alias should document the same no-content contract | Explicit `204 No Content`; no JSON/Pydantic body |
| POST | `/api/incidents/analyze` | `IncidentAnalysisResponse` | Consumer uses all ten fields | ✅ COMPLIANT | None identified | `IncidentAnalysisResponse`: exact fields listed in section 5 |
| GET | `/api/incidents/results/export` | Runtime `text/csv`; OpenAPI currently describes empty `application/json` | Export is a file response, not a JSON object | ⚠️ OPTIMIZE / CONTRACT ADJUSTMENT | HTTP/OpenAPI content type is incorrect or incomplete | Explicit CSV contract: `text/csv`, suitable `Response`/`response_class`, correct `responses`, and documented `Content-Disposition` when applicable |
| GET | `/health` | `HealthResponse` | No internal consumer confirmed; infrastructure may consume it | ✅ COMPLIANT | None identified | `HealthResponse`: `status: str`; preserve body |

## 5. Endpoint details requiring changes

### POST `/auth/login`

**Implemented:** The route uses the nominal `TokenResponse` schema. The known backoffice login flow consumes the token.

**Final contract:** `TokenResponse` with exactly:

- `access_token`
- `token_type`

Keep `token_type` even though current code does not branch on it: it is part of the standard Bearer/OAuth2 contract. Do not include `email`, `user`, `role`, `password`, or `hashed_password`.

### GET `/auth/me`

**Implemented:** The route uses `AuthMeResponse` and projects only the confirmed identity fields.

**Final contract:** `AuthMeResponse` with exactly `id`, `email`, `is_active`, and `role`. Do not remove `email`: this endpoint represents authenticated identity and the consumer uses it.

### POST `/users`

**Implemented:** The client parses JSON but reads no returned field, then performs a separate login whose token establishes the session. The route now returns only a nominal registration acknowledgement.

**Final contract:** `RegistrationResponse` with exactly `message`. The stable message is `User registered successfully.` Do not return `email`, `role`, `is_active`, `created_at`, or `hashed_password`.

### DELETE `/users/{user_id}`

**Current:** The route has no body and returns 204.

**Target:** Make the HTTP contract explicit with `Response`/`response_class`, status documentation, or equivalent FastAPI metadata. Keep 204 and keep the body empty. Do not invent a JSON response model.

### GET and PUT `/profiles/me`

**Implemented:** Both routes project `ProfileMeResponse`. The UI reads only `name`, `phone`, and `address`; IDs are not used.

**Final contract:** `ProfileMeResponse` with exactly `name`, `phone`, and `address`, preserving current nullability. The IDs are removed from this UI-specific projection based on consumer evidence, not merely because they are internal.

### POST `/inventory/orders/inbound` and `/inventory/orders/outbound`

**Current:** The inbound and outbound write routes return full `StockEntryResponse` and `StockExitResponse` objects. The consumer uses no response field and only distinguishes success/failure.

**Target:** A shared `MovementCreatedResponse` with exactly `id`. This preserves a useful reference to the created resource while removing discarded metadata. Do not change status codes in this phase.

### GET `/inventory/orders`

**Current:** The response includes top-level `id`, `movement_type`, `sku_id`, `quantity`, `warehouse`, `created_at`, `user_uuid`, `reference`, `exit_type`, `tracking_number`, plus nested `sku` fields `id`, `name`, `sku`, `client_name`, `category`, and `warehouse`. The consumer uses all listed top-level fields except `sku_id`, and uses only `sku.name` and `sku.sku` from the relation.

**Target:** `InventoryOrderListItem` with exactly:

- `id`
- `movement_type`
- `quantity`
- `warehouse`
- `created_at`
- `user_uuid`
- `sku_name`
- `sku_code`
- `reference`
- `exit_type`
- `tracking_number`

Remove `sku_id`, the nested `sku` object, and unused `SKUSummary` fields from the HTTP projection. The `sku` relationship remains required internally for integrity and lookup; only the HTTP projection is flattened.

### Supplier mutation aliases

**Current:** `/api/suppliers` and `/suppliers` share a create handler returning `SupplierResponse`. The `/api` consumer ignores the object and refetches the list. The rate and status PATCH pairs likewise share handlers, and their `/api` responses are ignored before refetching.

**Target:** For both create aliases, use a nominal `SupplierMutationResponse` containing exactly `id` (or an equivalent minimal acknowledgement). For both rate aliases and both status aliases, use `SupplierMutationResponse` containing exactly `id` and `updated_at`. Keep both aliases and do not change status codes. The implementation must apply the same contract consistently to each shared handler.

### Supplier delete aliases

**Current:** `/api/suppliers/{supplier_id}` and `/suppliers/{supplier_id}` share a 204 no-content behavior.

**Target:** Make 204 explicit through HTTP response metadata, with no JSON/Pydantic body and no status change.

### POST `/api/incidents/analyze`

**Implemented:** The route uses the nominal `IncidentAnalysisResponse` schema. The analyzer logic and all ten output fields are unchanged.

**Final contract:** `IncidentAnalysisResponse` with exactly:

- `total_records: int`
- `valid_records: int`
- `invalid_records: int`
- `invalid_breakdown: dict[str, int]`
- `category_breakdown: dict[str, int]`
- `status_breakdown: dict[str, int]`
- `country_breakdown: dict[str, int]`
- `closed_scored: int`
- `score_distribution: dict[int, int]`
- `average_satisfaction: float`

Do not remove any field.

### GET `/api/incidents/results/export`

**Current:** Runtime returns CSV, but OpenAPI currently documents an empty `application/json` response.

**Target:** Document the actual HTTP contract as `text/csv`, using an appropriate `Response`/`response_class`, correct OpenAPI `responses`, and `Content-Disposition` where applicable. Do not create a Pydantic model for CSV content.

### GET `/health`

**Implemented:** The route uses the nominal `HealthResponse` schema and preserves the stable body `{ "status": "ok" }`. No internal consumer is confirmed, but infrastructure or external monitoring may depend on it.

**Final contract:** `HealthResponse` with exactly `status: str`. The current body is preserved and the endpoint remains a JSON response.

## 6. Security findings

- `hashed_password` exists internally but is not exposed by the current safe user projections.
- Plaintext passwords never form part of response payloads.
- Password-reset token/JTI persistence is not exposed in responses.
- Forgot-password keeps a generic message, preserving anti-enumeration behavior.
- The login `access_token` is intentional and required by the authentication contract.
- `user_uuid` in inventory orders is currently used by the UI and must not be removed from the optimized projection.
- Any IDs removed from a projection must be justified by consumer evidence; they must not be removed solely because they are described as “internal”.
- No vulnerability is asserted where the evidence shows a safe contract already exists.

## 7. Request-schema findings

- Authentication request schemas are separated from authentication response schemas.
- Inventory writes use specific request schemas.
- Inventory request schemas use `extra="forbid"`.
- User registration accepts only the fields required by its flow.
- Profile updates accept only `name`, `phone`, and `address`.
- Supplier rate and status PATCH operations use separate request schemas.
- No clear mass-assignment case was found in the audited write endpoints.
- For endpoints without an internal consumer, an accepted field that is not sent by this repository is not automatically considered incorrect; external clients remain possible.

## 8. Planned implementation

Completed:

- Explicit nominal contracts for auth/login, health, and incidents JSON.

Completed in this phase:

1. Adjusted inventory order and movement projections.

Remaining implementation:

1. Adjust supplier mutation projections.
2. Make 204 and CSV HTTP contracts explicit.
3. Add HTTP contract coverage and global verification.

Auth, user-registration, and profile payload optimization is completed above. Phase 2.1 implementation is also reflected above; the remaining items are still pending.

## 9. Definition of done

The implementation phase will be complete when:

- all 33/33 registrations are represented and reviewed;
- every JSON success response has an explicit nominal output schema;
- no ORM/TinyDB internal object is exposed raw;
- all 204 routes are explicitly documented as no-content;
- CSV is explicitly documented as `text/csv`;
- sensitive credentials are excluded;
- optimized projections match known consumers;
- tests pass;
- at least three manual `/docs` verifications are completed;
- final audit statuses are updated to ✅.

This document is the baseline classification only. It does not implement serializers, modify backend/frontend/tests, update the Memory Bank, commit, or push changes.
