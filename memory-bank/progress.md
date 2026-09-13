# Progress: TrackFlow

## Estado actual

- Auditoría de serialización backend en progreso.

## Completado relevante

- Baseline backend reproducido inicialmente: 136 tests passing.
- Inventario exhaustivo de superficie API: 33 registros method+path, 27 visibles en OpenAPI y 6 aliases ocultos de suppliers.
- Detectado mismatch real en `GET /inventory/orders` para movimientos con referencias SKU huérfanas.
- Mismatch corregido preservando `sku: SKUSummary`.
- Las referencias huérfanas generan ahora un error de integridad controlado.
- Suite después de la corrección: 138 tests passing.

## Pendiente

- Trazado completo de consumidores.
- Clasificación de endpoints.
- `docs/serialization-audit.md`.
- Implementación de serializers/contratos faltantes.
- Tests HTTP de serialización.
- Verificación manual de `/docs`.

## Siguiente paso

- Fase 1.2 — consumer tracing completo.