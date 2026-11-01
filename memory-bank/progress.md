# Progress: TrackFlow

## DEV-55 — Bloque A: Celery, Redis, Flower y tarea empresarial

- Fases 2–4 implementadas en `feature/dev-55-async-tasks` sin modificar el
  pipeline Prefect existente ni el endpoint HTTP de reporting.
- Añadido `services/task_queue/` con una aplicación Celery central, Redis como
  broker y backend, serialización exclusivamente JSON, estados `STARTED`,
  eventos, ACK tardío, rechazo ante pérdida del worker y prefetch 1.
- `trackflow.run_weekly_pipeline` recibe únicamente `task_id` implícito,
  `run_id` y `week_start`, reconstruye el runtime CLI en el worker y reutiliza
  `pipelines.pipeline.run_weekly_pipeline`. El resultado conserva estado y
  contadores empresariales; los fallos expuestos por Celery usan un diagnóstico
  acotado sin copiar mensajes potencialmente sensibles.
- Docker Compose incorpora el perfil opt-in `async-tasks`: Redis 7.4.1 con
  AOF, `noeviction`, volumen y healthcheck; worker separado de FastAPI; y
  Flower en loopback:5555 con soporte de autenticación. El perfil `scheduler`
  de DEV-53 permanece independiente.
- QA focalizado final de tarea, seguridad Docker, Prefect y DEV-53:
  **106 PASS** (incluye **10 PASS** de `test_task_queue.py`). Suite restante:
  **667 PASS, 24 SKIP, 1 FAIL** heredado del baseline `35e695f` en el contrato
  CORS de `test_inventory_auth.py`: el código, el test y las versiones
  FastAPI/Starlette relevantes no cambian en el diff DEV-55. El commit base no
  se ejecutó en un checkout separado. La colección global además continúa
  bloqueada por la carga dinámica de `test_inventory_client_ids.py`; ocurre
  tanto con Python 3.12 como 3.13.
- QA Docker real: Redis, worker y Flower alcanzaron `healthy`; se verificaron
  `noeviction`, AOF activo, worker conectado, tarea registrada, eventos
  habilitados y recuperación de un resultado `FAILURE` seguro desde Redis. La
  API Flower respondió 401 sin credenciales, confirmando su protección; no se
  accedió al secreto. Los servicios QA quedaron detenidos, sin borrar el
  volumen ni los contenedores.
- Pendiente para Bloque B: adaptación del POST, GET de tareas, registro
  PostgreSQL, outbox/relay, claims/fencing, retries definitivos, DLQ y
  reconciliación. Compose deberá proporcionar al servicio `api` el mismo
  `REDIS_URL` interno del worker sin activar Redis en el perfil ordinario. La
  tarea empresarial no se ejecutó contra datos persistentes.

> Las subsecciones DEV-53 siguientes preservan la cronología del desarrollo.
> Frases como "DEV-53 continúa incompleto" describen aquel corte histórico y
> no sustituyen el estado final documentado en las secciones DEV-53 posteriores.

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

## DEV-53 — Fases 2-4: export nocturno, scheduler y QA automatizado

- Implementado `scripts/nightly_export.py`: fecha objetivo estricta o ayer UTC,
  adquisición mediante `services.job_runner`, export de `telemetry_events` en
  intervalo UTC semiabierto y ejecución exacta de
  `python -m pipelines.pipeline` sin `--week-start`.
- Los CSV se publican sin overwrite mediante temporal en el mismo filesystem,
  `flush`, `fsync`, cierre y hard link. Un archivo existente se valida y
  reutiliza; uno corrupto bloquea la ejecución sin reemplazo.
- Los códigos no cero del pipeline producen `failed`; el código 4 se describe
  como resultado incierto. Las interrupciones controlables terminan y esperan
  al hijo antes de liberar propiedad. El stdout/stderr del pipeline se hereda
  para conservar diagnóstico seguro en logs del contenedor.
- Añadido scheduler independiente de FastAPI con Supercronic, cron único
  `15 0 * * *` UTC, usuario no root y volumen persistente en `/app/data/raw`.
  No ejecuta el job al arrancar ni desde un healthcheck.
- La imagen instala exactamente `uv.lock` con `uv sync --frozen --no-dev`.
  Prefect se ejecuta sin UI y con timeout de arranque efímero de 120 segundos;
  esto corrige el timeout observado con el default de 20 segundos dentro de la
  imagen non-root.
- La corrección pre-commit endurece la ventana de creación y la terminación del
  subprocess: bloquea temporalmente `SIGINT`/`SIGTERM`, inicia una sesión POSIX,
  espera el grupo completo de Prefect incluso si el padre ya terminó y tolera
  interrupciones repetidas durante cleanup. La ventana pos-`wait()` también
  queda dentro del mismo guard: una interrupción antes o durante la consulta del
  grupo activa cleanup, y si no puede confirmarlo conserva `processing` en vez
  de ejecutar el CAS a `failed`.
- El servicio Compose `scheduler` es opt-in mediante el perfil `scheduler`.
  `docker compose config --services` lista solo `api` e `interfaces`; con
  `--profile scheduler` añade `scheduler`. El runbook manual documenta revisión
  por UUID exacto, procesos, logs, CSV y commit incierto antes de un CAS
  `processing`→`failed` y un único reintento.
- QA unitario/estático final focalizado: **103 PASS** en nightly export,
  infraestructura scheduler, job runner y CLI del pipeline. Build de
  `trackflow-nightly-scheduler:dev53-qa`: **PASS**.
- E2E real one-shot sobre dos bases PostgreSQL desechables distintas, el
  contenedor autorizado y un volumen QA exclusivo: **1 PASS**. Acreditó éxito,
  CSV, pipeline, estado `completed`, repetición idempotente, fallo controlado,
  historial `failed`, reintento con reutilización del CSV y concurrencia del
  job runner. El volumen y los contenedores QA fueron limpiados. Posteriormente,
  el orquestador verificó en ambas bases propietario `trackflow_test`, cero
  conexiones y cero tablas; las eliminó y confirmó cero coincidencias restantes.
- Gate POSIX posterior al endurecimiento: imagen QA reconstruida **PASS**; probe
  Linux con un padre y descendiente reales **PASS** bajo un init reaper
  (`parent_exit=-15`, grupo inexistente tras cleanup). Sin reaper, un huérfano
  zombie queda correctamente como cleanup no confirmado, sin prometer una
  transición terminal. El E2E one-shot completo se repitió con la imagen nueva:
  **1 PASS en 135.42 s**. El test eliminó sus contenedores, objetos y volumen;
  después se verificaron cero conexiones/cero tablas, se eliminaron las dos
  bases QA exactas y se confirmó cero coincidencias.
- Probe Linux específico de la carrera pos-`wait()`: un padre real retornó 0
  dejando un descendiente activo; `run_pipeline` limpió el grupo y confirmó
  `group_exists_after_run_pipeline=False`. No se repitió el E2E completo porque
  la corrección sólo amplía el manejo de interrupciones antes de la comprobación
  existente y el camino normal ya había sido acreditado con la misma lógica de
  cleanup.
- La aceptación humana/operativa del scheduler y del artefacto final continúa
  pendiente. `SIGKILL`, caída del host o pérdida de energía aún pueden dejar
  `processing`; no existe lease, expiración ni takeover automático.

## DEV-53 — primera ejecución real y corrección post-ejecución

- La primera ejecución supervisada para `TARGET_DATE=2026-10-25`, job
  `3ede5c14-d4a0-48d7-a9ec-b3533b5e63e5`, publicó y preserva un CSV válido de
  6 filas. El subprocess semanal alcanzó Prefect y terminó `blocked_quality`;
  por contrato, el `job_run` nocturno terminó `failed`. Durante esta ejecución
  se observaron errores de serialización Prefect asociados a `Engine`/`RLock`.
- La evidencia persistida confirma `records_processed=0`,
  `aggregates_calculated=0`, `commit_result=not_attempted`,
  `block_reason=empty_batch_not_certified` y el diagnóstico adicional
  `coverage_insufficient`. No se publicaron KPI.
- El bloqueo es legítimo: los movimientos históricos preceden los contratos de
  orden, checkpoint y outbox, y reporting no recibió telemetría canónica para
  esa semana. No se fabricó evidencia ni se relajó el quality gate.
- Corregido el ruido de caché de Prefect: las tasks que reciben engines/runtime
  o realizan efectos usan `NO_CACHE`; la transformación pura conserva su clave
  determinista y expiración de una hora.
- La recuperación tras un intento `failed` no requirió cambio productivo: un
  nuevo intento adquiere otra fila, valida y reutiliza el CSV inmutable y vuelve
  a ejecutar el pipeline. `completed` se omite; `processing` incierto continúa
  requiriendo reconciliación manual.
- QA post-ejecución: 178 unitarias de pipeline PASS (3 skips de escenarios
  opcionales), 72 pruebas de nightly/job runner PASS (9 integraciones opt-in
  omitidas sin sus URLs), 7 integraciones de publicación PostgreSQL PASS y 4
  integraciones del pipeline dual/outbox PASS en un contenedor tmpfs nuevo y
  desechable, eliminado al terminar.
- La segunda ejecución real autorizada, job
  `0c571877-0cca-42ad-ae3d-6b44e7a2a130`, reutilizó el CSV existente con su
  SHA-256 sin cambios y no reprodujo los errores `Engine`/`RLock`. El pipeline
  volvió a `blocked_quality` por cobertura histórica insuficiente y no produjo
  publicación KPI productiva.
- Validación final Linux posterior a `NO_CACHE`: imagen scheduler construida con
  el código actual y E2E one-shot **1 PASS en 204.78 s** sobre dos bases nuevas
  en PostgreSQL `tmpfs` y un volumen CSV exclusivo. Acreditó publicación
  positiva, `skipped_completed`, fallo controlado, recuperación con CSV
  idéntico, exclusión concurrente y ausencia de los diagnósticos de
  serialización `Engine`/`RLock`. Supercronic validó el cron `15 0 * * *`, la
  imagen ejecutó como `10001:10001` y todos los recursos E2E fueron eliminados.

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

## DEV-55 — Bloque B (fases 5–7)

- Implementación en revisión: el POST de reporting acepta de forma transaccional
  `pipeline_publication_runs` + `async_tasks` + `task_dispatch_outbox` y ya no
  ejecuta Prefect desde FastAPI.
- `GET /tasks/{task_id}` conserva separados estado público, Celery, estado
  durable, resultado empresarial y publicación KPI.
- Relay PostgreSQL→Celery independiente con `task_id` estable,
  `FOR UPDATE SKIP LOCKED`, token/lease, backoff acotado y reconciliación.
- Claims con attempt, token, epoch y CAS. Sólo un claim expirado en `preflight`
  se recupera automáticamente; después de iniciar Prefect no se reejecutan
  efectos inciertos.
- Tres ejecuciones fallidas totales constituyen el contrato académico;
  `max_retries=3` permanece únicamente como cota superior de Celery.
- DLQ PostgreSQL idempotente; `blocked_quality` es éxito técnico y
  `commit_uncertain` exige reconciliación con `published = null`.
- DDL explícito en `infra/sql/reporting_async_tasks.sql`; nunca se aplica desde
  el lifespan de FastAPI.
- QA PostgreSQL real aislado: DDL, atomicidad/rollback, claims/fencing,
  exclusión de relays y DLQ idempotente validados. La aceptación manual de
  Fase 8 sigue pendiente.
- Fase 7R: 21 POST reales con worker detenido devolvieron 202 y persistieron
  run+tarea+outbox; las 20 mediciones precalentadas quedaron entre 46.549 y
  99.325 ms (p95/p99 99.325 ms) y la primera aceptación midió 85.610 ms.
- E2E dual-engine desechable: el caso positivo terminó `SUCCESS/succeeded`,
  publicó un KPI y mantuvo un solo run; el caso de calidad terminó
  `SUCCESS/blocked_quality`, `published=false`, un intento y sin DLQ.
- Corrección Fase 7R: el cruce preflight→Prefect ahora exige claim no expirado;
  el reconciliador sólo repara desde historial empresarial terminal cuando el
  claim también expiró, evitando competir con un worker vivo.
- Crashes QA: publicación incierta recuperada con el mismo `task_id`, dos
  entregas y una sola ejecución; crash preflight recuperado en epoch 2; crash
  tras `prefect_started` quedó `reconciliation_required` sin reejecución.
- La ejecución positiva fría midió 71.106 s. El worker Compose exige ahora
  `TRACKFLOW_WORKER_SHUTDOWN_GRACE_SECONDS`; QA usó soft/hard/visibility/grace
  120/180/240/180 s. Producción sigue pendiente de calibración propia.
- Flower protegido devolvió 401 sin autenticación y mostró SUCCESS, FAILURE y
  dos retries para tareas correlacionadas; la fila SQL DLQ confirmó el tercer
  fallo total como `retries_exhausted`.
- Regresión final: 63 tests DEV-55/PostgreSQL, 167 de Prefect/DEV-53 y 704
  globales (29 opt-in skipped, un CORS histórico deselected) pasaron. La suite
  completa conserva un error de colección ajeno en `test_inventory_client_ids`
  bajo el intérprete host y el CORS ya presente en `8bf7fda`.
- Fase 8 y aceptación humana siguen pendientes; no se creó commit.
