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
| ✅ Compliant | 33 |
| ⚠️ Optimize / contract adjustment | 0 |
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
| DELETE | `/users/{user_id}` | Explicit 204 No Content | No JSON consumer; deletion is status-based | ✅ COMPLIANT | None identified | 204 No Content, no body |
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
| POST | `/api/suppliers` | `SupplierCreatedResponse` | Ignores returned object and immediately calls `fetchSuppliers()` | ✅ COMPLIANT | Implemented minimal nominal creation acknowledgement | `SupplierCreatedResponse`: `id` |
| POST | `/suppliers` | Same handler/`SupplierCreatedResponse` as `/api/suppliers` | No internal consumer confirmed for canonical path; `/api` consumer refetches | ✅ COMPLIANT | Shared handler now applies the approved minimal contract | Same `SupplierCreatedResponse`: `id` |
| PATCH | `/api/suppliers/{supplier_id}/rate` | `SupplierMutationResponse` | Response ignored; list is refetched | ✅ COMPLIANT | Implemented mutation acknowledgement with persisted update timestamp | `SupplierMutationResponse`: `id`, `updated_at` |
| PATCH | `/suppliers/{supplier_id}/rate` | Same handler/`SupplierMutationResponse` as `/api` rate alias | No internal consumer confirmed for canonical path; `/api` consumer refetches | ✅ COMPLIANT | Shared handler now applies the approved minimal contract | Same `SupplierMutationResponse`: `id`, `updated_at` |
| PATCH | `/api/suppliers/{supplier_id}/status` | `SupplierMutationResponse` | Response ignored; list is refetched | ✅ COMPLIANT | Implemented mutation acknowledgement with persisted update timestamp | `SupplierMutationResponse`: `id`, `updated_at` |
| PATCH | `/suppliers/{supplier_id}/status` | Same handler/`SupplierMutationResponse` as `/api` status alias | No internal consumer confirmed for canonical path; `/api` consumer refetches | ✅ COMPLIANT | Shared handler now applies the approved minimal contract | Same `SupplierMutationResponse`: `id`, `updated_at` |
| DELETE | `/api/suppliers/{supplier_id}` | Explicit 204 No Content | No JSON consumer; deletion is status-based | ✅ COMPLIANT | None identified | 204 No Content, no body |
| DELETE | `/suppliers/{supplier_id}` | Same explicit 204 contract as `/api` alias | No internal consumer confirmed for canonical path | ✅ COMPLIANT | None identified | 204 No Content, no body |
| POST | `/api/incidents/analyze` | `IncidentAnalysisResponse` | Consumer uses all ten fields | ✅ COMPLIANT | None identified | `IncidentAnalysisResponse`: exact fields listed in section 5 |
| GET | `/api/incidents/results/export` | Explicit `text/csv` HTTP/OpenAPI contract | Export is a file response, not a JSON object | ✅ COMPLIANT | None identified | `text/csv` with documented attachment response |
| GET | `/health` | `HealthResponse` | No internal consumer confirmed; infrastructure may consume it | ✅ COMPLIANT | None identified | `HealthResponse`: `status: str`; preserve body |

## 5. Endpoint contract details

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

**Final:** The route explicitly declares `Response`, returns 204, and has no body.

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

**Implemented:** The shared aliases use nominal mutation response schemas. Create returns `SupplierCreatedResponse` with only `id`; rate and status return `SupplierMutationResponse` with only `id` and `updated_at`. The backoffice ignores these acknowledgements and refetches the list as before.

**Final contract:** For both create aliases, use `SupplierCreatedResponse` containing exactly `id`. For both rate aliases and both status aliases, use `SupplierMutationResponse` containing exactly `id` and `updated_at`. Keep both aliases and status codes unchanged; the shared handlers apply the same contracts consistently.

### Supplier delete aliases

**Final:** `/api/suppliers/{supplier_id}` and `/suppliers/{supplier_id}` share an explicit 204 no-content contract.

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

**Final:** The route explicitly documents a `text/csv` success response and the attachment header.

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
- Auth, user-registration, and profile payload optimization.
- Inventory order and movement projections.
- Global HTTP contract verification.
- Manual Swagger QA through a real FastAPI/Uvicorn server.

No implementation work remains for this ticket.

## 9. Definition of done

Completed:

- all 33/33 registrations are represented and reviewed;
- every JSON success response has an explicit nominal output schema;
- no ORM/TinyDB internal object is exposed raw;
- all 204 routes are explicitly documented as no-content;
- CSV is explicitly documented as `text/csv`;
- sensitive credentials are excluded;
- optimized projections match known consumers;
- tests pass;
- manual `/docs` verifications are completed;
- final audit statuses are updated to ✅.

## Final verification

### Automated verification

- Exact runtime manifest: 33 registrations.
- 29 nominal JSON response contracts.
- 4 special HTTP contracts.
- 27 OpenAPI-visible registrations.
- No inferred-only JSON contracts.
- ASGI HTTP verification completed for auth/profile, suppliers, inventory,
  incidents, and health.
- Full backend suite: 170 passing.

### Manual Swagger QA

`/docs` was tested manually against a real FastAPI server running through
Uvicorn. Representative endpoints validated:

- `GET /health`
- `POST /users`
- `POST /auth/login`
- `GET /auth/me`
- `POST /suppliers`
- `GET /suppliers`
- `POST /inventory/products`
- `POST /inventory/orders/inbound`
- `GET /inventory/orders`
- incidents analyze and CSV export

The checks confirmed the minimal registration, token, supplier mutation,
inventory movement, flat inventory list, and incidents CSV contracts. Bearer
authentication returned 401 without a token and the approved safe identity
projection with a valid token. No credential or persistence-internal fields
were exposed.

### Final verdict

Serialization audit complete.

- 33 compliant
- 0 optimize
- 0 missing

No raw persistence objects exposed. No credential/internal fields leaked.
Special 204 and CSV contracts are explicit.
