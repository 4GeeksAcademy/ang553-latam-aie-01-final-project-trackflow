# Progress: TrackFlow

## TrackFlow MCP Server — implementation gates A–F

- Gate A **PASS**: the root runtime pins `langchain-mcp-adapters==0.3.2` with
  MCP SDK v1 (`mcp>=1.28,<2`). The independent `mcps/trackflow/` environment
  isolates `mcpauth==0.2.0b1` from the root `requests`/email dependency
  constraint; both uv locks and imports were verified on Python 3.12.
- Gates B/B2 **PASS**: the MCP boundary uses `mcpauth`, Streamable HTTP and
  Protected Resource Metadata with strict RS256 issuer/audience/signature/
  expiration checks and exact scopes. Standard Token Exchange V2 produces a
  distinct `trackflow-api` bearer. The API preserves local HS256 callers while
  accepting OIDC only on the approved incident/inventory routes; roles and
  tenant memberships always come from TrackFlow persistence.
- Gate C **PASS**: five typed MCP tools expose incident create/read/status and
  inventory list/read through existing HTTP endpoints. Safe projections,
  scope-before-network enforcement, GET-only inventory, sanitized errors,
  request correlation and secret-free structured logging are covered by
  adversarial tests. No database or business service is imported by the MCP.
- Gate E1 **PASS**: disposable Keycloak 26.8.2, PostgreSQL 16 `tmpfs`, temporary
  TinyDB, API and MCP proved real PRM, RS256 claims, login, token exchange,
  distinct audience bearer, identity mapping, 401/403 behavior, persistence,
  version conflict, tenant isolation and inventory mutation rejection. The
  opt-in E2E result was **1 PASS in 6.10 s**; MCP unit QA was **50 PASS in
  4.78 s** and the root OIDC regression was **61 PASS in 17.18 s**. Secret/JWT/
  payload scans were clean. All QA services and owned containers are stopped;
  ports 18082, 55440, 18083 and 18084 were confirmed free.
- Gate D alternative A **PASS**. `/support/query` now accepts only a Keycloak RS256
  token for `trackflow-mcp` with both read scopes, links it to an active
  TrackFlow user, and derives role/memberships from TrackFlow persistence. The
  async endpoint and graph use `graph.ainvoke`; operational nodes call only MCP
  tools through `langchain-mcp-adapters`, with no SQL/direct fallback.
  Backoffice retains its local JWT globally and uses a separate, in-memory
  Authorization Code + PKCE S256 token only for Support. Focused OIDC, adapter,
  deadline, graph and endpoint QA was **54 PASS in 6.56 s**. A fresh disposable
  Keycloak/API/MCP/PostgreSQL/LangGraph run completed **1 PASS in 13.90 s**
  (18.38 s command wall time), including real PKCE, dual-session isolation,
  token exchange, mixed and cross-tenant Support paths, optimistic concurrency
  and inventory write rejection. Independent database reads confirmed one
  closed version-2 incident with two history events, two unchanged SKUs and no
  forbidden mutation. Runtime scans found no secrets, complete JWTs or payload/
  PII markers in owned logs. All owned processes, containers and temporary data
  were removed; ports 18082, 55440, 18083 and 18084 are free.
- Gate F documents the disposable QA procedure and the identity decision. No
  service, credential, persistent user/data store or Git state was modified by
  this documentation gate.
- The principal human functional acceptance with Backoffice and MCP Inspector
  is complete. Interactive Keycloak OAuth, discovery of exactly five tools,
  authorized incident and inventory reads, tenant isolation, and the complete
  create/read/status lifecycle were verified manually. Incident `TRF-000002`
  was created as `OPEN` version 1, changed to `CLOSED` version 2 and read back
  with the persisted result. Backoffice Support manually passed the Ticket,
  Inventory, Mixed and Clarification routes; the OIDC expiry redirect preserves
  and replays the pending question.
- Inventory read-only enforcement has two distinct evidence levels. Human MCP
  Inspector acceptance verified the published discovery and permitted inventory
  reads. The explicit rejection of an unknown inventory mutation tool, the
  adapter's pre-network rejection of non-GET methods and the absence of a
  downstream mutation were established by focused adversarial automation, not
  by a manual inventory write attempt.
- Private GitHub Codespaces QA infrastructure is prepared under
  `mcps/trackflow/qa/codespaces/`. Its reproducible launcher, loopback-only
  Compose topology and restrictive single-port gateway now support the
  designated MCP Playground through a short-lived, exact-scope bearer obtained
  with Keycloak Device Authorization Grant. No callback is invented. Direct
  Access Grants, Client Credentials Grant, DCR, implicit flow,
  service accounts and general browser authorization remain disabled. The
  helper releases a validated token only through non-interactive stdout for a
  direct clipboard pipe; it does not persist or display the bearer. Gateway,
  claim, polling, malformed-response, secret-handling and cleanup contracts
  passed private/static QA, including 89 focused MCP/Codespaces tests. No port
  was published and no real Codespaces or external Playground validation is
  claimed.
- The remaining acceptance work is explicit: start the stack in a real GitHub
  Codespace, connect the designated public MCP Playground, complete external
  academic certification, and validate the real Knowledge/RAG route when its
  private provider configuration is available.
- Repository-wide Backoffice ESLint is not PASS. Three historical errors and
  one warning remain in files outside the MCP change set. Focused lint for the
  modified MCP Backoffice files, TypeScript validation and the isolated
  production build pass; the global ESLint debt is not attributed to MCP.

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

## Keycloak 26.8.2 Hardening Fix — 2026-11-14

- **Issue:** QA launcher failed during Keycloak hardening verification:
  `RuntimeError: Public QA client hardening verification failed.`
  (line 131 of `mcps/trackflow/qa/codespaces/harden_keycloak.py`)

- **Root Cause:** Keycloak 26.8.2 returns `null`/`None` for the
  `authorizationServicesEnabled` field when disabled. Verification code
  checked `is not False`, which rejected `None`, causing false negative.

- **Fix Applied:** Modified verification logic to accept both `False` and
  `None` as valid disabled states:
  - Line 119: Public client check → `not in (False, None)`
  - Line 183: MCP client check → separated, same logic

- **Testing:** Added `test_keycloak_client_fields.py` with 3 regression tests
  covering the field behavior and verification logic. All pass.
  Full integration test (config → harden → verify) passes.

- **Security:** No security degradation. OAuth controls, grants, scopes and
  hardening policy remain unchanged. Fix corrects verification only.

- **Status:** ✓ QA launcher now ready. Next: re-run `start.sh` for full
  Codespaces QA environment and MCP Playground validation.

## SQL Provisioning Fix for QA Launcher — 2026-11-14

- **Issue:** After Keycloak hardening completed, launcher failed during seeding:
  `psycopg2.errors.UndefinedTable: relation "trackflow_client" does not exist`

- **Root Cause:** TrackFlow uses TWO separate table management systems:
  - `SQLModel.metadata`: Inventory + auth (auto-created by `create_db_and_tables()`)
  - `operational_security_metadata`: Incidents (explicitly provisioned via SQL)
  Launcher never applied the explicit DDL (`infra/sql/incident_query_security.sql`).

- **Fix Applied:**
  1. Created `provision_qa_ddl.py` (124 lines): Applies incident DDL to QA database
  2. Modified `start.sh`: Inserted DDL step after hardening, before seed
  3. Added `test_qa_provisioning.py` (251 lines): 3 regression tests

- **Regression Tests (All PASS):**
  - `test_ddl_provisioning_creates_four_tables`: Verifies 4 tables created
  - `test_seed_fails_without_ddl`: Confirms seed fails without DDL (as expected)
  - `test_full_integration_ddl_then_seed`: Full flow succeeds

- **Security:**
  - Validates QA environment marker (`TRACKFLOW_MCP_QA=1`)
  - Only operates on loopback QA instance (127.0.0.1:55440)
  - No password logging or temporary files
  - Transaction rollback on error
  - No production schema modification

- **Status:** ✓ Launcher sequence fixed. Full Codespaces QA should now proceed
  from Keycloak hardening → DDL provisioning → seed → API startup → MCP runtime
  → gateway → full `PRIVATE_READY` signal.

## MEM-092 — Memoria y auto-mejora de agentes, Parte 1 — 2026-11-15

- **Estado técnico:** implementación local completa y pendiente de revisión humana,
  migración/despliegue y PR. No se declara aceptación del hito.
- Se añadió memoria episódica estructurada en PostgreSQL con ownership de
  conversación por identidad, propuestas con consentimiento, decisiones
  append-only, entradas versionadas y autorización aprobatoria verificable.
- El mismo `ExternalSupportAgent` incorpora prefiltro determinista,
  autoevaluación estructurada acotada, recuperación tenant-aware y renderizado
  `user_reported` no confiable. RAG, Qdrant, MCP, OIDC y checkpoint efímero
  mantienen sus contratos existentes.
- `POST /support/query` se extendió aditivamente con continuidad, selección de
  cliente/ámbito y propuesta segura. `POST /support/memory/decision` implementa
  approve/reject/edit/ambiguous/topic_change con idempotencia, edición derivada en
  backend y errores sanitizados. Backoffice mantiene la conversación solo en
  `sessionStorage` namespaced por usuario.
- DDL explícito: aplicar `infra/sql/incident_query_security.sql` y después
  `infra/sql/agent_memory.sql`. No se ejecuta provisioning al importar y el DDL
  de memoria todavía no se ha aplicado a una base persistente del proyecto.
- Cleanup es funcional, transaccional e idempotente, pero requiere que un
  scheduler u operador externo invoque `MemoryStore.expire_and_clean`; el hito no
  instala el scheduler.
- QA local segura cubre persistencia, consentimiento, concurrencia, aislamiento,
  autoevaluación, recuperación, limpieza, API, ciclos de aprobación/rechazo,
  contratos UI y regresión Support/RAG. El arnés PostgreSQL opt-in exige una base
  loopback QA vacía y desechable. El gate final se repitió sobre PostgreSQL 16
  fresh en `tmpfs`: 10 pruebas pasaron en 2.25 s, exit 0, y el contenedor se
  autoeliminó al terminar.
- **Pendientes honestos:** validar en vivo el JSON Schema del proveedor
  estructurado; ejecutar E2E real de navegador y aceptación manual; resolver el
  panic ambiental de Turbopack (el build Webpack sí compila); programar cleanup;
  aplicar migración, desplegar, abrir/revisar PR y obtener aceptación humana.
- Documentación técnica pública: `docs/agent-memory.md` y
  `services/api/agent_memory/README.md`.
- Correctivo de presupuesto de autoevaluación: el cap pasó de 6 a 22 segundos
  para cubrir el timeout de generación configurado (20 s) más el pacing por
  defecto (2 s), siempre limitado por `QueryDeadline` menos la reserva. Timeout
  del proveedor permanece fail-closed, sin reintentos ni pérdida de la respuesta
  principal. QA focal: **41 PASS en 7.70 s**; regresión independiente ampliada
  de memoria, Support y pacing: **169 PASS en 16.16 s**.

## MEM-092 — correctivo previo a aceptación humana — 2026-11-15

- El siguiente turno de una conversación con propuesta pendiente se clasifica
  ahora contra esa propuesta mediante el gateway FourGeeks y JSON Schema
  estricto. El mismo flujo soporta aprobación, rechazo, edición con
  reconfirmación, descarte ambiguo, cambio de tema y una pregunta adicional
  separada que atraviesa una sola vez el agente Support normal.
- La identidad, conversación, propuesta, tenant, categoría y ámbito continúan
  recuperándose y validándose en backend. El contenido editado se deriva de la
  propuesta original y vuelve a pasar por la política de memoria. Baja
  confianza produce descarte ambiguo; el fallback sólo reconoce expresiones
  completas exactas y nunca substrings.
- El Backoffice ya no convierte preventivamente un nuevo mensaje en
  `topic_change`: lo envía a `/support/query` para la clasificación
  conversacional auténtica. Los botones explícitos Accept/Reject/Edit conservan
  `/support/memory/decision` e idempotencia.
- Añadido `python -m scripts.agent_memory_cleanup --execute` como invocador
  operacional mínimo. Exige PostgreSQL, no provisiona, no instala scheduler y
  emite sólo contadores. Reutiliza la limpieza transaccional existente para
  expiración, capacidad e historial auditable.
- QA del correctivo: regresión integrada memoria/Support **122 PASS en 15.37 s**;
  regresión OIDC/MCP/RAG/Support **166 PASS en 22.29 s**; integración PostgreSQL
  16 fresh en loopback/`tmpfs` **11 PASS en 3.39 s** y contenedor eliminado;
  contratos frontend + cleanup CLI **16 PASS en 2.96 s**; ESLint focalizado,
  TypeScript, compileall, `git diff --check` y build Webpack de 19 rutas PASS.
- La función de secretos del runbook fue reemplazada por una forma compatible
  con Windows PowerShell 5.1 y PowerShell 7; se validó una salida hexadecimal de
  64 caracteres sin imprimir el secreto.
- Pendiente honestamente: compatibilidad del JSON Schema contra el proveedor
  vivo y aceptación humana con navegador. La periodicidad del comando de
  cleanup debe configurarse en el mecanismo de mantenimiento del despliegue; no
  se añadió otro servicio. No se aplicó DDL ni se modificaron datos persistentes.

## MEM-092 — correctivo de contrato JSON Schema — 2026-11-15

- La autoevaluación selecciona ahora un esquema cerrado específico para
  `carrier_coverage`, `recurring_incident` o `b2b_report_preference`. Todos los
  campos son obligatorios y estrictos, los objetos rechazan propiedades extra y
  `country` sólo acepta `US` o `ES`; `should_propose=false` usa la forma cerrada
  `proposals=[]`.
- El contrato interno ya no conserva contenido arbitrario: usa modelos tipados
  por categoría antes de volver a aplicar `canonical_key_for`,
  `_validated_content` y las validaciones independientes de `MemoryStore`.
- Las trazas distinguen únicamente códigos sanitizados de formato, país,
  política de retención, autorización, conflicto y contrato; no incluyen el
  prompt, JSON crudo ni contenido de memoria.
- QA aislado del correctivo: **39 PASS en 6.86 s**; regresión ampliada de memoria,
  consentimiento, API y Support: **165 PASS en 15.45 s**. `compileall`, validación
  estructural de los tres schemas y `git diff --check` pasaron. No se usaron
  PostgreSQL QA ni servicios activos.
- Pendiente honestamente: el gateway se validó con transporte simulado; la
  aceptación del JSON Schema por FourGeeks real sigue sin certificación externa.

## MEM-092 — continuidad OIDC y recuperación de propuestas — 2026-11-15

- Backoffice restaura la conversación en recargas ordinarias y callbacks OIDC,
  y recupera la propuesta pendiente mediante una lectura autenticada que vuelve
  a aplicar ownership, tenant, estado y expiración en backend.
- Los intents de decisión guardan únicamente usuario, conversación, propuesta,
  acción y clave idempotente. Una renovación nunca aprueba automáticamente: la
  UI exige un reintento explícito y reutiliza la misma clave.
- Cliente y ámbito se rehidratan desde la propuesta autorizada. Cambio de
  usuario, propuesta resuelta/expirada, respuesta cruzada y `404/409` invalidan
  el intent visible; las respuestas Support existentes se conservan durante
  solicitudes del mismo usuario cuando es posible.
- La clave legacy de aprobaciones/rechazos interrumpidos se migra únicamente
  después de recuperar la propuesta para el usuario autenticado, permitiendo
  retomar la propuesta original de aceptación sin duplicar la decisión. Si el
  navegador perdió la conversación, el `proposal_id` se usa sólo como pista y
  el backend obtiene la conversación después de validar ownership y tenant.
- QA local aislada: contratos de recuperación y frontend **34 PASS**; regresión
  ampliada Support/memoria **136 PASS**. TypeScript, ESLint focalizado,
  `compileall` y `git diff --check` pasan. No se usaron servicios activos,
  PostgreSQL QA, credenciales ni proveedor real.
- Pendiente: repetir el escenario manual afectado después de actualizar el
  proceso QA; la propuesta real no fue leída ni resuelta por estas pruebas.

## MEM-092 — correctivo de consentimiento ambiguo — 2026-11-16

- Una clasificación conservadora desconocida ya no conserva silenciosamente la
  propuesta: atraviesa `ConsentCoordinator`, registra `ambiguous_discard` y no
  crea memoria. El idempotency key determinista mantiene un solo registro ante
  reintentos. Un `topic_change` estructurado conserva el procesamiento Support;
  si el proveedor falla, prevalece el descarte fail-closed y la consulta puede
  reenviarse después.
- `FourGeeksStructuredConsentClassifier` selecciona ahora un JSON Schema cerrado
  por categoría. Todos los campos raíz son obligatorios; edición y pregunta
  adicional usan listas acotadas de cero o un elemento, sin `dict[str, Any]`,
  `anyOf`, defaults ni objetos abiertos. El contenido editado continúa pasando
  por la política y requiere una segunda confirmación.
- El fallback registra únicamente diagnósticos sanitizados: timeout,
  HTTP/esquema, respuesta inválida, validación, proveedor ausente o error
  inesperado. No registra texto, propuesta, prompt, payload ni credenciales.
- El límite de clasificación permanece en 6 segundos: la incidencia observada
  no mostró timeout y este correctivo no altera latencia sin evidencia. Queda
  pendiente validar el esquema contra FourGeeks real durante la aceptación.
- QA aislada sin proveedor ni PostgreSQL QA: consentimiento/Support **46 PASS**;
  regresión ampliada memoria, Support, grafo y frontend **146 PASS**;
  `compileall` y validación estructural de los tres schemas PASS.

## MEM-092 — cierre técnico y preparación de entrega — 2026-11-16

- Esta sección reemplaza, para el estado actual, los pendientes históricos de
  las secciones MEM-092 anteriores; sus cifras se conservan como evidencia de
  los gates ejecutados en cada fecha.
- **Veredicto aceptado:** `APROBADO CON OBSERVACIONES`. No se declara
  certificación académica ni despliegue productivo.
- La aceptación real completó el ciclo A de aprobación de cobertura SEUR, con
  decisión auditable, memoria activa y recuperación en otra conversación; el
  ciclo B de rechazo DHL, con auditoría y ninguna memoria; y la edición de una
  preferencia B2B, con propuesta original `edited`, sustituta `pending` y cero
  activaciones automáticas.
- El JSON Schema cerrado del evaluador de memoria fue aceptado en vivo por
  FourGeeks y produjo la propuesta real. El esquema corregido del clasificador
  conversacional y la transición `ambiguous_discard` pasan QA determinista, pero
  su repetición E2E posterior al correctivo continúa pendiente por autenticación
  Keycloak. La propuesta sintética usada para ese escenario permaneció `pending`,
  sin decisión y sin memoria; no debe contarse como PASS.
- Cierre automatizado: **326 PASS en 35.89 s** para memoria, consentimiento,
  Support, LangGraph, RAG, MCP y contratos frontend; **11 PASS en 2.52 s** contra
  PostgreSQL 16 fresh, loopback y `tmpfs`; **38 PASS en 4.06 s** para OIDC. Total:
  **375 pruebas aprobadas**. ESLint focalizado, TypeScript `--noEmit`,
  `compileall` y `git diff --check` también pasaron.
- Los metadatos de PostgreSQL QA confirmaron una propuesta aprobada con su única
  memoria activa, una propuesta rechazada sin memoria, una propuesta editada y
  su reemplazo pendiente. No había memorias activas sin decisión aprobatoria ni
  propuestas aprobadas sin memoria activa.
- Cleanup dispone del invocador operacional
  `python -m scripts.agent_memory_cleanup --execute`. La periodicidad debe
  configurarse en el mecanismo de mantenimiento existente, inyectando
  `DATABASE_URL` mediante secretos, preservando el código de salida y evitando
  solapamientos. Se recomienda ejecución horaria si el retraso posterior al TTL
  de 24 horas debe limitarse aproximadamente a una hora; no se añadió scheduler.
- Pendientes de entrega: revalidación E2E post-correctivo de ambigüedad cuando se
  retome Keycloak, configuración operativa de cleanup, migración/despliegue,
  revisión selectiva del worktree, commit, PR y revisión humana final.

## SEC-114 — Harness y guardrails — 2026-11-16

- **Veredicto técnico:** `APROBADO CON OBSERVACIONES`; pendiente de revisión
  humana, operaciones Git y aceptación manual. No se declara despliegue ni
  certificación productiva.
- Support aplica guardrails de entrada antes de consentimiento/RAG/MCP/memoria,
  conserva la precedencia e invariantes MEM-092 y valida fronteras externas y
  la respuesta HTTP completa antes de serializar. Los fallos usan respuestas
  seguras compatibles y no activan el evaluador de memoria.
- Se añadió el lookup real tenant-aware por `StockExit.tracking_number`, filtrado
  en SQL mediante `CommercialOrder.client_id`, y el sexto tool MCP
  `get_authorized_tracking`. Sólo se afirma `recorded_dispatch`; no se aceptan
  tenants desde el modelo ni se exponen ubicación, entrega o ETA.
- La jurisdicción permanece como control con contexto autoritativo sintético:
  una solicitud ES no sustituye `applicable_country=US`; sin procedencia
  confiable el agente se abstiene. No se añadieron campos persistentes ni
  políticas comerciales.
- PostgreSQL 16 real en loopback/`tmpfs`: **2 PASS**, incluyendo noninterference
  y flujo servicio → adaptador MCP → grafo. Contenedor detenido y eliminado.
  MCP aislado: **65 PASS, 1 SKIP**. Regresión raíz: **1263 PASS, 41 SKIP,
  1 fallo de baseline** en la invalidación de caché con bind no
  weak-referenceable, idéntico en `HEAD` y no modificado por SEC-114.
- Observabilidad registra sólo metadatos cerrados y UUID de correlación. El
  comando de sesión controlada mostró `blocked=2`, `redirected=1`.
- Pendientes honestos: proveedor real, E2E OAuth/manual, E2E histórico de
  ambigüedad MEM-092, TypeScript sin dependencias instaladas y decisión separada
  sobre la deuda de caché. Detalle: `docs/sec-114-agent-harness-guardrails.md`.

## SEC-114 — correctivos Gate 6.2 — 2026-11-16

- Los guardrails contextuales bloquean solicitudes y respuestas que combinan
  intención de divulgación con tarifas/condiciones negociadas o detalles
  físicos internos. Se preservan tarifas estándar documentadas, condiciones
  generales, tarifas preferenciales sujetas a Miguel Torres, LA/ZGZ, cobertura
  y SLA; no se añadieron bloqueos globales de palabras.
- La frontera MCP reduce respuestas `not_found`/`unavailable` del lookup de
  tracking antes de exponerlas: el identificador ecoado por la API no cruza el
  tool. Payloads con campos inesperados o datos de pedido en un fallo continúan
  rechazándose como deriva contractual.
- E2E real exclusivo SEC-114: **1 PASS en 63.09 s**. Dos identidades PKCE,
  Keycloak 26.8.2, MCP real, API, PostgreSQL 16 `tmpfs` y agente Support probaron
  denegación de `45821` para A, lectura positiva de B, tracking compartido sin
  interferencia, scopes y proyección `recorded_dispatch`. Puertos/contenedores
  quedaron limpios; evidencia sanitizada y hashes están fuera del repositorio.
- Verificación focal: **224 PASS, 2 SKIP** (los skips PostgreSQL opt-in quedaron
  cubiertos por el E2E real); MCP completo: **66 PASS**. Regresión raíz:
  **1282 PASS, 41 SKIP, 1 FAIL**. El único fallo sigue siendo
  `test_invalidate_products_preserves_orders`; `cache.py` y `test_cache.py`
  conservan exactamente los hashes del baseline `3fad3ca` y SEC-114 no usa esa
  caché para tracking.
- Frontend: `npm ci --ignore-scripts` desde el lockfile y TypeScript
  `--noEmit --incremental false` PASS. Los hashes de ambos manifiestos quedaron
  sin cambios. npm reportó 7 vulnerabilidades moderadas y 4 altas heredadas; no
  se ejecutó `audit fix`.
- FourGeeks real permanece **PENDIENTE**: no existe `FOUR_GEEKS_API_KEY` legítima
  en este entorno. No se fabricaron credenciales ni se convirtió QA determinista
  en aceptación viva. El E2E histórico de ambigüedad MEM-092 también permanece
  pendiente como prueba viva, aunque sus invariantes deterministas pasan.

## SEC-114 — correctivo de respuestas humanas — 2026-11-16

- Las acciones y códigos de guardrail se conservaron. La decisión interna añade
  únicamente `response_language` (`en`/`es`), que no forma parte de HTTP ni de
  observabilidad.
- Small talk presenta al sistema como asistente de soporte TrackFlow del área CX,
  nunca como Valentina Cruz. El prompt FourGeeks también deja explícito que el
  asistente no es una persona empleada o manager.
- Jailbreak, información comercial confidencial, detalles físicos internos y
  tareas personales tienen rechazos breves, diferenciados, bilingües y no
  reflectivos. Las respuestas redirigen a soporte permitido sin prometer tracking
  de transportista en tiempo real.
- Regresión focal de contenido/HTTP/RAG: **162 PASS**; observabilidad,
  consentimiento y tools: **122 PASS**; verificación final de archivos tocados:
  **124 PASS**. No se repitió E2E ni se modificó infraestructura.

## SEC-114 — Backoffice human-acceptance mode — 2026-11-16

- `mcps/trackflow/qa/run_sec114.ps1` now has an explicit opt-in interactive
  lifecycle (`Validate`, `Start`, `Status`, `Credential`, `Stop`). Omitting
  `-Interactive` preserves the approved automated E2E and cleanup behavior.
- Interactive acceptance reuses the isolated SEC-114 Keycloak, PostgreSQL,
  API, MCP, synthetic seed, ownership checks, and sanitized evidence flow, and
  adds the real Backoffice on loopback port `28080`. It does not rerun the
  tracking E2E.
- Ephemeral local and tenant-A OIDC passwords are kept in a Windows DPAPI blob
  under the session temp directory. The retrieval action copies only the
  selected password to the clipboard and never prints it. A pre-existing
  session blocks a second start and credential rotation.
- The stop action captures content-free guardrail counters/readiness and hashes
  sanitized human evidence before verifying and stopping only exact resources
  owned by that session. MEM-092 ports and historical services are untouched.
- Keycloak QA retains its previous callbacks and adds only the isolated
  `http://127.0.0.1:28080/backoffice/support` PKCE callback/origin.
- Gate 6.4.1 makes the runner compatible with Windows PowerShell 5.1 by loading
  `System.Security` explicitly when needed and using a disposable
  `RandomNumberGenerator.Create()` instance with caller-provided bytes. RNG and
  DPAPI now run as non-persistent preflight probes before interactive startup.
- The corrected preflight passed under Windows PowerShell `5.1.26100.9711`
  (Desktop, CLR `4.0.30319.42000`), including RNG and DPAPI round-trip. It left
  no SEC-114 session state, listeners or containers. The two failed starts left
  only the already designated empty acceptance-evidence directory; historical
  processes on `18083`, `18084` and MEM-092 `18184` were inspected but untouched.
- Gate 6.4.2 replaces direct listener-PID/command matching with bounded process
  ancestry to the exact recorded launcher, plus loopback, command marker,
  session-time and creation-order checks. The existing Next.js chain was proven
  as `57784 -> 59760 -> 86268 -> 101116 -> 99664` where `99664` is the recorded
  launcher. Recovered `Status` reported all five services ready, 5 Support
  requests and 0 MCP POSTs. Deterministic coverage accepts a legitimate child
  tree and rejects an identically marked listener outside the recorded tree.
- Gate 6.4.4 corrects the acceptance stock expectation to `6`, reads MCP
  transport access lines from stdout, and separates transport POSTs, typed
  business-tool events, and unclassified legacy events. Guardrail INFO logging
  now emits only guard/action/category/reason; MCP typed events emit only
  tool/result/duration/downstream status. Neither includes prompts, responses,
  tokens, credentials, client IDs or correlation IDs. Historical absent events
  remain unrecoverable and are not inferred.

## SEC-114 — aceptación humana final — 2026-11-16

- Backoffice A–F: PASS funcional. Producto sintético `91001`: `6` unidades.
- Una segunda sesión con procesos nuevos confirmó bloqueos `security` y
  `content`, redirecciones `content` y `structural`, y el evento empresarial
  tipado `get_inventory_product.success = 1`; transporte MCP y tools permanecen
  separados.
- Ambas sesiones SEC-114 cerraron sus procesos y contenedores exclusivos. Los
  puertos quedaron libres y la evidencia externa verificó sus hashes SHA-256.
- El prompt SEC-114 no fue revalidado contra FourGeeks real. La credencial y el
  gate live posteriores pertenecen a MEM-092 y no acreditan este hito.

## Hito 9 / Parte 1 — Gate 3 técnico — 2026-11-17

- Se implementó la recepción RFP admin-only en el FastAPI y Backoffice
  existentes: upload durable, ticket consultable, polling y separación entre
  estado empresarial y estado técnico del job.
- El grafo canónico vive únicamente en `data/pipelines/rfp_intake/`: clasifica
  Markdown, extrae metadata, despacha workers concurrentes por `Send`, valida
  el fan-in, sintetiza y produce el handoff `rfp-intake-v1`. Los responsables
  permanecen cerrados a Ana Whitfield, Carlos Vega y Sofía Ramos.
- PostgreSQL usa DDL explícito y metadata aislada. Ticket/job/outbox, fencing,
  lease/heartbeat, tres intentos, backoff 30/120 y relay RFP son independientes
  de reporting. El PDF y Markdown comparten un volumen entre API/worker bajo
  UID 10002, sin permisos 0777 ni borrado automático.
- PDF→Markdown ocurre antes de cualquier LLM en un subprocess terminable, con
  límite de páginas, timeout, hash y métricas de páginas/texto/legibilidad. Los
  tres fixtures con hashes aprobados convierten localmente; no se alteraron.
- QA focal Python: **79 PASS**. TypeScript y ESLint focales: **PASS**. Lock,
  compileall, `docker compose config --no-env-resolution`, builds API/worker,
  imports en contenedor, conversión en worker y volumen compartido: **PASS**.
  PostgreSQL 16 real en loopback/`tmpfs`: DDL, descarte transaccional, complete
  transaccional con handoff y outbox/retry: **PASS**. Todos los recursos Docker
  exclusivos de QA fueron retirados.
- Pendientes honestos: envío de los tres PDFs al proveedor FourGeeks quedó
  **BLOCKED** por requerir autorización explícita del payload externo; por ello
  ModaViva/Luna/RapidShip no tienen aceptación viva. Redis/Celery E2E y la
  aceptación manual posterior desde Backoffice también siguen pendientes. La
  política productiva de acceso de Ventas no se amplió más allá de admin QA.

## Hito 9 / Parte 1 — cierre E2E aislado — 2026-11-17

- Un E2E opt-in ejecutó API HTTP, PostgreSQL 16 `tmpfs`, Redis, relay, Celery,
  conversión PDF, grafo LangGraph y persistencia reales sobre una red Docker
  interna. Solo la generación estructurada fue sustituida por un stub HTTP
  local determinista; no se leyeron credenciales ni se llamó a FourGeeks.
- Resultado reproducido independientemente: **1 PASS en 113.73 s**. ModaViva
  terminó ES/EUR con `warehouse + reverse` y `lastmile=false` después de un
  fallo estructurado recuperable (`attempt_count=2`); Luna terminó US/USD con
  `warehouse + lastmile`, 5.000 pedidos/mes y onboarding separado de deadline;
  RapidShip quedó `descartado/vendor_pitch`, sin workers, secciones ni handoff.
- PostgreSQL confirmó 3 tickets/jobs succeeded, 2 análisis completos, 1
  descarte, 3 metadata, 2 handoffs, 4 secciones, 4 intentos y 4 outboxes
  publicados. Reentregar un `job_id` terminal dejó el snapshot sin cambios.
  Cada directorio conservó `source.pdf` y `source.md` con UID:GID 10002:10002.
- El E2E descubrió y corrigió dos bloqueos reales: la API normaliza ahora
  `postgresql://` al driver `psycopg2` ya instalado, y Next.js proxyfica
  `/rfps/:path*` hacia el backend. Regresión focal final: **81 PASS, 1 SKIP**;
  build Backoffice, TypeScript y ESLint PASS.
- Todos los contenedores, redes y volúmenes `trackflow-rfp-e2e-*` quedaron
  eliminados. Las imágenes locales de QA no se publicaron. Playwright se omitió
  porque no está instalado y añadirlo exigiría cambios de dependencias sin
  cobertura proporcional; la aceptación manual Backoffice sigue reservada a
  Usuario + ChatGPT. La prueba no certifica el proveedor LLM externo.

## Hito 9 / Parte 1 — aceptación manual y cierre pre-commit — 2026-11-17

- El Gate 3 E2E aislado quedó aprobado con PostgreSQL, Redis, Celery, relay,
  FastAPI y LangGraph reales; únicamente la generación LLM utilizó un stub HTTP
  local determinista.
- En Gate 4, Usuario + ChatGPT validaron desde el Backoffice auténtico:
  ModaViva `PASS` (`warehouse + reverse`, ES/EUR), Luna Cosmetics `PASS`
  (`warehouse + lastmile`, US/USD, 5.000 pedidos/mes) y RapidShip `PASS`
  (`descartado/vendor_pitch`). Un documento educativo de verbos también quedó
  `PASS` como `descartado/not_a_request` después de ampliar exclusivamente el
  stub para documentos ajenos a RFPs.
- El listado RFP normaliza ahora `RFPSummary.departments_needed` de `NULL` a
  `[]`; la regresión cubre tickets aceptados aún sin metadata y descartados.
- Gate 5B retiró el Markdown completo de los payloads `Send`: cada worker recibe
  solo `department_id`, metadata validada, el extracto pertinente y el conteo
  de líneas necesario para validar citas. El fan-out concurrente, fan-in,
  synthesizer y handoff permanecen cubiertos por pruebas focales.
- Un smoke Linux/Docker aislado ejecutado como UID:GID `10002:10002` sobre el
  montaje de desarrollo comprobó inicialización, escritura y lectura TinyDB,
  login sintético y `/auth/me` con HTTP 200. No fue necesario modificar
  Compose; los recursos efímeros exclusivos del smoke se retiraron.
- FourGeeks real no fue utilizado ni certificado. La aceptación manual acredita
  el flujo integrado con stub local, y el RBAC RFP permanece limitado a
  `admin` durante QA; la política productiva para Ventas sigue pendiente.
- Los hallazgos materiales de Gate 5 sobre aislamiento de workers y continuidad
  de Memory Bank quedaron corregidos. La portabilidad de permisos de bind mounts
  fuera del entorno Docker validado continúa dependiendo de ownership/ACL del
  host y debe verificarse en cada destino Linux.

## Hito 9 / Parte 2 — Gate 3, Fases 1 y 2 — 2026-11-18

- Se implementaron contratos cerrados para borradores departamentales y globales,
  evaluaciones de legibilidad/relevancia/compliance, previews fail-closed y el
  handoff `rfp-response-v1`. Un fallo técnico conserva diagnóstico operacional,
  pero no fabrica `EvaluationResult` ni handoff consumible por Parte 3.
- El compliance departamental y global permanece separado. SLA, moneda y tabla
  de descuentos global usan únicamente evidencia documentada o pendientes
  explícitos; los valores comerciales desconocidos bloquean aprobación sin ser
  inventados. Las iteraciones departamentales y globales cuentan generaciones
  reales y tienen límite contractual de tres.
- La persistencia usa una migración forward explícita, metadata RFP aislada,
  hashes canónicos, transacciones caller-owned, CAS, leases, fencing e
  idempotencia por identidad+contenido para reconciliar COMMIT incierto. Los
  fallos técnicos se almacenan fuera de evaluaciones y no pueden producir un
  `rfp-response-v1` incompleto.
- QA focal y regresión de Parte 1: **51 PASS**. PostgreSQL 16 real, efímero,
  loopback y `tmpfs`: **2 PASS**, incluida migración repetible, ruta positiva,
  fencing obsoleto, reconciliación idempotente y fallo técnico sin handoff. Los
  contenedores exclusivos de QA fueron eliminados.
- La wheel construida offline contiene `data.pipelines.rfp_response` y
  `services.rfp_response`, y conserva los paquetes verificados de Parte 1, API,
  RAG y pipeline. Fase 3, API, relay, Celery, grafo integrado, Backoffice y toda
  aprobación humana continúan pendientes de autorización y revisión posterior.

## Hito 9 / Parte 2 — Gate 3, Fase 3 — 2026-11-18

- Se implementaron generadores departamentales para `warehouse`, `lastmile` y
  `reverse`, además del bloque global de moneda, SLA y descuentos. Los precios
  no documentados permanecen pendientes: no se reutilizan tarifas demo ni se
  inventan precios de última milla, devoluciones o porcentajes de descuento.
- Los cuatro documentos canónicos se consumen mediante fragmentos semánticos
  mínimos con hash SHA-256. El proveedor determinista de QA recibe únicamente
  claims aprobados y códigos pendientes; no acredita al proveedor FourGeeks.
- La evaluación integra legibilidad bilingüe, pertinencia, compliance por
  sección y global, feedback accionable y reducción determinista. El preview
  falla cerrado si el contenido no puede vincularse al conjunto de claims
  permitido. Un transportista propio no recibe tarifa ni compromiso TrackFlow.
- QA focal más regresión de Parte 1/Fases 1–2: **69 PASS, 1 SKIP**. PostgreSQL
  16 real, efímero, exclusivo
  de loopback y `tmpfs`: **3 PASS**, incluida una carrera concurrente que
  confirma un solo run idempotente y un único claim. El contenedor de QA fue
  eliminado al terminar.
- Siguen fuera de alcance y pendientes de autorización: grafo LangGraph
  integrado, Celery/relay, endpoints FastAPI, Backoffice, FourGeeks real y las
  funciones de aprobación humana de Parte 3.

## Hito 9 / Parte 2 — correctivo de auditoría Gate 3 — 2026-11-18

- Evaluación y persistencia comparten una única serialización y función de hash
  canónicas. La prueba PostgreSQL genera, evalúa y persiste el mismo borrador y
  rechaza cualquier divergencia del hash durable.
- `rfp-response-start-v1` incorpora el resumen comercial y todos los aspectos
  documentados de las secciones de intake. La pertinencia conserva por aspecto
  `addressed`, `missing` o `contradicted`, y el resultado persistido incluye
  feedback accionable e identificadores de aspectos pendientes.
- Compliance y preview cubren promesas de devolución inferiores a 48 horas,
  SLA de última milla gestionada, moneda por país, presencia de tabla de
  descuentos, valores confidenciales y evidencia no correspondiente. Los
  términos materiales se comparan contra una generación determinista autorizada
  y el preview falla cerrado antes de exponer contenido.
- `start_response_run` valida el handoff completo, metadata, servicios,
  secciones, responsables y referencias antes de crear el run. El handoff final
  reconcilia cada sección con borrador, preview y evaluación persistidos.
- La integración PostgreSQL parte de un fixture congelado del DDL original de
  Parte 1, conserva ticket/job/outbox históricos y aplica dos veces la migración
  forward. Resultado observado: **5 PASS** en PostgreSQL 16 aislado. QA focal:
  **52 PASS**. Regresión Parte 1 y Fases 1–3: **81 PASS, 6 SKIP**; los skips son
  los E2E/PostgreSQL opt-in, cubiertos por las ejecuciones aisladas pertinentes.
- Fase 4 continúa sin iniciarse: no se añadieron grafo integrado, Celery/relay,
  FastAPI, Backoffice, FourGeeks real ni aprobación humana de Parte 3.

## Hito 9 / Parte 2 — cierre focal previo a Fase 4 — 2026-11-18

- Los requisitos del cliente y las respuestas de TrackFlow quedan separados en
  el contrato departamental. La pertinencia exige una respuesta distinta,
  presente y respaldada por evidencia oficial para cada aspecto; copiar una
  solicitud o usar solo el nombre del departamento no acredita cobertura.
- La regla de devoluciones inferiores a 48 horas reconoce horas y días en
  inglés y español dentro de cualquier sección, pero únicamente cuando la
  duración aparece en contexto de devolución, reembolso o logística inversa.
- Los aspectos del intake con tarifas negociadas o confidenciales se retiran
  del contexto mostrable al proveedor y no autorizan claims. La inspección de
  preview bloquea una tarifa negociada de un transportista aunque exista una
  referencia válida al handoff.
- Una moneda deja de aprobarse automáticamente cuando el país del cliente y el
  ámbito operativo no determinan una única regla oficial. El caso ES / US / USD
  produce un bloqueo de completitud y revisión humana, no una corrección
  inventada ni una aprobación silenciosa.
- QA sobre el estado final: **63 PASS** focales; regresión de Parte 1 y Fases
  1–3: **92 PASS, 6 SKIP** opt-in; PostgreSQL 16 aislado, loopback y `tmpfs`:
  **5 PASS**. El contenedor efímero fue eliminado. Fase 4 permanece sin iniciar.

## Hito 9 / Parte 2 — Gate 3, Fase 4 — 2026-11-18

- `RFPResponseGraph` integra el handoff `rfp-intake-v1` persistido mediante un
  fan-out LangGraph por departamento y fan-in determinista. Cada rama conserva
  su progreso; tres evaluadores se ejecutan en paralelo y sus resultados se
  reducen sin compartir sesiones PostgreSQL.
- Borradores, previews, evaluaciones y fallos se persisten bajo el claim y fence
  del response run. Una recuperación reutiliza el último borrador sin evaluar y
  una evaluación global terminal sin handoff; solo una generación real aumenta
  el contador empresarial, con límite exacto de tres.
- Los fallos técnicos de evaluador reintentan el mismo borrador y conservan
  diagnóstico. El cierre técnico del run ocurre después del fan-in, permitiendo
  que otras ramas guarden resultados válidos; nunca se fabrica evaluación ni
  `rfp-response-v1` en esa ruta.
- El relay RFP existente usa routing cerrado por `job_kind`: `intake` mantiene
  `trackflow.process_rfp` y `response_generation` usa
  `trackflow.process_rfp_response`. Ambos comparten outbox, relay, broker y
  backend. La aceptación concurrente produce un único response run, job y
  outbox; redelivery reconcilia el handoff sin duplicar artefactos.
- La legibilidad inglesa de respuesta usa fórmulas deterministas locales y no
  depende del recurso NLTK `punkt_tab`. El feedback regenerable se conserva en
  el contrato de cada borrador y se entrega al generador correspondiente.
- QA final observado: regresión pertinente **134 PASS, 9 SKIP** opt-in; las
  ocho pruebas PostgreSQL omitidas pasaron después en PostgreSQL 16 nuevo,
  `healthy`, loopback y `tmpfs`: **8 PASS**. Los contenedores efímeros fueron
  eliminados. Un smoke vivo adicional con Redis 7.4.1, worker Celery `solo`,
  relay compartido, PostgreSQL y LangGraph reales terminó `succeeded` /
  `ready_for_part3` con 2 borradores, 2 evaluaciones y 1 handoff. La redelivery
  del mismo task id mantuvo exactamente 2/2/1 artefactos y `attempt_count=1`.
  Worker y contenedores se retiraron. FastAPI, Backoffice, aprobación humana,
  documento final y FourGeeks real permanecen fuera de alcance y sin iniciar.

## Hito 9 / Parte 2 — Gate 3, Fase 5 — 2026-11-18

- FastAPI amplía las rutas administrativas RFP existentes para aceptar de forma
  idempotente un response run persistente y consultar el run actual o uno
  concreto. La solicitud HTTP solo crea run/job/outbox; la generación continúa
  exclusivamente mediante el relay y Celery compartidos.
- La proyección HTTP valida contratos e integridad hash antes de responder. No
  expone payloads de borrador ni referencias diagnósticas, y elimina contenido
  de previews `blocked`, `indeterminate` o `technical_error` aunque una fila
  inconsistente contuviera texto.
- El Backoffice RFP existente permite iniciar la generación en tickets
  `analisis_completo`, consulta progreso e iteraciones por departamento, muestra
  feedback y pendientes con responsable, y renderiza solamente previews
  `passed`. Distingue aprobación automática, revisión humana y fallo operativo;
  no incorpora acciones de aprobación ni documento final de Parte 3.
- QA focal de API, seguridad, OpenAPI y regresión RFP: **111 PASS**. PostgreSQL 16
  efímero verificado como `healthy`, loopback y `tmpfs`: **8 PASS**, incluida
  proyección recuperable del handoff y concurrencia/idempotencia. ESLint de los
  tres archivos Backoffice afectados y `tsc --noEmit`: PASS.
- El build Next compiló el código en el primer intento, pero la validación
  completa no pudo cerrarse porque el entorno de dependencias existente carece
  de `@vercel/turbopack/postcss`. El lint completo conserva tres errores y un
  warning preexistentes fuera de RFP. La regresión global final ejecutó
  **1415 PASS, 50 SKIP, 1 DESELECTED**; el único test excluido fue el fallo
  preexistente y reproducible en
  `test_invalidate_products_preserves_orders` ajeno a RFP. No se modificaron
  dependencias ni cache de inventario para ocultarlos; los artefactos temporales
  y el contenedor de QA fueron retirados.

## Hito 9 / Parte 2 — Gate 3, Fase 6 — 2026-11-18

- El E2E aislado ejecutó FastAPI, outbox/relay, Redis, Celery, LangGraph y
  PostgreSQL 16 reales con proveedores deterministas. Cuatro solicitudes HTTP
  fueron consumidas concurrentemente por cuatro hilos Celery y terminaron en
  menos de un segundo por task; persistieron 4 runs, 4 jobs/outbox publicados,
  11 borradores, 11 evaluaciones y 4 handoffs.
  La redelivery de un job terminal mantuvo 2 borradores, 2 evaluaciones,
  `attempt_count=1` y un único handoff.
- Los Dockerfiles de API y worker ahora empaquetan el pipeline
  `data/pipelines/rfp_response`; el worker incluye además los cuatro documentos
  oficiales requeridos por los generadores. Las imágenes QA se construyeron y
  ejecutaron correctamente sin publicar artefactos ni usar FourGeeks real.
- Una instalación limpia con `npm ci` resolvió la ausencia de
  `@vercel/turbopack/postcss`: era un `node_modules` local incompleto, no un
  defecto de manifiesto. `next build`, ESLint focal y `tsc --noEmit` pasaron. El
  Backoffice real permitió login administrativo, inicio asíncrono, progreso,
  iteraciones y previews seguros, sin controles de aprobación humana.
- QA final: E2E vivo **2 PASS**; PostgreSQL aislado **8 PASS**; regresión RFP
  focal **87 PASS**; suite completa **1416 PASS, 52 SKIP, 1 DESELECTED**. El test
  excluido `test_invalidate_products_preserves_orders` falla de forma
  reproducible y sus dos archivos son idénticos al baseline `2f3076e`.
- El escenario de transportista propio está cubierto de forma determinista en
  el grafo, pero no pudo atravesar HTTP porque `rfp-intake-v1` no transporta ese
  dato; no se amplió el contrato durante Fase 6. La aceptación E2E integral
  permanece pendiente de decisión sobre esa limitación y de la revisión humana
  de estos resultados.

## Hito 9 / Parte 2 — Gate 3, Fase 6.1 — 2026-11-19

- El POST administrativo de response generation acepta el modelo triestado
  `unknown`, `trackflow_managed` o `customer_managed`; omitir el cuerpo conserva
  `unknown` por compatibilidad. El Backoffice exige una selección explícita y
  no presenta la confirmación como extracción de `rfp-intake-v1`.
- Cada run conserva un snapshot inmutable del modelo, actor, procedencia y fecha.
  La migración PostgreSQL es forward y repetible: los runs previos reciben
  `unknown` / `legacy_migration` sin perder artefactos. Repetir el mismo contexto
  devuelve el run existente y un modelo incompatible produce conflicto 409.
- La decisión se transforma al campo triestado existente
  `customer_managed_carrier`. Su evidencia usa el dominio separado
  `admin-confirmation` y un hash del snapshot; los campos internos de auditoría
  no se exponen en la proyección HTTP.
- El E2E real aislado Backoffice/API/relay/Celery/LangGraph/PostgreSQL confirmó:
  `customer_managed` sin precio ni SLA atribuible a TrackFlow, `unknown` con
  `CARRIER_MODEL_REQUIRED`, y `trackflow_managed` con SLA aplicable y
  `LAST_MILE_RATE_REQUIRED`. Una redelivery no duplicó artefactos.
- QA final: E2E Fase 6.1 **1 PASS**; regresión focal **71 PASS**; PostgreSQL 16
  aislado **12 PASS**; suite completa **1418 PASS, 57 SKIP, 1 DESELECTED**.
  El escenario técnico terminal controlado conservó diagnóstico y produjo cero
  handoffs/evaluaciones; fue una inyección en el límite del store, no una caída
  natural del evaluador HTTP. Durante polling concurrente se observó un 503
  fail-closed transitorio y la lectura posterior fue consistente.
- Riesgo pendiente: el proveedor determinista genera una frase que no satisface
  legibilidad; `customer_managed` termina tras tres borradores en
  `needs_human_review` aunque no tenga bloqueo comercial. No se debilitó esa
  evaluación ni se declaró aprobación automática.

## Hito 9 / Parte 2 — Gate de aceptación manual, correctivo final — 2026-11-19

- El preview global usa una allowlist construida en backend: moneda, SLA,
  exclusiones, descuentos y pendientes. La proyección HTTP reconstruye esa
  vista incluso para previews antiguos, mientras UUID, hashes, evidencia y
  payload completo permanecen únicamente en PostgreSQL.
- `customer_managed_carrier=None` ya no activa el requisito de tarifa. Unknown
  conserva `CARRIER_MODEL_REQUIRED` con Carlos Vega; TrackFlow-managed requiere
  `LAST_MILE_RATE_REQUIRED`; customer-managed mantiene tarifa y SLA no
  aplicables.
- Backoffice traduce estados de job, generación, evaluación departamental y
  global; separa obligaciones comerciales de recomendaciones de calidad y
  legibilidad, renderiza términos globales en campos y tabla, y contiene el
  ancho en layouts estrechos.
- El proveedor determinista consume el feedback de legibilidad: simplifica
  afirmaciones sustentadas sin cambiar umbrales. El E2E correctivo
  customer-managed pasó en la segunda generación; unknown terminó en revisión
  humana con un único pendiente de modelo.
- QA: **57 PASS** focales iniciales, **91 PASS** de regresión RFP, **12 PASS**
  PostgreSQL aislado, ESLint/TypeScript/build Next PASS, y suite completa
  **1420 PASS, 57 SKIP, 1 DESELECTED**. La comprobación visual confirmó preview
  legible, estados españoles, separación de feedback y ausencia de overflow.

## Hito 9 / Parte 2 — Gate LLM, Etapa A — 2026-11-20

- El runtime de response generation admite modos explícitos `deterministic` y
  `fourgeeks`. El segundo reutiliza `FourGeeksHTTPClient` solo para generación
  estructurada, sin Qdrant ni embeddings, y falla técnicamente ante
  configuración ausente, error upstream, presupuesto agotado o salida inválida;
  no existe fallback silencioso.
- El adaptador FourGeeks genera segmentos departamentales para Warehouse, Last
  Mile y Reverse con JSON Schema cerrado. Cada segmento cita claims autorizados
  y una barrera determinista valida cobertura léxica, cifras, unidades y
  polaridad antes de convertir la redacción en un claim trazable. Precios,
  términos globales, compliance, legibilidad y safety gate siguen en backend.
- La pertinencia LLM produce `addressed`, `missing` o `contradicted` por
  `aspect_id`, con claim IDs verificables y feedback accionable. El grafo pasa
  ese feedback a la siguiente generación y conserva el máximo de tres
  borradores reales; reintentos técnicos no incrementan ese contador.
- Presupuesto, concurrencia, pacing, timeout y tokens de entrada/salida son
  configurables y compartidos thread-safe por run. PostgreSQL persiste solo
  metadatos seguros de invocación bajo fencing, sin prompts ni respuestas.
- QA sin llamadas externas: regresión focal de response **84 PASS**; API/E2E
  **9 PASS, 2 SKIP**; PostgreSQL 16 exclusivo, loopback y `tmpfs` **13 PASS**.
  El suite principal terminó **1444 PASS, 58 SKIP, 1 FAIL**; el único fallo es
  `test_invalidate_products_preserves_orders`, ya reproducido en el baseline.
  La recolección del suite raíz adicional permanece bloqueada por dependencias
  opcionales MCP ausentes (`trackflow_mcp`, `mcpauth`). No se llamó al gateway
  real ni se consumió API; el contenedor QA fue eliminado al finalizar.

## Hito 9 / Parte 2 — Gate LLM, B0/B0.1 — 2026-11-20

- B0 realizó una solicitud real de generación al Gateway FourGeeks y se detuvo
  ante `validation_error` HTTP 200, sin ejecutar pertinencia. B0.1 añadió una
  observación process-local que conserva solo estructura y tipos de la
  respuesta, nunca prompts, contenido, claves o cabeceras.
- La única llamada diagnóstica adicional confirmó JSON OpenAI-compatible:
  `choices` contenía un elemento con `message` objeto, pero `content` era null,
  `finish_reason=length` y había campo de razonamiento. El proveedor reportó
  1050 tokens de prompt, 600 de completion y 1650 totales; el límite completo
  de salida se consumió antes de producir contenido final.
- Acumulado B0: exactamente **2 solicitudes reales**, sin reintentos,
  embeddings, fallback ni evaluación semántica. JSON Schema no queda validado
  end-to-end porque no existió contenido estructurado. La corrección mínima
  propuesta es autorizar un límite de completion mayor manteniendo modelo y
  schema, o confirmar documentalmente un parámetro soportado para desactivar
  razonamiento; ninguna variante fue ejecutada. Regresión offline del cliente y
  adaptador: **91 PASS**.

## Hito 9 / Parte 2 — Gate LLM, B0.2 — 2026-11-20

- Una verificación offline confirmó que el cliente transmite exactamente
  `max_tokens=3000` manteniendo JSON Schema estricto. La única generación real
  Warehouse autorizada terminó HTTP 200 con `finish_reason=stop` y contenido
  textual no vacío.
- El proveedor reportó 560 tokens de prompt, 1413 de completion y 1973 totales.
  La respuesta validó JSON Schema y Pydantic; produjo tres claims sustentados,
  tres términos comerciales, cero pendientes y superó grounding y safe preview
  como `verified_safe`, sin reason codes.
- Se usó el modelo aprobado `online/openrouter/deepseek/deepseek-v4-flash`, una
  sola solicitud, concurrencia 1, cero reintentos, embeddings, fallback o
  evaluación semántica. B0.2 se detuvo tras la generación conforme al gate.

## Hito 9 / Parte 2 — Gate LLM, B0.3 — 2026-11-20

- El borrador efímero de B0.2 no estaba persistido, por lo que se reconstruyó
  explícitamente un fixture Warehouse sintético equivalente mediante claims y
  evidencias oficiales; su preview previo fue `verified_safe`. No se efectuó
  otra generación.
- La única evaluación semántica real autorizada terminó HTTP 200 con
  `finish_reason=stop`, JSON Schema y Pydantic válidos. El aspecto
  `warehouse:smoke:1-2` fue clasificado `addressed`, score 100, con tres
  referencias a claims realmente visibles y explicación presente.
- La validación determinista confirmó que todas las referencias pertenecían al
  borrador autorizado. No hubo feedback porque no existieron aspectos missing
  o contradicted. El proveedor reportó 809 tokens de prompt, 557 de completion
  y 1366 totales; se usó una llamada, concurrencia 1, sin reintentos,
  embeddings, fallback, infraestructura compartida ni E2E B1.

## Hito 9 / Parte 2 — Gate LLM, B1A — 2026-11-20

- Se ejecutó un único E2E real Warehouse en infraestructura Docker exclusiva:
  FastAPI, PostgreSQL/outbox, relay, Redis, Celery, LangGraph, Gateway FourGeeks,
  persistencia y lectura API. PostgreSQL y Redis no publicaron puertos y usaron
  almacenamiento `tmpfs` dentro de la red aislada `trackflow-rfp-b1a-net`.
- El run `3e950458-ffff-4f60-98ae-20be73f09605` consumió exactamente dos
  llamadas reales compartiendo un presupuesto global de seis: generación
  departamental y pertinencia. Ambas terminaron `succeeded` con el modelo
  `online/openrouter/deepseek/deepseek-v4-flash`; se reportaron 2829 tokens en
  total (1395 prompt y 1434 completion) y cero fallos técnicos.
- Warehouse terminó en una generación: legibilidad aprobada (51.4978),
  pertinencia 100 con el aspecto atendido y compliance departamental aprobado.
  El hash evaluado coincide con el hash canónico del borrador. Compliance global,
  grounding y ambos safety gates aprobaron; se persistieron dos borradores, dos
  evaluaciones, cuatro resultados de evaluador, dos previews y un handoff
  `rfp-response-v1` con outcome `ready_for_part3`.
- La lectura autenticada de API devolvió HTTP 200 y únicamente previews con gate
  `passed`; el job Celery/outbox se publicó una vez y terminó `succeeded` en un
  intento. Regresiones offline: **35 PASS** (LLM + Fase 4) y **9 PASS** (API).
- Limitación: el status HTTP exacto del Gateway no queda persistido en la
  telemetría del run; `outcome=succeeded` y los tokens prueban respuesta válida,
  pero no permiten informar el código histórico sin inferencia. La comprobación
  visual del Backoffice quedó bloqueada por el entorno de automatización: el
  navegador integrado no hidrató la app y el control de Edge se detuvo al no
  poder verificar con seguridad la URL activa. No se ejecutó un segundo
  escenario ni se consumieron llamadas LLM adicionales.

## Hito 9 / Parte 2 — Gate LLM, B1B — 2026-11-20

- Se ejecutó un único escenario real Warehouse + Last Mile en infraestructura
  B1B exclusiva, con operación US, USD, volumen mensual de 20 m³ y modelo de
  transportista `trackflow_managed`. El presupuesto global fue 12 llamadas,
  2.500 tokens de entrada, 3.000 de salida y concurrencia máxima 2.
- El grafo realizó cinco llamadas FourGeeks: generación Warehouse exitosa,
  tres intentos técnicos de generación Last Mile rechazados por
  `contract_validation`, y pertinencia Warehouse exitosa. Las dos primeras
  generaciones se solaparon temporalmente, acreditando concurrencia real sin
  sobrescritura. Se consumieron 2.634 tokens de prompt y 2.118 de completion.
- Warehouse terminó `passed` en una generación, con legibilidad 54,8092,
  pertinencia 100, compliance aprobado, hash coincidente y preview seguro con
  las tarifas y condiciones documentadas de Los Ángeles. Last Mile quedó en
  `technical_failure` con cero generaciones empresariales: no se persistió ni
  expuso una salida inválida, y los tres intentos técnicos no incrementaron su
  contador empresarial.
- El run terminó `technical_failure`, el job `failed` con
  `response_technical_failure`, global no se inició y se generaron cero
  handoffs. La rama Warehouse se conservó con su borrador, evaluación y preview.
  La API devolvió HTTP 200, omitió payloads, contenido interno y referencias de
  diagnóstico, y no expuso preview Last Mile.
- El entorno B1B permanece encendido para aceptación manual en loopback; no se
  ejecutó otro escenario ni se modificó Parte 3, Git o infraestructura
  compartida. La contraseña administrativa aleatoria se conserva solo como hash
  en el volumen aislado y se copió al portapapeles local de Windows.

## Hito 9 / Parte 2 — B1B diagnóstico Last Mile — 2026-11-20

- La respuesta FourGeeks original no se persistió, por diseño, y la telemetría
  histórica agrupó Pydantic, referencias, grounding y pendientes bajo
  `contract_validation`; por ello no es posible atribuir honestamente el fallo
  B1B a una ruta concreta retrospectiva. Sí se demostró una brecha contractual:
  Last Mile exigía `LAST_MILE_RATE_REQUIRED` en un validador posterior, mientras
  el JSON Schema enviado solo declaraba un array arbitrario de strings.
- La generación ahora incluye `required_pending_codes` como dato explícito y
  especializa el schema por ejecución con cardinalidad exacta y enum permitido.
  El validador determinista posterior permanece intacto. La tarifa sigue
  pendiente, sin valor ni moneda, y el backend —no el LLM— renderiza el pendiente.
- Las invocaciones inválidas nuevas conservan únicamente ruta, tipo de error,
  tipo de dato recibido y restricción violada. No almacenan valores, respuestas,
  prompts ni reasoning. Pydantic y las validaciones deterministas ya no se
  colapsan en una sola categoría.
- Una violación estructurada determinista se detiene tras un intento técnico;
  timeouts y fallos transitorios mantienen la política de reintentos. Los fallos
  empresariales siguen usando feedback y generaciones reales hasta el máximo de
  tres. Esto evita repetir a temperatura cero llamadas idénticas que no pueden
  corregirse sin cambiar contrato o prompt.
- QA sin tráfico externo: regresión focal **108 PASS**, clasificación de
  reintentos **52 PASS**, regresión RFP completa **127 PASS, 16 SKIP** y
  PostgreSQL 16 aislado **13 PASS**. El run B1B permanece intacto con cinco
  llamadas, un borrador Warehouse y cero handoffs; su infraestructura sigue
  encendida para revisión manual.

## Hito 9 / Parte 2 — Gate LLM B1B.1 Last Mile — 2026-11-20

- El preflight offline del adaptador y grafo pasó **47/47 pruebas**. El proceso
  real cargó `services/rfp_response/llm.py` desde el worktree correctivo actual,
  con modo FourGeeks explícito, presupuesto global de dos llamadas,
  concurrencia uno, salida máxima de 3.000 tokens y sin fallback.
- Se realizaron exactamente dos llamadas reales al modelo
  `online/openrouter/deepseek/deepseek-v4-flash`: generación Last Mile y
  pertinencia del mismo borrador. Ambas devolvieron HTTP 200, `finish_reason`
  `stop`, JSON Schema y Pydantic válidos. El consumo informado fue 1.245 tokens
  de prompt y 1.109 de completion (2.354 total), con latencias HTTP de
  10.237,544 ms y 3.206,782 ms respectivamente.
- La generación produjo exactamente `LAST_MILE_RATE_REQUIRED`; la tarifa quedó
  `pending`, sin valor ni moneda. Grounding determinista y safety aprobaron, el
  preview seguro quedó disponible y no se detectó precio inventado.
- La pertinencia calificó el aspecto sintético como `addressed` con dos claims
  visibles, score 100 y evidencia determinista válida. El hash del borrador no
  cambió y coincide con el hash asociado a la evaluación.
- El éxito fue técnico, no comercial: compliance departamental falló con
  `TF-COMMERCIAL-001`, `blocks_commercial_completeness=true`,
  `requires_human_input=true`, `overall_pass=false` y terminal
  `needs_human_input`. No hubo embeddings, reintentos automáticos ni llamadas
  adicionales, y el entorno B1B histórico no se modificó ni reinició.

## Hito 9 / Parte 2 — B1B.2, aceptación visual y cierre pre-PR — 2026-11-20

- El E2E B1B.2 ejecutó una RFP sintética Warehouse + Last Mile mediante
  FastAPI, outbox, relay, Redis, Celery, LangGraph, PostgreSQL y FourGeeks real.
  Consumó exactamente cuatro llamadas: generación y pertinencia para cada
  departamento, sin fallback ni fallos técnicos. Warehouse terminó `passed` en
  la primera generación; Last Mile terminó `needs_human_input` en la primera
  generación con `LAST_MILE_RATE_REQUIRED`, sin inventar tarifa.
- Compliance global aprobó moneda USD, SLA aplicable del 90 % y la estructura
  documentada de descuentos. El handoff durable `rfp-response-v1` conservó
  ambos borradores y evaluaciones con outcome `needs_human_review`. El job
  técnico terminó `succeeded` en un intento.
- Se corrigió exclusivamente la proyección HTTP/presentación global: el estado
  visible se deriva de `global_compliance.passed`, mientras el outcome
  consolidado permanece separado. Usuario + ChatGPT verificaron visualmente en
  Backoffice Warehouse aprobado, Last Mile con información humana pendiente,
  términos globales aprobados, propuesta en revisión humana y job finalizado.
- La auditoría pre-PR no encontró secretos, `.env` privados, artefactos QA
  crudos ni archivos ajenos a Parte 2. Confirmó que `rfp-response-v1` referencia
  borradores y evaluaciones recuperables. Detectó dos bloqueos de entrega: el
  antiguo techo publicado de salida no reproducía los E2E reales y faltaba un
  runbook estable de migración/operación/pruebas.
- El techo configurable `RFP_RESPONSE_LLM_MAX_OUTPUT_TOKENS` queda alineado en
  código, `.env.example` y documentación a **3000 tokens máximos por solicitud**;
  no representa consumo obligatorio. Se mantiene el presupuesto global, entrada
  máxima, concurrencia, pacing, timeout, modelo, JSON Schema y prohibición de
  fallback. El runbook versionado vive en
  `docs/operations/rfp-response-generation.md`.
- QA final offline: configuración/grafo/API/Backoffice **70 PASS**; regresión
  RFP completa **141 PASS, 16 SKIP** opt-in; suite principal **1470 PASS,
  58 SKIP, 1 FAIL**. El único fallo,
  `test_invalidate_products_preserves_orders`, y sus archivos relevantes son
  idénticos al baseline `2f3076e`. La colección adicional de `mcps/trackflow`
  permanece bloqueada por dependencias opcionales ausentes (`trackflow_mcp` y
  `mcpauth`). Los skips PostgreSQL/E2E requieren infraestructura aislada y no se
  ejecutaron durante este cierre. No se realizaron llamadas FourGeeks nuevas.
