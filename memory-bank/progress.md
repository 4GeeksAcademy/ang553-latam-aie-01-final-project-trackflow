# Progress: TrackFlow

## Estado actual

- Fase 1 — auditoría y consumer tracing completada.
- Baseline formal creado en `docs/serialization-audit.md`.
- Fase 2 — implementación de response contracts completada.
- Fase 2.1 completada.
- Fase 2.2 completada.
- Fase 2.3 completada.
- Fase 2.4 completada.
- Fase 2.5 completada.
- Fase 3 completada.
- Fase 3.1 completada.
- Fase 3.2 completada.

## Completado relevante

- Fase 1 audit baseline completada.
- Baseline backend reproducido inicialmente: 136 tests passing.
- Inventario exhaustivo de superficie API: 33 registros method+path, 27 visibles en OpenAPI y 6 aliases ocultos de suppliers.
- Detectado mismatch real en `GET /inventory/orders` para movimientos con referencias SKU huérfanas.
- Mismatch corregido preservando `sku: SKUSummary`.
- Las referencias huérfanas generan ahora un error de integridad controlado.
- Suite después de la corrección: 138 tests passing.
- Consumer tracing completo.
- Explicit nominal response contracts implementados:
	- `POST /auth/login` → `TokenResponse`.
	- `GET /health` → `HealthResponse`.
	- `POST /api/incidents/analyze` → `IncidentAnalysisResponse`.
- OpenAPI refleja los tres schemas nominales.
- Contratos nominales explícitos de Fase 2.1 implementados para auth/login, health e incidents.
- Auth/profile response contracts optimizados:
	- `GET /auth/me` → `AuthMeResponse`.
	- `POST /users` → `RegistrationResponse`.
	- `GET/PUT /profiles/me` → `ProfileMeResponse`.
- Frontend contracts alineados en `uis/backoffice/` y `apps/talent-pipeline-tracker/`.
- `POST /inventory/orders/inbound` usa `MovementCreatedResponse` con solo `id`.
- `POST /inventory/orders/outbound` usa `MovementCreatedResponse` con solo `id`.
- `GET /inventory/orders` usa `InventoryOrderListItem` plano con `id`,
  `movement_type`, `quantity`, `warehouse`, `created_at`, `user_uuid`,
  `sku_name`, `sku_code`, `reference`, `exit_type` y `tracking_number`.
- La relación SKU continúa siendo requerida internamente.
- `InventoryDataIntegrityError` continúa protegiendo referencias huérfanas.
- Bulk-loading de SKUs y comportamiento no-N+1 preservados.
- Frontend backoffice alineado con el nuevo contrato.
- Supplier create aliases usan `SupplierCreatedResponse` con solo `id`.
- Supplier rate/status aliases usan `SupplierMutationResponse` con `id` y `updated_at`.
- Los handlers compartidos mantienen el mismo contrato bajo `/suppliers` y `/api/suppliers`.
- `GET` list/detail conservan `SupplierResponse` completo.
- Persistencia TinyDB verificada; rate/status actualizan `updated_at`.
- Frontend backoffice alineado con los contratos mínimos.
- Todos los 33 method+path registrations tienen contrato explícito apropiado.
- DELETE user y supplier usan explicit 204 No Content.
- Supplier alias `/api` comparte el mismo contrato 204.
- CSV export documenta `text/csv` correctamente en OpenAPI.
- No se usan serializers JSON artificiales para 204 o CSV.
- Auditoría actual: 33 compliant, 0 optimize, 0 missing.
- Fase 3.1 — verificación global HTTP completada.
- Manifest runtime global verifica exactamente 33 registrations.
- Las 29 respuestas JSON usan contratos Pydantic nominales.
- Los 4 contratos especiales son 3 respuestas 204 No Content y 1 respuesta text/csv.
- Los 27 decorators fuente tienen contrato explícito: 24 JSON y 3 special.
- No existen contratos JSON inferred-only.
- OpenAPI contiene 27 registrations visibles.
- HTTP real verificado vía ASGI/FastAPI para user/auth, profile, suppliers,
  inventory, incidents, CSV export y health.
- Inventory HTTP usa SQLite in-memory aislado mediante dependency override.
- Incidents HTTP verifica analyze real seguido de CSV export.
- Suite actual: 170 tests passing.
- Audit permanece 33 compliant / 0 optimize / 0 missing.
- Manual `/docs` QA passed contra FastAPI real mediante Uvicorn.
- Fase 1.2D — autorización de lecturas de inventory corregida: los tres GET
	requieren `get_current_user` y devuelven 401 sin Bearer token.
- La regresión de contratos globales de orders usa headers autenticados.
- Suite actual tras la corrección: 172 tests passing.
- QA específico de 1.2D completado; no se implementó caching ni middleware de
	timing.
- Fase 1.3A — middleware HTTP de timing implementado con logger dedicado
	`api.timing`, logging seguro de método/path/status/duración y test focalizado.
- Suite completa: 173 tests passing.
- Baseline pequeño de candidatos ejecutado antes del seed.
- Harness reproducible aislado creado para perfiles `base`, `medium` y `large`.
- Suite completa continúa pasando con 173 tests.
- Fase 2.1 de caching — primitiva TTL process-local implementada con reloj
	inyectable, invalidación explícita y tests unitarios de expiración.
- Fases 2.2–2.5 de caching — proyecciones autenticadas de productos y órdenes
	cacheadas con TTL, invalidación tras creación de SKU y movimientos, y QA de
	cache hit/invalidation.
- Evidencia principal: `orders` crece de ~2.32 ms a ~104.36 ms (44.98x) y
	~1.44 MB de payload en `large`; `products` de ~2.73 ms a ~12.51 ms
	(4.58x); `suppliers` de ~1.72 ms a ~10.60 ms (6.16x).
- `product detail` permanece prácticamente estable (~2.5 ms) y tiene bajo valor
	actual para caching.

## Pendiente

- Selección final de endpoints.
- Estrategia de TTL/invalidation.
- Implementación de cache.
- Benchmark post-cache.
- `CACHING_REPORT.md`.
- Frontend Lazy Loading aprobado.

## Siguiente paso

- Decidir qué endpoints cachear usando coste × frecuencia × estabilidad.
