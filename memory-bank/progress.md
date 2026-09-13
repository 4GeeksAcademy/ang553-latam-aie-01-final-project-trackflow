# Progress: TrackFlow

## Estado actual

- Fase 1 — auditoría y consumer tracing completada.
- Baseline formal creado en `docs/serialization-audit.md`.

## Completado relevante

- Baseline backend reproducido inicialmente: 136 tests passing.
- Inventario exhaustivo de superficie API: 33 registros method+path, 27 visibles en OpenAPI y 6 aliases ocultos de suppliers.
- Detectado mismatch real en `GET /inventory/orders` para movimientos con referencias SKU huérfanas.
- Mismatch corregido preservando `sku: SKUSummary`.
- Las referencias huérfanas generan ahora un error de integridad controlado.
- Suite después de la corrección: 138 tests passing.
- Consumer tracing completo.
- Auditoría formal completada: 13 compliant, 17 optimize/contract adjustment, 3 missing explicit response contract.

## Pendiente

- Implementación de contratos explícitos.
- Optimización de payloads aprobados.
- Contratos HTTP 204/CSV.
- HTTP contract tests.
- QA de `/docs`.
- Cierre final de auditoría.

## Siguiente paso

- Fase 2 — implementación de serializers/response contracts.