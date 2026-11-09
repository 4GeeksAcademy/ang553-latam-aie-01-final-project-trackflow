# Progress: TrackFlow

## Milestone 7 — final functional correction and live-test preparation

- Historical generation inputs and outputs were re-audited without model
  calls. The generation runner supplied the full approved chunk directly, so
  `gen-storage-long-term` was not a retrieval or context-preparation failure;
  DeepSeek omitted the no-movement eligibility condition despite receiving it.
- The general generation instruction now requires all material commercial
  qualifiers (eligibility/trigger, movement/inactivity, scope, exceptions,
  approvals, periods and figures) and internally consistent yes/no openings.
- A new deterministic offline review remains separate from frozen artifacts.
  It recognizes equivalent wording, passes the three genuine literal false
  negatives and rejects the long-term-storage omission: 3/4.
- Focused RAG/API/global-contract QA: 39 PASS. Historical Recall@3 remains
  9/10 (90%) at the frozen threshold 0.57; no real embedding or generation
  request was made.
- Official Docker startup exposed and corrected two local-runtime defects:
  Windows CRLF in `uis/start.sh` is normalized inside the image, and Compose
  now defaults the interface rewrite target to `http://api:8000` instead of an
  empty value. The private generic PostgreSQL URL was switched only to the
  already-installed `psycopg2` driver; destination and credentials were
  preserved.
- Qdrant, FastAPI and interfaces are running for human acceptance. Qdrant is
  healthy with 14 canonical points (Cosine, 1024); `/health`, `/login` and the
  Knowledge Base page are reachable; the unauthenticated frontend auth proxy
  returns 401. Browser automation cannot reach host localhost, so visual and
  interactive answer acceptance remains pending for the user.

## Evaluación temporal del modelo de regresión — Correctivo R1 completado

- Evaluación independiente implementada bajo `pipelines/eval/`; reutiliza el
  Random Forest y las funciones causales existentes sin modificar
  `pipelines/sales_forecast.py` ni el pipeline semanal.
- Cinco folds expansivos exactos sobre 2016–2023, cada uno con 12 meses de
  validación recursiva; 2024–2025 permanece excluido de selección y diagnóstico.
- MAE de validación: `EUR 83,868.01 ± EUR 11,164.85`; RMSE de validación:
  `EUR 97,859.15 ± EUR 9,522.25` (desviación estándar muestral entre folds).
- Baseline estacional: MAE `EUR 70,918.54 ± EUR 29,456.80` y RMSE
  `EUR 81,102.93 ± EUR 28,129.30`. El modelo mejora el MAE en 3/5 folds,
  pero su MAE medio es 18.26% mayor que el baseline.
- Correctivo R1: curva principal reconstruida con validación fija en 2023 y
  cinco ventanas recientes y continuas de 36/48/60/72/84 meses, todas cerradas
  en diciembre de 2022. La ventana mayor obtuvo el mejor MAE fijo
  (`EUR 98,484.94`), 8.98% menor que la ventana de 36 meses; la relación no es
  monotónica (`r=-0.272`) y las cinco ventanas superan el MAE del baseline 2023.
- Diagnóstico principal conservado como overfitting, ahora sustentado por la
  brecha persistente bajo validación fija y contrastado con la estabilidad
  temporal. La evidencia es mixta y no separa sobreajuste, recursividad,
  estacionalidad ni extrapolación. Mayor regularización queda solo como
  experimento correctivo no garantizado; no se modificó el modelo.
- La cifra contextual aproximada de EUR 9 millones anuales está respaldada por
  `CONTEXT.md` y `projectbrief.md`, pero sin periodo. No se reconcilia con el
  CSV, que suma EUR 9,384,261.30 en 2016 y EUR 14,418,668.57 en 2023.
- Artefactos reproducibles: `fold_metrics.json`, `learning_curve.png` y
  `evaluation_report.md`; entry point `scripts/evaluate_sales_forecast.py`.
- QA R1: compilación pertinente PASS; 11 pruebas focalizadas PASS; regresión
  combinada de forecasting 40 PASS; ejecución real del script PASS.
- Pendiente: aceptación humana y académica; la ruta autorizada
  `pipelines/eval/` no acredita aceptación de la ruta académica alternativa.

## Predicción mensual de ventas — implementación técnica

- Implementación independiente añadida en `pipelines/sales_forecast.py`; el
  pipeline semanal `pipelines/pipeline.py` permanece intacto.
- Dataset canónico validado en `pipelines/raw/trackflow_sales.csv`: 120 meses,
  mercado consolidado y SHA-256
  `2B8C782EFE182AA001DB8044689E3D65E98B2CC17649B598CEDB665E7D2F4C33`.
- Split cronológico fijo: 96 meses originales de entrenamiento (2016-2023),
  24 de prueba (2024-2025) y 84 ejemplos efectivos tras 12 meses de historial.
- `RandomForestRegressor` determinista y pronóstico recursivo de 24 meses sin
  targets futuros; baseline estacional causal y banda descriptiva calibrada
  únicamente mediante backtests temporales internos a 2016-2023.
- Métricas y artefactos reales generados en `artifacts/sales_forecast/`.
  Random Forest: MSE `18473941158.679684` EUR², RMSE `135918.87712411283` EUR,
  RMSE% `10.462758562757822`; el baseline estacional obtuvo menor RMSE
  (`123915.51155718694` EUR), limitación conservada explícitamente.
- PSI, Gini y K² se implementaron como interpretaciones operativas documentadas
  pendientes de validación académica.
- Correctivo R1 completado: validación de finitos, unidades enteras y coherencia
  de ingreso medio; ejecución segura con CSV externo sin exponer rutas; prueba
  E2E anti-fuga con dos targets de prueba distintos; diagnósticos anuales, bias,
  mayores errores y cobertura; evidencia reproducible de bins PSI y backtests.
- La banda descriptiva cubrió 15/24 meses (62.5%). El bias RF
  `actual - prediction` fue `-39605.41` EUR; RMSE anual: `114073.19` EUR en
  2024 y `154710.02` EUR en 2025. Mayor error absoluto: `313912.40` EUR en
  noviembre de 2025.
- Suite focalizada: 29 pruebas PASS. Incluye split, contratos de datos,
  causalidad de features, anti-fuga unitaria y E2E, reproducibilidad, métricas,
  diagnósticos y consistencia cruzada de artefactos.
- No se realizaron commit, staging, push, merge ni PR.

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

## Milestone 7 — RAG Knowledge Base, Block A

- Provider-neutral RAG core implemented under `services/rag/` with separate
  setup, embedding, retrieval, generation and query responsibilities.
- Four canonical TrackFlow documents produce 14 deterministic semantic chunks
  with contractual payloads and stable point IDs.
- Simulated setup is idempotent, reconciles stale points and refuses to replace
  an incompatible collection.
- The 4Geeks and Qdrant HTTP contracts are implemented with `httpx`. Block A
  used mocks only; Block B mentoring verified the real embeddings endpoint for
  all 14 chunks at 1024 dimensions.
- `POST /knowledge/query` is protected for `admin`/`manager`; `user` receives
  403 and unauthenticated requests receive 401.
- Backoffice Knowledge Base page and opt-in Qdrant Compose profile implemented.
- Evaluation artifact separates 3 calibration, 10 final positive and 3
  negative cases. Recall@3 remains unmeasured.
- Live queries remain gated until `RAG_MIN_SCORE` is calibrated in Block B.
- B2-R1 aligned the verified model identifiers without the obsolete
  `litellm/` prefix across private configuration, examples and Compose.
- Host-side indexing uses `http://127.0.0.1:6333`; the API container uses the
  fixed Docker DNS URL `http://qdrant:6333`. Collection remains
  `trackflow_knowledge`, vector size 1024 and Qdrant is still opt-in.
- The apparent ASGI test hang was caused by the Windows sandbox blocking the
  internal socket used to create an asyncio event loop, before FastAPI ran.
  Tests passed outside that restriction: 31 focused RAG/API/serialization
  tests.
- B2-R2 added an explicitly sequential real-indexing adapter with a minimum
  two-second pause, zero HTTP retries, redirects disabled, sanitized status
  reporting and accumulated numeric usage.
- Real indexing completed with 14 embedding requests and 891 prompt/total
  tokens: `trackflow_knowledge` is green with Cosine distance, 1024 dimensions
  and exactly 14 canonical points distributed 3/4/3/4. All payloads, source
  texts and deterministic IDs matched; no duplicates, missing IDs or foreign
  points were found. The 14 IDs persisted after a Qdrant container restart.
- Final B2-R2 QA passed 32 focused RAG/API/serialization tests. Qdrant remains
  running under the opt-in `rag` profile; no retrieval evaluation or generation
  call was performed.
- Finalization froze the evaluation input at SHA-256
  `31b3057a1af3ef6bcaaa4f142dc51997e37011c03572d7b074e7e3f6f9fa7dd0`.
  Calibration selected `RAG_MIN_SCORE=0.57` exclusively from three positive
  calibration queries (3/3 at rank 1).
- Real retrieval evaluation passed 9/10 = 90% Recall@3. The only miss was the
  international-carrier-from-Los-Angeles question, whose expected chunk did not
  clear the frozen threshold. Black Friday retrieved its expected section at
  rank 1 with score 0.7490.
- Real negatives produced retrieval-only abstention for 2/3 cases. Sunday
  delivery retrieved a related high-demand chunk, so the generation contract
  now uses an exact `INSUFFICIENT_CONTEXT` sentinel mapped to a fixed public
  abstention with no sources; this is tested offline but not yet verified with
  DeepSeek.
- Evaluation used 16 real embedding requests and 183 total tokens: calibration
  3/38, final 10/122, negatives 3/23. No corpus embeddings were regenerated.
- Gateway throttling now applies after every embedding or generation request;
  generation status/count/usage are sanitized. Backend focused QA passes 33
  tests; targeted frontend lint and TypeScript pass; production build passes
  outside the sandbox process restriction.
- After explicit approval, DeepSeek completed the frozen eleven-case rubric.
  Manual source comparison passed 11/11, including exact SLA, returns, carrier,
  storage, approval and insufficient-context behavior. Usage was 3,483 total
  tokens with provider-reported cost `0.00064068956`.
- The twelfth and final authorized generation ran through an authenticated real
  API E2E with isolated in-memory users, Qdrant and the gateway: manager 200,
  user 403, missing/invalid JWT 401, one embedding and one generation. The Black
  Friday answer used the expected source and preserved the no-guarantee rule.
- Pending human acceptance: visual/browser review without another model call,
  and confirmation of the commercial team's production role assignments.
- Local backoffice login diagnosis confirmed that the form and Next.js proxy
  were submitting correctly: `/auth/login` returned 200, but `/auth/me`
  immediately returned 401 because the private `JWT_SECRET_KEY` value retained
  an extra closing quote. The private delimiter was corrected without changing
  the secret itself, the API container was recreated, and token validation now
  returns 200 both directly and through the interfaces proxy. Invalid
  fictitious credentials remain rejected with 401; the existing active admin
  account was not modified and human credential entry remains pending.
- Post-human RAG diagnosis proved that Next.js's 30-second default proxy timeout
  caused the visible Sunday-delivery 500: the proxy emitted `ECONNRESET` at
  30 seconds while FastAPI completed with 200 after 36.628 seconds. The
  backoffice now uses a bounded 90-second proxy timeout, validated by a
  35.261-second offline proxy test. The exact inactive-inventory query has no
  persisted historical candidates or scores, so its cause remains pending a
  human retest. Retrieval behavior and the frozen 0.57 threshold are unchanged;
  sanitized candidate/outcome logs now make retrieval versus generation
  abstention distinguishable without storing questions, chunks or prompts.
- Definitive observability diagnosis found that the API container had loaded
  the new RAG code, but `rag.query` inherited the root `WARNING` level, so every
  diagnostic `INFO` event was discarded. The logger now owns an INFO stream
  handler, and the middleware request UUID is propagated into RAG queries.
  Offline Docker output confirms correlated retrieval and outcome events;
  provider failures are classified by retrieval/generation stage without
  logging exception bodies. The exact inactive-inventory execution remains
  unrecoverable because its filtered events and query vector were never
  persisted. No retrieval policy, corpus, threshold or frozen result changed.
- A subsequent human retry returned 503 after 30.515 s; the correlated RAG
  event was `technical_error` during retrieval at 30.465 s, not an abstention.
  The historical event predates substage logging, so it cannot prove whether
  embedding or Qdrant timed out. A same-container Qdrant read/query using an
  already stored vector returned 200 in 117 ms, making the external embedding
  request the leading explanation without treating it as retroactive proof.
  Retrieval now emits sanitized embedding/Qdrant start, success and error
  events with duration and safe categories (`timeout`, `http_error`,
  `network_error`, `validation_error`). The 30-second provider timeout and zero
  retries remain unchanged; technical failures still return 503.
- The next instrumented human run proved the contextual-retrieval weakness:
  storage standard rates cleared the primary threshold at `0.6174929`, while
  the answer-bearing long-term-inventory section ranked second at `0.55981266`
  and was excluded, producing a legitimate generation abstention from
  incomplete context. Retrieval now retains `RAG_MIN_SCORE=0.57` for primary
  evidence and may add only semantic top-k candidates from an already
  established source document as explicitly labeled complementary context.
  Complementary evidence cannot establish relevance, cross-document candidates
  remain excluded, and no full-document expansion occurs. Offline RAG/API
  regression passes; no gateway call, corpus change or Qdrant mutation was
  performed. The historical 9/10 Recall@3 remains evidence for the prior
  threshold-only version; the current method needs an independent, versioned
  reevaluation before receiving an academic metric.
- A real long-term-inventory retest then completed embedding (8098.8 ms) and
  Qdrant retrieval (184.8 ms) with the expected primary/complementary storage
  sections, but generation ended in HTTP 503 after 40134.7 ms. The effective
  gateway timeout remains 30 seconds with a two-second minimum interval and no
  retries. The duration strongly supports, but does not prove, a generation
  timeout because the historical event retained neither category nor provider
  status; 429 or saturation are not asserted. Generation now logs sanitized
  start/success/error substages plus category, optional provider status and
  duration, and repeats the safe diagnosis in the final technical outcome.
  Empty or malformed successful responses are validation errors. Offline QA
  covers timeout, HTTP 429/500, network, validation, provider errors, privacy,
  API 503 and authentication without any gateway call. Corpus, 14 Qdrant
  points, threshold, models and frozen academic evidence remain unchanged.
- Final RAG closeout verified that the SLA compensation base is expressly 5%
  of that country's monthly fee after two consecutive country-level months
  below 90%; high-demand conditions are separate and tangential to a pure
  compensation question. The generation contract now answers the concrete
  question, uses complementary evidence only when materially necessary, avoids
  unsupported economic inference, and does not claim exhaustiveness from
  partial context.
- Contextual retrieval is versioned as `rag-contextual-v2`: primary threshold
  remains 0.57 and only the highest-ranked below-threshold candidate per
  established document can be complementary. It cannot establish relevance.
  The versioned evaluation plan preserves the historical 90% and marks a fresh
  independent holdout as required for current Recall@3, abstention and real
  fidelity metrics.
- The carrier human query conclusively failed at embedding with a sanitized
  timeout near 30 seconds and never reached Qdrant. The provider-side cause is
  unknown; no rate-limit or saturation claim is made. Timeouts, retries, model
  and consumption remain unchanged. The Backoffice now presents HTTP 503 as
  temporary provider unavailability rather than insufficient knowledge.
- Final dependency review found a direct critical npm advisory in Next.js
  16.2.7, with 16.4.5 reported as the fixed release. Repository policy requires
  explicit approval to change `package.json`, so the upgrade remains blocking.
  The Python lock is consistent; `pip-audit` was unavailable and was not
  installed. An earlier expanded Compose diagnostic exposed private values in
  session output, so gateway and database credentials require rotation before
  sharing. One PostgreSQL telemetry persistence failure was counted separately
  and has no demonstrated RAG dependency; that subsystem was not changed.

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
- Security handoff upgraded the explicitly authorized Backoffice packages to
  Next.js and eslint-config-next `16.4.5`. The effective container version
  passes focused ESLint, TypeScript, production build, routes/rewrites and the
  retained 90-second proxy setting; `npm audit --omit=dev` is clean.
- A subsequent explicit authorization aligned `uis/website` on Next.js and
  eslint-config-next `16.4.5` while retaining React/React DOM `19.2.4`.
  Website production audit moved from one critical, four high and one moderate
  finding to zero. The shared interfaces image was rebuilt, both named
  `node_modules` volumes were refreshed without deletion, and only the
  interfaces service was recreated. Website lint/typecheck/build and
  Backoffice focused lint/typecheck/build pass; global Backoffice lint retains
  three errors and one warning in unrelated historical files. Both effective
  runtimes report Next.js `16.4.5`; API and Qdrant were not restarted.
- The user-owned gateway and PostgreSQL rotation steps are documented without
  exposing or changing private values. `rag-contextual-v2` now has a
  prospective, hash-bound independent set (12 positive, four negative) and a
  separate material-fact rubric. It has not been executed and no metric was
  inferred; independent review plus explicit authorization for up to 16
  embeddings and 16 generations remains required.
- Final human precision evidence showed that same-document expansion admitted
  a tangential high-demand SLA fragment, while both relevant Aragón carrier
  fragments were present in semantic top three but below 0.57. Retrieval now
  applies deterministic local lexical verification only to semantic top-three
  candidates: two content overlaps plus a corpus-discriminative cue are
  required. The threshold, `k`, corpus, vectors and Qdrant remain unchanged.
  Lexical evidence may establish one primary document and add at most one
  verified complement; generic single-token overlap cannot expand context.
- Sanitized logs prove the repeated carrier fingerprint came from two distinct
  API requests about 19 seconds apart, not an internal retry, but cannot prove
  whether the browser submissions were intentional. The sealed v2 holdout and
  hashes remain unchanged and were not used for rule selection. This method
  change requires independent system-version registration before evaluation;
  no historical or prospective metric was inherited.
- Final evaluation preparation registers the unchanged semantic + lexical
  method as `rag-hybrid-v3`. A guarded runner defaults to offline hash
  validation, requires a hash-bound independent approval record plus an
  explicit 32-request acknowledgement, executes sequentially with the existing
  two-second interval and zero retries, persists after each case, and refuses
  to overwrite results. No gateway request was made.
- Holdout structure, references and rubric facts pass offline review, but the
  implementation-side agent authored and inspected the questions. Six positive
  intents overlap earlier calibration/generation topics, so strict independence
  is not established even though exact wording is unique. The review template
  remains unapproved; execution requires human adjudication or a new set from
  an isolated evaluator.
- Safe credential inspection identifies Supabase and confirms that
  `DATABASE_URL`, both `TRACKFLOW_PIPELINE_*_DATABASE_URL` variables and
  `TRACKFLOW_REPORTING_DATABASE_URL` currently share the password. Rotation is
  user-owned and still pending; no secret value was displayed or changed.
- Supabase post-rotation recovery is now verified. The four private PostgreSQL
  URLs use one Session Pooler identity and role password, with `postgres` as the
  operational source and `trackflow_reporting` as the analytical destination.
  Both databases pass live read-only identity/OID checks, SQLModel inventory
  reads and container-side connectivity. A telemetry insert was verified inside
  an isolated transaction and rolled back with no persistent row.
- Controlled restart of `api`, `interfaces` and `qdrant` preserved data and
  configuration. Qdrant remains at 14 unique 1024-dimension Cosine points whose
  IDs and payloads exactly match the current deterministic corpus chunks.
  Scheduler and async profiles stayed stopped; their optional async PostgreSQL
  tables are not provisioned and must not be started as functional services.
- An obsolete host Uvicorn owned IPv4 port 8000 while Docker owned IPv6. After
  confirming its TrackFlow command, the old process was stopped; IPv4, IPv6 and
  `localhost` now expose the same current 31-route API. The `.env` JWT
  representation was normalized without changing or displaying the active
  secret, and Compose quiet validation passes.
- Permanent recovery instructions are documented in
  `docs/operations/supabase-and-local-startup-runbook.md`; a manual Project
  Learnings note is prepared alongside it. Broad offline QA produced 486 pass,
  two skips and one unrelated CORS preflight failure. The historical
  `test_inventory_client_ids.py` loader also fails collection under Python 3.13
  and remains explicitly uncounted.
- `rag-hybrid-v3` is now bound to a SHA-256 freeze manifest covering retrieval,
  prompt, configuration, API/UI integration, corpus, evaluation runner, tests
  and dependency locks. The ignored private environment is excluded; its
  4Geeks key is present but rotation cannot be proven from local state.
- The existing 12-positive/four-negative holdout remains byte-for-byte sealed
  and validates offline, but is classified as secondary regression evidence
  because it was authored and inspected from the implementation side. A
  minimal independent-evaluator package now contains only the four canonical
  documents and academic criteria. A new isolated set and review record are
  required before real execution; no gateway request or metric was produced.
- Freeze QA completed with 49/49 RAG core tests and 11/11 RAG API tests. The
  evaluator runner now records top-three precision plus semantic-primary,
  lexical-primary and complementary recovery separately; real-answer fidelity
  remains deliberately pending independent adjudication.
- Custodial validation of the externally authored v2 holdout confirmed both
  reported hashes, the 12/4 split, rubric correspondence, grounded commercial
  facts and genuinely absent negative answers. Execution remains blocked
  because all expected references use Markdown paths and top-level headings
  rather than the 14 contractual Qdrant document/section pairs; the
  multi-document case also needs three chunk references. The sealed v2 files
  were not edited and the runner was not rebound. A corrected, newly hashed
  version is required; no gateway request was made.
- The evaluator-supplied v2.1 correction now passes custodial validation: both
  reported hashes match, all 16 IDs and facts remain unchanged from v2, all 14
  declared reference pairs are canonical, and `ihv2-p12` requires the correct
  three chunks. Earlier v2 artifacts remain untouched.
- A hash-bound v2.1 binding configures the guarded runner without changing the
  evaluated RAG. Instrumentation now reports question-level Recall@3 separately
  from per-reference and complete-reference coverage, so the three-source case
  records 3/3 independently of its single Recall@3 contribution. The second
  freeze changed only runner, validator, current evaluation plan and tests;
  all 29 manifest hashes match.
- Final offline preflight passed 50 core/evaluation tests and 11 API/auth tests.
  The blank review template is rejected as designed, no result exists, Qdrant
  remains healthy at 14 points, and no gateway call was made. Live execution is
  ready only after a truthful completed review record and explicit authorization
  for at most 16 embeddings plus 16 generations.
- The TrackFlow user explicitly approved the v2.1 questions, contractual
  references and fidelity rubric after human review. A separate review record
  preserves that the reviewer participated in the project and is not
  independent from implementation; it also preserves the author-exposure,
  intent-overlap and California/Los-Angeles limitations. The approval does not
  authorize gateway consumption. The current runner's stricter independence
  gate will therefore reject this honest record until separately adjudicated;
  no evaluation was executed.
- Traceability correction: the earlier `approved=true` review record was
  incorrectly attributed to the TrackFlow user. It is preserved byte-for-byte
  and a hash-bound amendment marks it superseded and unusable for execution.
  The replacement template is pending a new explicit user decision and already
  declares `independent=false` plus all known limitations.
- The runner gate now requires an attributable reviewer, timezone-aware date,
  approval source, complete checks and a truthful review classification. Strict
  independence is optional: a non-independent review is accepted only under
  `prospective_with_independence_limitations` with explicit acknowledgement;
  an independent label requires a declared basis. Focused gate/RAG QA passes
  51 tests. No holdout execution or gateway call occurred.
- The self-identified TrackFlow responsible user then supplied a new explicit
  human `APPROVED` statement and acknowledged the documented independence
  limitations. A separate hash-bound record passes the gate as
  `prospective_with_independence_limitations`; it remains
  `execution_authorized=false`. Review approval is complete, but no gateway
  consumption or live holdout execution is authorized yet.
- A later explicit one-run authorization permitted at most 16 embeddings and
  16 generations. The real v2.1 run stopped correctly at the first technical
  error after three embeddings and three generations: `ihv2-p03` generation
  timed out with no provider HTTP status. Two positive cases completed, both
  with the expected section at semantic rank 1; their 2/2 partial Recall@3 is
  not the required 12-case academic metric. No negative or multisource case ran.
- Custodial fidelity passes `ihv2-p01` and fails `ihv2-p02`, whose answer kept
  the 5% country-fee basis but omitted the 90% country-level SLA and the
  two-month trigger. Provider usage was 110 embedding tokens and 1,176
  generation tokens, with reported generation cost `0.00012468456`. No retry
  or second execution occurred; final evaluation status is BLOCKED.
- Follow-up adjudication preserves the frozen `p02` FAIL and 1/3 score. Its
  direct answer is commercially correct for compensation and fee base, and the
  two-month premise already appears in the question, but the 90% threshold is
  omitted and the frozen rubric still requires all three facts.
- The original runner cannot resume and refuses to overwrite its result. A new
  evaluation-only continuation tool, outside the 29 frozen-system files,
  validates the immutable parent hash, skips completed `p01/p02`, and resumes
  from incomplete `p03`. Because exact `p03` context was not persisted, it must
  rerun the full case rather than perform generation-only recovery. Maximum new
  budget is 14 embeddings plus 14 generations (28 total), with a new explicit
  authorization, zero retries and separate attempt-2 reporting. Offline resume
  QA passes four tests; no external request was made.
- The authorized attempt-2 continuation stopped at its first operation: the
  `ihv2-p03` embedding timed out after approximately 30.6 seconds with no HTTP
  status or usage response. It made one embedding request and no generation
  request; the immutable parent and all 29 frozen hashes remain unchanged.
- Gateway diagnosis found no local payload-size, retry, redirect or client-close
  defect. Attempt 1 timed out in DeepSeek generation after a successful 6.7 s
  embedding and local retrieval; attempt 2 timed out in the embedding model.
  Both align with the shared 30 s `httpx` boundary, but provider saturation,
  rate limiting and upstream causes remain unproven because neither failure
  returned an HTTP response.
- Evaluation-only immutable stage checkpoints now bind successful embeddings,
  retrieval traces, generations and later fidelity adjudication to the case,
  question, holdout/rubric/binding, freeze manifest and corpus hashes. The
  adapter wraps the unchanged frozen RAG and refuses overwrite. Ten focused
  checkpoint/resume tests and 62 offline RAG/API regressions pass. No historical
  result was altered and no gateway call occurred during this diagnosis.

## Milestone 7 — technical closure for Draft PR

- Active RAG development is closed by user decision. The implemented system
  remains `rag-hybrid-v3` over four canonical documents, 14 deterministic
  chunks and the green `trackflow_knowledge` Qdrant collection (1024/Cosine).
  Retrieval, prompts, models, threshold, corpus and historical artifacts were
  not changed during closure.
- Holdout v2.1 remains incomplete: attempt 1 completed `p01/p02` and timed out
  during `p03` generation; attempt 2 timed out during the `p03` embedding. Total
  consumption is four embeddings plus three generations. `p01` fidelity passed,
  `p02` retains its frozen FAIL, and final v3 Recall@3 is not calculable. The
  historical threshold-only 9/10 = 90% is not relabeled as current evidence.
- Final offline closure QA passes 83 Python tests covering RAG, checkpoints,
  API, JWT, roles and serialization. Website full lint and TypeScript pass;
  Backoffice milestone-focused lint and full TypeScript pass; both Next.js
  16.4.5 production builds pass. Offline production npm audits report zero
  findings and `uv lock --check` passes.
- Backoffice global lint still reports three errors and one warning in
  historical non-RAG profile/login/AuthContext/inventory files. A Python
  vulnerability scanner is not installed. These are disclosed limitations and
  were not modified as part of the RAG closeout.
- Compose validation passes. API, Website, login and Knowledge Base respond
  locally, and Qdrant remains healthy with exactly 14 points. No external model
  request, reindex, holdout run, Git staging, commit, push or PR occurred.
- The exact 18 tracked plus 76 untracked delivery files, mandatory exclusions,
  proposed commit, complete Draft PR text and Milestone 8 transition are
  recorded in `docs/rag/pre-pr-handoff.md`. Website now ignores `.env*` and
  `*.tsbuildinfo` to prevent QA artifacts or private frontend settings from
  entering staging.
- The next roadmap item is Milestone 8 (Agents), recommended as a separate
  `feature/agents-customer-support` branch only after the user-owned RAG commit
  and Draft PR. No agent implementation has started.

## Milestone 8 — LangGraph agent, Part 1

- The approved Part 1 implementation now wraps the unchanged `RAGService`
  retrieval and generation operations in a compiled `StateGraph`. Retrieval is
  executed once, its exact selected context is passed to generation, and the
  existing `QueryResult`, source deduplication, abstention and technical-error
  behavior remain observable at the authenticated `/knowledge/query` endpoint.
- The graph uses an in-process `InMemorySaver` only. Every execution receives a
  UUID `run_id`/`thread_id`; content-bearing checkpoints are deleted after both
  terminal and failed runs. Sanitized traces are bounded to 100 runs/one hour
  by default and retain only node order, transitions, timings, counts, booleans
  and controlled error category/status.
- Three deterministic compiled-graph evaluations pass with local fakes:
  answered with the authentic high-demand source and policy facts, retrieval
  abstention without generation, and generation abstention. These evaluations
  verify architecture and do not claim live-model accuracy. Focused graph/API
  QA passes 21 tests, including equivalence against `RAGService.query()` for all
  three outcomes and retrieval/generation error propagation.
- `langgraph 1.2.20` is locked through `uv`; the FastAPI image builds and imports
  the graph and route successfully without starting services. No gateway call,
  DDL, reindex, Git write or live service startup occurred. A wider offline RAG
  run passed 86 tests and retained seven pre-existing Windows byte-hash failures
  caused by CRLF-sensitive historical evaluation artifacts; no frozen RAG file
  was changed.
- The observability correction preassigns one UUID per entered API execution and
  reuses it as `run_id`, LangGraph `thread_id`, and the HTTP correlation header
  for both 200 responses and controlled in-execution 503 responses. A 503 raised
  before route execution remains sanitized without a fictitious run ID. The
  focused independent API, agent, and global-contract review passes 32 tests.
- Python 3.10 compatibility no longer depends on `typing.Required`: a total
  input `TypedDict` makes only `question` required, while the derived graph state
  keeps `context`, `answer`, and `result` optional. An authorized uv-managed
  Python 3.10.21 installation passes graph import/compilation and 11 agent tests;
  the combined API/agent suite passes 22 tests under Python 3.12.14. The
  required/optional contract is confirmed, and both `uv lock --check` and
  `git diff --check` pass.

## Milestone 8 — RAG gateway resilience revision

- `rag-hybrid-v3.1-resilience` adds an 80-second monotonic query budget below
  the local 90-second Next.js proxy boundary. Embeddings receive at most two
  selectively admitted attempts; Qdrant and generation remain single-call.
- Retry classification uses controlled error category and HTTP status. Only
  timeouts, network failures, 429 and 500/502/503/504 qualify. Backoff, jitter,
  gateway pacing and bounded `Retry-After` all consume the shared budget.
- Successful query embeddings may be reused from a process-local, bounded
  LRU/TTL cache with exact derived identity and single-flight concurrency.
  Errors, invalid vectors, answers and provider bodies are never cached.
- Sanitized graph traces now report cache/retry/deadline metadata and external
  request counts under the existing `run_id`; CORS exposes the run header.
- The corpus, prompt, models, semantic threshold, frozen results and historical
  manifests remain unchanged. No Qdrant reindex was made.
- Offline QA passes 94 resilience/LangGraph/RAG tests with the four known
  Windows CRLF byte-hash checks explicitly deselected, plus 17 non-HTTP
  serialization checks. In the supported Python 3.12 container, the complete
  RAG API suite passes 13/13 and serialization passes 19/20; its sole failure
  is a pre-existing assertion that requires a Windows backslash path while the
  container is Linux. The Python 3.13 ASGI suites still hang on their first
  request and were stopped rather than reported as passing.
- The final Python 3.12 API image builds and runs locally. API health, Backoffice
  login and Knowledge Base return successfully; Qdrant remains healthy with 14
  unique 1024-dimension Cosine points and valid TrackFlow payload contracts.
- Manual acceptance is complete. Black Friday runs
  `598082eb-9be1-4fcd-8027-2f52f01b4cbb` and
  `c58afad0-bd18-4ce4-a186-cbd23d549824` share fingerprint `34151fcc97eb` and
  prove `miss` (one embedding request) then `hit` (zero embedding requests).
  Both retained the same two commercial sources, with one Qdrant call and one
  generation call per query. Their HTTP durations were 13,628.7 ms and
  15,129.3 ms respectively; generation variability made the cache hit slower
  end to end, so total latency is not cache evidence. The unsupported transport
  case returned HTTP 200 with `insufficient_context=true`, no generation and no
  technical error. No retry, deadline exhaustion or sensitive trace content was
  observed in the accepted live runs; retry/deadline behavior remains covered
  by deterministic offline tests.
- Final closeout added explicit integration coverage for a persistent embedding
  failure reaching LangGraph as `retry_exhausted`, plus HTTP coverage proving
  `retry_exhausted` and `deadline_exhausted` return sanitized 503 responses with
  the same correlated run ID. The focused resilience suite passes 36/36 and the
  supported Python 3.12 RAG API suite passes 13/13.
- Documented deadline limitation: synchronous `httpx` read timeouts measure
  inactivity between chunks. The 80-second budget controls admission and phase
  allocation but cannot hard-interrupt an adversarial slow-trickle response.

## Milestone 9 prerequisite — incident query and tenant security

- Added an explicit, separately provisioned PostgreSQL design for verified
  clients, user/client memberships, operational incidents and append-only
  incident history. Operational tables are isolated from startup
  `SQLModel.metadata.create_all`; `infra/sql/incident_query_security.sql` is a
  reviewable DDL artifact and has not been applied.
- Added shared `AccessContext` authorization sourced from authenticated TinyDB
  identity plus PostgreSQL membership. Admin has global access; manager and
  user access is restricted to assigned active clients; user is read-only for
  the new operational incident capability.
  Membership creation is admin-only and verifies the TinyDB user before the
  PostgreSQL write without a fictitious cross-database foreign key.
- Added authenticated operational incident POST/GET/PATCH routes alongside the
  unchanged academic analyze/export routes. Incident IDs are database-sequence
  derived (`TRF-000001`), status changes use optimistic versioning and approved
  transitions, and cross-tenant/not-found reads share the same 404 projection.
  Email is optional, permission-redacted, and never replaced with invented data.
- Operational incident creation is validated by its typed contract rather than
  the academic CSV carrier whitelist. Newly encountered authentic carriers are
  accepted, email remains optional, and genuinely invalid country, category,
  email, tracking length and description values remain rejected. Focused
  incident QA passes 18 tests with one Python 3.13 ASGI scenario skipped.
- Inventory product/order reads now filter tenant scope in SQL, aggregate stock
  only for visible SKUs, use tenant-scoped caches, and omit movement actor UUIDs
  from HTTP projections. Every inventory mutation now requires an
  `AccessContext`: admin is global, manager is limited to active assigned
  clients, and user is denied. SKU, client and commercial-order targets are
  filtered in SQL before idempotency or domain processing; foreign and missing
  targets share a sanitized 404. Computed stock semantics remain unchanged.
- Inventory client identity now fails closed against active
  `trackflow_client` rows instead of treating the four historical static
  clients as operational truth. Sensitive membership flags never grant PII to
  role `user`, including direct `AccessContext` construction. Cache database
  namespaces use weakly registered monotonic tokens rather than reusable
  Python object IDs.
- The reviewed DDL uses explicit `public` objects, explicitly named identity
  sequences for incidents and history, and a stored PostgreSQL-generated
  incident ID. Incident creation is one INSERT with `RETURNING`; no second
  UPDATE remains. History transitions,
  reopen reasons and status/score coherence are database-checked, and the
  redundant history index was removed. A versioned read-only preflight is
  included; the DDL explicitly does not claim or configure RLS.
- The Backoffice incident page now supports authenticated operational lookup,
  creation and status history. Creation/mutation controls are hidden from role
  `user`, and closing requires an operator-entered satisfaction score.
- Focused deterministic SQLite coverage passes 15 tests for IDs, history,
  optimistic locking, all seven inventory mutation families, role and tenant
  matrices, cache isolation and fail-closed client registration,
  tenant hiding, PII redaction, persisted memberships, scoped inventory and the
  controlled admin registration flow. Two HTTP ASGI scenarios are present but
  skipped on the available Python 3.13 runtime because of the repository's
  known FastAPI ASGI hang.
- Additional Python 3.13 regression QA passes 71 product/order-endpoint tests,
  38 movement/business-producer tests, 29 inventory-order tests (three known
  lifespan cases deselected), 47 LangGraph Part 1/Gateway Resilience tests and
  16 static serialization tests. `compileall`, `uv lock --check` and
  `git diff --check` pass. The Windows Python 3.12 isolated attempt remained
  blocked in slow dependency/module filesystem reads, but a Linux Python 3.12
  API container using the current code subsequently passed lifespan, health,
  authentication and real HTTP incident/inventory authorization scenarios.
- Backoffice `npm ci` repaired only `node_modules`; package manifests and lock
  files are unchanged. TypeScript and focused ESLint for all touched frontend
  files pass. Global ESLint still reports three pre-existing errors and one
  warning in unrelated profile/login/auth/inventory files. The webpack
  production build passes completely outside the restrictive sandbox,
  including compilation, TypeScript, all 18 static routes and build traces.
- PostgreSQL 16.15 integration now passes in the authorized disposable
  `trackflow-incident-qa-20261108` container on loopback port 55439, backed by
  `tmpfs` with no persistent mounts. The first DDL attempt exposed invalid
  schema-qualified index-name syntax and rolled back atomically; the two index
  names were corrected and the second application committed successfully.
  Catalog checks confirm four operational tables, two named identity sequences,
  23 constraints, nine indexes and the append-only trigger. Synthetic tests
  pass generated IDs, `INSERT ... RETURNING`, FK/check failures, rollback,
  immutable history, PII redaction, tenant isolation and inventory writes.
- A real two-session PostgreSQL race exposed that stale version validation ran
  after transition validation. The order is corrected so stale writers
  consistently receive `IncidentConflict`; the repeated race produced one
  successful version-2 update and one conflict. Manual Backoffice acceptance
  remains pending. The disposable database remains running for that later gate.
- Manual acceptance exposed two QA frontend startup/configuration blockers.
  `next dev` served the shell but failed its HMR WebSocket and never mounted the
  login client tree; the acceptance server now uses the verified production
  build. The operational client request also lacked a Next.js rewrite and was
  answered by Next's own 404 instead of FastAPI. `/api/access/:path*` now
  proxies to the configured backend. Playwright against the real QA services
  confirms HTTP 200, no client-access error, a visible selector populated from
  PostgreSQL QA and no console or network errors. Human acceptance remains
  pending.
- Manual incident creation then exposed inappropriate reuse of the academic
  CSV carrier allowlist by the operational write service. The operational
  typed contract is now authoritative: an authentic carrier name and omitted
  optional email produce `OPEN` version 1 with one append-only `CREATED`
  history row, while invalid taxonomy, email, tracking length and description
  remain rejected. The failed human attempt did not persist a row.
- The first successful browser retest also exposed a post-201 UI error caused
  by dereferencing React `event.currentTarget` after an `await`; the form
  reference is now captured before the asynchronous request. A separate
  Playwright context against the production Backoffice and disposable
  PostgreSQL completed create, close, reopen and discard as versions 1 through
  4, with HTTP 201/200 responses, no failed requests and exactly one database
  row for the generated tracking number. A genuinely short tracking number
  returned 422 and persisted no row. Transparent HTTP retry idempotency is not
  yet part of the write contract and remains an explicit product decision;
  uniqueness on tracking number would be incorrect because multiple incidents
  may legitimately refer to one shipment.
- Human acceptance completed the operational lifecycle of `TRF-000012` through
  `OPEN` v1, `CLOSED` v2 and a reasoned reopen to `OPEN` v3, including lookup
  after a full Backoffice reload. The human result was not repeated or claimed
  by automation.
- Final QA created two non-sensitive temporary memberships only in disposable
  PostgreSQL. Authentic HTTP and direct-service checks confirmed owner access,
  foreign/missing/invalid 404 equivalence, user write denial, admin global
  access and email redaction. Inventory HTTP checks confirmed tenant-scoped
  products and caches, correct zero versus five-unit stock, hidden foreign
  SKUs, denied user writes and no private movement actor projection. No active
  manager identity exists in the isolated auth copy, so manager scope was
  verified against the real service/database with a synthetic QA access
  context: one own-client movement persisted and the foreign-client attempt
  produced 404 and zero rows.
- A separate negative ticket completed invalid-transition, missing-reopen-
  reason, stale-version and real two-request concurrency checks. The race
  produced one HTTP 200 and one 409, the ticket ended `DISCARDED` v4 with
  history versions 1–4, and invalid or denied creates persisted no rows.
- Final review sanitized incident telemetry templates so GET/PATCH traces use
  `/api/incidents/{incident_id}` rather than a public incident identifier. Two
  historical inventory-client test fixtures were aligned with Python 3.13 and
  the current nullable threshold column; all six migration tests pass. Focused
  incident/tenant, inventory, Backoffice, LangGraph and resilience regressions
  pass. The known Python 3.13 ASGI/lifespan hangs remain explicitly skipped or
  environment-blocked rather than reported as passes.

## Milestone 9 — LangGraph external tools, backend unit 1

- Added typed read-only ticket and inventory tools that use the existing
  authorized domain services. Ticket projections exclude email, tracking,
  descriptions, history reasons, actors and internal references; inventory
  preserves the existing computed-stock behavior and hides foreign products as
  missing.
- Extracted tenant-aware single-product reading into
  `read_inventory_product()` and made the existing HTTP endpoint reuse it.
- Added a deterministic English router and compiled multi-source support graph
  for knowledge, ticket, inventory, mixed and clarification routes. The graph
  reuses the Part 1 `retrieve_context`, `generate_answer`, `abstain` and
  `finalize_answer` operations through a shared builder; it does not call
  `RAGService.query()` as an opaque node.
- Added authenticated `POST /support/query` for admin and manager. Its request
  accepts only the required question, constructs one `AccessContext`, starts a
  shared 80-second deadline before graph routing and creates RAG configuration
  and clients lazily only for routes that need knowledge.
- PostgreSQL tool reads receive a transaction-local statement timeout capped
  at 5 seconds and the remaining shared deadline while preserving the existing
  3-second response reserve. `pool_timeout` is unchanged;
  pool checkout remains an infrastructure-level limit outside PostgreSQL's
  statement timeout.
- Mixed results are assembled without another model call. Successful sources
  survive technical failure of another source as an explicit partial response;
  total technical failure is a sanitized 503 with the graph run ID.
- Extended bounded tracing records route, node/tool order, component status,
  controlled error category, call counts, partial state and existing RAG
  resilience metadata without questions, identifiers, stock values or response
  content. Checkpoints remain ephemeral and are deleted in `finally`.
- Backend/unit QA covers routing, identifier normalization, ambiguity,
  tenant isolation, stock zero, tool timeouts, lazy RAG, shared deadline,
  partial/total failure, role policy, trace privacy, checkpoint cleanup,
  inventory/incident regression and Part 1/Gateway resilience. Python 3.13
  continues to hang in the historical ASGI RAG API suite; static OpenAPI and
  response-contract regression passes, and no gateway call was made.
- Backoffice Phase C remains intentionally outside this unit. No DDL, database
  mutation, dependency change, provider call or Git write was performed.

## Milestone 9 — LangGraph external tools, Backoffice and evaluations

- Added the authenticated `/backoffice/support` experience for admin and
  manager roles, preserving the existing AuthGuard and navigation patterns.
  The UI renders clarification, partial response, ticket, inventory and
  knowledge-source states without exposing sensitive ticket fields or raw
  backend errors.
- Added a typed Backoffice API client for `POST /support/query` and the matching
  Next.js `/support/:path*` backend rewrite. Telemetry uses the static
  `/support/query` template; Knowledge Base navigation and behavior remain
  unchanged.
- Added focused frontend contract tests for authentication/role gating,
  navigation, proxy routing, safe telemetry and partial-state presentation.
  TypeScript, ESLint and production build remain the frontend acceptance gates
  because this package has no browser-unit-test dependency.
- Added a reproducible offline evaluation runner and sanitized evidence artifact
  covering knowledge, tickets, inventory, mixed queries, clarification and
  partial dependency failure. The runner uses only local fakes and does not
  open a gateway, database or authentication connection. Its artifact contains
  no question, answer, record ID, SKU, stock or operational data.
- No DDL, persistent data, dependency, root configuration, protected file,
  provider, Supabase, authentication store or Git state was modified.

## Milestone 9 — regression test cleanup

- Added cross-platform Git attributes for hash-bound RAG JSON artifacts. Files
  whose committed blob is LF now materialize as LF even when
  `core.autocrlf=true`; the three requested frozen inputs were verified as LF.
  Explicit exceptions preserve the sealed CRLF byte representation of the
  result artifacts whose review hashes depend on it. No evidence content,
  expected hash or evaluation result changed.
- Updated inventory HTTP regression fixtures to use isolated SQLite plus the
  per-test TinyDB identity store, operational security metadata, an active
  client and a real manager membership. Requests still authenticate through
  the real HTTP login and exercise the production authorization dependencies.
- CORS coverage now distinguishes preflight allow-origin/method/header behavior
  from exposed response headers on a real request with `Origin`. Inventory
  order projections explicitly assert that the private `user_uuid` field is
  absent, and tenantless legacy inventory remains hidden.
- Removed the two Python 3.13 ASGI skip gates after isolated evidence showed
  each affected incident and inventory authorization test passing three
  consecutive executions without timeout. Their test bodies and assertions
  remain unchanged; final combined QA is delegated to the independent gate.
