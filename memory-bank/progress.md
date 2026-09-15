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
- Proyecto de diseño de telemetría TrackFlow en progreso; Fase 1 completada y validada.

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
- Lazy Loading real de IncidentSummary mediante `next/dynamic` completado.
- Placeholder ligero compartido mantiene la UI inicial.
- IncidentSummary completo solo se carga cuando existe `result`.
- ESLint específico pasa.
- Build de backoffice pasa.
- QA manual completado.
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
- Primitive TTL in-process completada con reloj monotónico inyectable,
	thread-safety, expiración lazy e invalidación explícita.
- Products cache completada con TTL de 30 s.
- Orders cache completada con TTL de 15 s y proyecciones serializables.
- Invalidación selectiva completada: creación de producto invalida products;
	inbound y outbound invalidan products + orders.
- Mutaciones fallidas conservan las cachés calientes.
- La autenticación continúa ejecutándose antes del cache hit.
- La suite completa actual: 193 tests passing.
- Benchmark post-cache base y large completado.
- Freshness validada después de mutaciones.
- Evidencia large: products pre ~12.51 ms, warm post-cache ~1.84 ms,
	~85% de reducción; orders pre ~104.36 ms, warm post-cache ~9.6 ms,
	~91% de reducción.
- Auditoría inicial del repositorio y definición del núcleo obligatorio de
	telemetría TrackFlow completadas.
- Cinco eventos obligatorios validados y catálogo ampliado con 12 oportunidades
	adicionales: 17 eventos en total.
- Cobertura de inventario/negocio, autenticación, validaciones/errores,
	rendimiento técnico y navegación/workflows.
- QA pre-commit de Fase 1 completado.

## Pendiente

- Decisión final sobre useMemo.
- `CACHING_REPORT.md`.
- Auditoría final del ticket.
- PR.
- Diseño completo del Event Envelope.
- Definición de properties y taxonomías bounded.
- Creación de `event-schemas.json`.
- Estrategia stream vs batch.
- Throttle/debounce donde corresponda.
- Análisis de riesgos/exclusiones.
- QA final contra criterios del proyecto.
- Resolver `client_id` como identificador estable y validar la equivalencia
	entre persistencia de `StockExit` y dispatch completado durante la
	instrumentación.

## Siguiente paso

- Fase 2: diseño del Event Envelope y contrato de eventos.
