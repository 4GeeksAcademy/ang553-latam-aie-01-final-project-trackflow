# Progress: TrackFlow

## DEV-53 — Fase 1/1R/1S: persistencia y control de estados

- Implementado el DDL explícito de `job_runs` para PostgreSQL con estados
  `pending`, `processing`, `completed` y `failed`, integridad temporal y forma
  de filas según el estado.
- La exclusión usa únicamente dos índices únicos parciales: un solo
  `processing` por `job_name` y un solo claim `processing|completed` por
  `(job_name, target_date)`. Los intentos `failed` permanecen como historial y
  permiten reintentos.
- Añadido `services/job_runner/`, independiente de FastAPI, cron, CSV y el
  pipeline semanal. La adquisición crea `pending` y cambia a `processing` en
  una transacción; solo un commit exitoso concede el derecho a ejecutar.
- Los conflictos esperados hacen rollback y consultan el estado ganador desde
  una transacción nueva. Las transiciones terminales usan compare-and-set desde
  `processing`; los errores persistidos siguen una política conservadora que
  reemplaza por completo formatos sensibles reconocidos.
- Fase 1R corrigió el manejo de fallos al abrir conexión o transacción, preserva
  la causa SQLAlchemy y garantiza cierre/rollback cuando corresponde. También
  alinea `created_at` con `started_at` y evita `finished_at < started_at` si el
  reloj de aplicación retrocede.
- Fase 1S evita que un fallo secundario de rollback sustituya la causa principal
  y conserva el resultado adquirido cuando el commit fue confirmado aunque el
  cierre posterior falle. Un error de commit continúa sin asumirse exitoso.
- QA unitario: **34 PASS**.
- QA PostgreSQL real sobre el contenedor autorizado, aislado en loopback y
  `tmpfs`: **7 PASS**. Se verificaron concurrencia para fechas iguales y
  diferentes, idempotencia después de `completed`, historial/reintento después
  de `failed`, CAS inválido y rollback sin filas `pending` huérfanas.
- Regresión focalizada de control transaccional: **54 PASS, 1 SKIP**. El skip
  corresponde a una integración opcional del pipeline que requiere su propia
  variable PostgreSQL y no acredita ni bloquea DEV-53 Fase 1.
- La tabla de prueba quedó sin filas residuales. No se aplicó el DDL a bases de
  despliegue ni se usó el contenedor PostgreSQL preservado.
- DEV-53 continúa incompleto: exportación CSV, subprocess del pipeline,
  scheduler externo, logging operacional y runbook pertenecen a fases
  posteriores. Una terminación abrupta todavía puede dejar `processing` y no
  existe takeover automático.

## Parte 3 — subflows, tests unitarios y dashboard de negocio

- Pipeline semanal refactorizado en subflows Prefect de extracción, cálculo por
  almacén/cliente y publicación; el observador opcional conserva tratamiento no
  crítico mediante estado explícito.
- La transformación valida y deduplica una sola vez y usa tasks independientes
  para volumen inbound, throughput outbound, frecuencia de quiebre y eventos de
  discrepancia; las fórmulas y diagnósticos aprobados de Parte 2 se preservan.
- Tests unitarios aislados añadidos en `tests/pipelines/test_pipeline.py`: 5 PASS.
- Dashboard de negocio implementado en `/reporting`, consumiendo el GET semanal
  autenticado existente y diferenciando carga, ausencia de publicación, 401,
  403, publicación vacía y error del servicio.
- Regresión focalizada validada: transformación 50 PASS, Prefect 13 PASS, CLI
  23 PASS y reporting HTTP 13 PASS (este último requirió ejecución fuera del
  sandbox por bloqueo ambiental de `TestClient`).
- Frontend focalizado validado: ESLint PASS para `app/reporting/page.tsx`,
  `lib/reportingApi.ts`, `types/reporting.ts` y `BackofficeHeader.tsx`; `tsc
  --noEmit` PASS. `npm run build` pasó fuera del sandbox y generó la ruta
  `/reporting`.
- El `npm run lint` global continúa en FAIL por tres errores preexistentes fuera
  del diff (`account/profile`, `login` y `AuthContext`) y un warning
  preexistente; no se modificó esa deuda fuera de alcance.
- QA PostgreSQL real completado sobre infraestructura aislada desechable: 7
  pruebas de `tests/test_pipeline_postgres_integration.py` PASS y 1 prueba de
  `tests/test_trackflow_e2e_postgres.py` PASS. Quedaron acreditadas publicación
  correcta, idempotencia, quality gate, estados persistidos con IDs Prefect y
  consulta HTTP real.
- La fixture HTTP obsoleta se actualizó al contrato empresarial vigente:
  `order_id` con `commercial_order` y dispatch enlazado, `movement_id` para el
  cruce de umbral y `count_id` para discrepancias. Producción y fórmulas no se
  modificaron.
- Backend original y dashboard quedaron preparados para aceptación en
  `http://127.0.0.1:8000` y `http://127.0.0.1:3000/reporting`, reutilizando la
  autenticación real existente. La semana positiva es `2026-10-19` y la semana
  publicada sin filas es `2026-10-12`.
- La aceptación funcional manual continúa `PENDIENTE_VALIDACION_MANUAL`; la
  preparación técnica anterior no la declara completada.
- Continúan pendientes RQ-15 (aceptación externa de ruta/CLI), RQ-17
  (certificación empresarial de cobertura) y la aceptación funcional manual.

## Estado histórico de Parte 2 (conservado)

> Esta sección registra un estado intermedio anterior. El estado vigente se
> documenta en las secciones más recientes situadas al inicio de este archivo.

- Parte 2 — Pipeline de Desempeño de Negocio: Fase 1 (núcleo de extracción y
	transformación) completada; el pipeline completo no está terminado.

### Parte 2 — Fase 1 completada

- Transformaciones semanales de los cuatro KPIs oficiales, con normalización y
	validación de eventos.
- Deduplicación y clasificación outbound mediante evidencia auxiliar;
	extracción SQL de solo lectura y preparación de evidencia de movimientos
	entre semanas.
- DDL explícito para `reporting.weekly_warehouse_client_performance`.
- Diagnósticos de calidad y overflow compatible con PostgreSQL `integer`.
- Suite focalizada: **73 pruebas PASS**.

### Parte 2 — Fase R1 completada

- Migración funcional completada: el paquete activo del pipeline es `pipelines/`.
- `pipelines/extraction.py` y `pipelines/weekly_performance.py` reutilizan la extracción y transformación de Fase 1; no se implementó una segunda lógica ETL.
- Los originales bajo `data/` se conservan intactos como referencia histórica.
- Los imports de pruebas focalizadas apuntan al paquete `pipelines`; setuptools incluye `pipelines*` en el descubrimiento de paquetes.
- Verificación final: **73 pruebas focalizadas PASS**.
- Los contratos empresariales y el DDL de reporting no se modificaron.

### En progreso / pendiente

- Fase 2.1 — `pipelines/publication.py`: publicación transaccional y UPSERT;
	control persistente de corridas, idempotencia y reconciliación.
- Integración y pruebas con PostgreSQL real todavía pendientes.
- Prefect, reintentos, caché, CLI y endpoints de reporting todavía pendientes.

### Limitaciones de Parte 2

- PostgreSQL real aún no se ha probado.
- Cobertura incompleta de `stock_threshold_triggered` e `inventory_discrepancy_detected`.
- Equivalencia entre movimientos `dispatch` y pedidos comerciales no certificada.
- Cuatro fallos de regresión y un problema de colección preexistentes,
  contrastados con el commit base.

**Siguiente paso:** implementar `pipelines/publication.py` en Fase 2.1, sujeto a
revisión y autorización arquitectónica.

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
- Fase 3.1 — pipeline Pandas de análisis de telemetría completado:
	dataset real preparado y verificado (25 eventos totales,
	20 dentro de últimos 7 días);
	`inventory_stock_insufficient` persiste correctamente;
	pipeline implementado en `services/telemetry/analysis.py`;
	métricas: `event_volume_by_day_and_type`,
	`operational_failures_by_type_and_day`,
	`stock_insufficiency_by_day_warehouse_category`;
	`build_telemetry_report` implementado;
	SQL aplica filtros temporales/event_type;
	Pandas realiza conversión temporal, refinamiento y agregación;
	`period.from`/`to` normalizados a UTC;
	tests de análisis: 30 PASS;
	suite telemetry relevante: 63 PASS;
	Pandas/uv.lock sincronizados.
	PENDIENTE: GET /telemetry/report, default 7 días,
	start_date/end_date, cache TTL 60s, auth_failure_rate,
	dashboard técnico.
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

## Bloqueado

- `inbound_order_created`: falta de `client_id` estable.
- Otros eventos obligatorios de inventory que requieren `client_id`.
- Ambigüedad semántica de `outbound_order_created`.
- Falta de policy/modelo para `stock_threshold_triggered`.
- Flujo de edición directa de stock ausente.
- Workflow de discrepancias ausente.
- Falta de policy de threshold para `api_request_slow`.
- Falta event type para errores uncaught del frontend.
- Fallo de emisión del token de password reset sin `request_outcome_class`
	válido.
- Timeout por inactividad de `inventory_workflow_abandoned` sin resolver.

## Estado de QA

- `BACKEND_COMPILEALL = PASS`.
- `BACKEND_TEST_EXECUTION = BLOCKED` por `ModuleNotFoundError: tinydb`.
- `FRONTEND_RUNTIME_UNAVAILABLE`: `uis/backoffice/node_modules` ausente.
- `FRONTEND_STATIC_DIAGNOSTICS = NO_ERRORS_REPORTED`.

## FASE 7 — CIERRE FINAL: TELEMETRÍA COMPLETADA

- GET /telemetry/report implementado con cache TTL 60 segundos.
- Default automático: últimos 7 días si no se especifican start_date/end_date.
- Parámetros start_date/end_date opcionales; ambos deben estar presentes y ser timezone-aware.
- Periodo resuelto una sola vez; intervalo [start_date, end_date).
- Cuatro métricas operacionales implementadas:
  - event_volume_by_day_and_type: volumen diario por tipo de evento.
  - operational_failures_by_type_and_day: fallos técnicos por tipo y día.
  - stock_insufficiency_by_day_warehouse_category: insuficiencias de stock por almacén y categoría.
  - auth_failure_rate_by_day: tasa de fallos de autenticación diaria (EXTRA).
- Pipeline Pandas completo: filtrado SQL → conversión UTC → agregación groupby → JSON.
- Dataset real verificado: 31 eventos totales, 26 en últimos 7 días.
- Dashboard técnico (EXTRA) implementado en uis/backoffice/app/backoffice/telemetry/page.tsx.
- Backend tests: 40 PASS (analysis) + 19 PASS (report) = 59 PASS.
- Frontend: ESLint, TypeScript, build — todos OK.
- Seguridad: sin secretos hardcodeados, diff limpio.
- Rama feat/telemetry-report lista para PR.

## Telemetry storage — estado final del ticket

- `telemetry_events` queda persistida en Supabase con 8 columnas contractuales e
	índices sobre `timestamp`, `event_type` y GIN sobre `tags`.
- La persistencia es append-only. `POST /telemetry/events` aplica validación
	parcial y reporta `received`, `stored` y `rejected`; cada batch usa un único
	bulk insert.
- Los eventos enviados por frontend usan `service="backoffice"`; los eventos
	generados en backend usan `service="api"`.
- `inbound_order_created` y `outbound_order_created` persisten después del
	commit de negocio.
- `inventory_stock_insufficient` persiste mediante
	`persist_backend_telemetry_best_effort` con sesión independiente;
	un fallo de telemetría no altera el HTTP 400 insufficient_stock y la
	Session principal no recibe commit/rollback adicional.
- `auth_login_failed` persiste best-effort, sin hacer depender la autenticación
	de SQL. La implementación usa `open_db_session()` y
	`persist_backend_telemetry_best_effort()`.
- En Codespaces se requiere Supabase Session Pooler por conectividad IPv4/IPv6.
	`DATABASE_URL` y `JWT_SECRET_KEY` quedaron configurados como repository
	secrets de Codespaces.
- E2E aprobado: inbound `+5`, outbound `-2`, `auth_login_failed` y batch mixto
	con `2 received / 1 stored / 1 rejected`; se verificaron 5 filas reales en
	Supabase.
- El frontend no fue modificado. Los fallos globales preexistentes de tests
	quedaron fuera de alcance.
- Para levantar el backend desde Codespaces se debe ejecutar desde la raíz del
	repositorio. El puerto 8000 se expone como Public solo temporalmente para E2E
	y debe volver a Private al cerrar.
