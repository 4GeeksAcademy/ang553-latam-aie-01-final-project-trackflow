# Progress: TrackFlow

## Estado actual

- Fase 1 — auditoría y consumer tracing completada.
- Baseline formal creado en `docs/serialization-audit.md`.
- Fase 2 — implementación de response contracts en progreso.
- Fase 2.1 completada.
- Fase 2.2 completada.

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
- Auditoría actual: 20 compliant, 13 optimize, 0 missing.
- Suite backend actual: 147 tests passing.
- Frontend: backoffice build/typecheck exitoso; Talent Pipeline compilation/TypeScript exitoso, prerender bloqueado por falta de `NEXT_PUBLIC_API_URL`; lint con fallos preexistentes no relacionados.

## Pendiente

- Optimización de movimientos y órdenes de inventory.
- Optimización de mutaciones de suppliers.
- Contratos HTTP 204/CSV explícitos.
- HTTP contract/global QA.
- Verificación manual de `/docs`.
- Cierre final de auditoría.

## Siguiente paso

- Fase 2.3 — inventory movement/order response optimization.