# Progress: TrackFlow

## Estado actual

- Fase 1 — auditoría y consumer tracing completada.
- Baseline formal creado en `docs/serialization-audit.md`.
- Fase 2 — implementación de response contracts en progreso.
- Fase 2.1 completada.

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
- Auditoría actual: 16 compliant, 17 optimize/contract adjustment, 0 missing explicit response contract.
- Suite actual: 142 tests passing.

## Pendiente

- Optimización de payloads auth/user/profile.
- Optimización de movimientos y órdenes de inventory.
- Optimización de mutaciones de suppliers.
- Contratos HTTP 204/CSV explícitos.
- HTTP contract QA.
- Verificación manual de `/docs`.
- Cierre final de auditoría.

## Siguiente paso

- Fase 2.2 — auth/user/profile payload optimization.