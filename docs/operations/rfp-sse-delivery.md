# Entrega de TrackFlow SSE Parte 1

La aceptación funcional en QA determinista está aprobada por Usuario + ChatGPT. La entrega Git sigue pendiente. El [runbook SSE](rfp-sse.md) conserva la evidencia de aceptación y los UUID de los tres casos.

## Cobertura de la rúbrica disponible

La matriz se basa en los requisitos obligatorios reproducidos en FASE 0.3 y en el encargo integral aprobado. El README original de la actividad y el BRIEFING original de TrackFlow se referencian en ese encargo. La comprobación de esta matriz cubre los criterios facilitados, con cotejo literal pendiente si se adjuntan ambos originales.

| Requisito | Implementación y evidencia |
| --- | --- |
| Aviso automático al equipo comercial al empezar a procesar una RFP válida | El intake publica `rfp_ticket_created` después de validar clasificación, metadata, routing y extractos, antes del análisis departamental. La subida inicial sólo crea el ticket. F1 cubre RFP válida, descarte, routing inválido y fallo posterior. Escenario A aprobado. |
| Contrato TrackFlow | `id` decimal, evento nombrado y JSON con `ticket_id`, `rfp_id`, `client_name`, `client_country`, `services_requested`, `status` y `created_at`. La proyección usa `analyzing`, conserva UTC y representa los datos ausentes con `null`. El estado interno permanece `analizando`. |
| Registro ordenado, durable e idempotente | PostgreSQL confirma decisión y evento juntos y serializa la asignación de cursores. F1 cubre rollback, fencing y lease, concurrencia, commit incierto, redelivery y reuso de la decisión confirmada. |
| SSE real con ciclo de vida acotado | FastAPI entrega `text/event-stream`, comentarios heartbeat y headers de caché y buffering. Libera la sesión PostgreSQL entre lotes y cancela al cerrar el cliente. Pruebas HTTP reales directas y comprobación de entrega progresiva mediante Next.js. |
| Recuperación, snapshot y retención | Snapshot y cursor comparten vista transaccional. Replay desde `Last-Event-ID`, cursor como cadena, recuperación de reinicio y reset `409` seguido de snapshot ante un cursor vencido o inválido. Purga configurable de prefijo de siete días por defecto que conserva la decisión durable. F2 y escenario B. |
| JWT y autorización comercial de mínimo privilegio | JWT existente, identidad activa y revalidación de permisos. Grant explícito para snapshot, stream y detalle reducido, con permisos administrativos preservados. F2 cubre token ausente, inválido o vencido, revocación, desactivación y acceso sin grant. Escenario C cubre dos comerciales y cierre de stream al salir. |
| Backoffice integrado y recuperación de la interfaz | La vista RFP consume `fetch` y `ReadableStream` con parser incremental UTF-8, cancelación, backoff acotado y jitter. Avisa e integra la fila de forma localizada, muestra estados de conexión, deduplica replay y carga propia, protege de respuestas tardías y conserva selección. El detalle existente reconcilia el estado terminal. F3, pruebas Node y escenarios A a C. |
| Compatibilidad con el flujo RFP existente | Los gates focales cubrieron intake, response generation, HITL, autorización y Backoffice. Se conserva la proyección administrativa y el detalle comercial sólo lee lo necesario. La prueba con analizador determinista usa PDF, API, worker y persistencia reales en QA. |

Los casos opcionales quedan fuera de Parte 1. La medición de 50 conexiones era adicional y queda sin medir. FourGeeks real y producción quedan fuera de la aceptación de QA. En el host Windows, el build acreditado usa Webpack y el lint focal pasó. El lint global registra cuatro errores y una advertencia preexistentes.

## Borrador de Pull Request

**Título propuesto** · TrackFlow SSE Parte 1 · Avisos comerciales de RFP con replay y permisos

**Descripción**

TrackFlow avisa al equipo comercial cuando una RFP válida empieza a procesarse. El registro PostgreSQL permite recuperar avisos tras cortes de conexión y el Backoffice existente actualiza la bandeja sin recargarla. El acceso comercial requiere grant explícito y permite leer únicamente el snapshot, el stream y la proyección mínima del ticket.

El cambio incluye P01, que serializa la autenticación TinyDB y devuelve HTTP 503 o cierra el stream ante un fallo del almacén, y P02, que reconcilia la fila con el detalle existente y conserva el estado terminal y la selección. Los gates técnicos cubrieron transporte directo y proxy, regresión de RFP, expiración, dos clientes, replay y cursor purgado. Usuario + ChatGPT aprobaron A, B y C en QA determinista. Para instalar, aplicar `rfp_sse.sql` después de intake y antes de arrancar el worker actualizado, y `rfp_sse_commercial.sql` después del esquema base de aprobación. Los grants se asignan explícitamente por entorno.

### Preguntas de diseño para el PR

1. **¿Cómo se distribuye el evento y qué cambia con 50 usuarios?** Cada navegador mantiene su propia conexión SSE autenticada y su cursor. Los streams leen lotes ordenados del mismo registro PostgreSQL y devuelven la conexión SQL al pool antes de escribir en el socket. La carga ociosa crece aproximadamente con el número de streams dividido por el intervalo de consulta. Con 50 usuarios mediría latencia de entrega, consultas por segundo, ocupación del pool y memoria. Si el sondeo resulta costoso, evaluaría un lector compartido por proceso conservando el replay individual y el manejo de consumidores lentos.
2. **¿Cómo se recuperan eventos y se evitan duplicados?** Snapshot y marca de avance se leen en una misma vista transaccional. El stream lee en orden a partir de esa marca. Tras un corte o reinicio, el cliente envía el último cursor aplicado con `Last-Event-ID`. El registro durable y la unicidad por tipo y ticket evitan duplicados del lado servidor. La interfaz fusiona por `ticket_id` y deduplica avisos por ID de evento. Si el cursor salió de retención, el cliente recibe `409` y recupera snapshot y cursor antes de volver a conectar.
3. **¿Por qué SSE en este caso?** El flujo de avisos es unidireccional, del servidor al navegador. SSE permite eventos nombrados, IDs y keep-alives sobre HTTP, integrado con el proxy y el bearer JWT existente vía `fetch`. La carga de PDF y las demás acciones conservan sus peticiones HTTP. WebSockets ofrece bidireccionalidad, que este flujo de avisos no requiere.

## Validación reproducible para el evaluador

1. Desde una copia con dependencias del repo, preparar una instancia PostgreSQL 16 exclusiva de pruebas. Configurar en privado `TRACKFLOW_SSE_QA_DATABASE_URL` con el DSN `postgresql+psycopg2` en `127.0.0.1`, puerto explícito desde 1024, usuario `trackflow_sse_qa` y base con prefijo `trackflow_sse_qa_`. El rol necesita crear esquemas. Las pruebas crean y eliminan únicamente sus esquemas aleatorios. El fixture aplica las migraciones necesarias.
2. Ejecutar los comandos de abajo desde la raíz, con el Python del proyecto y Node compatible con el Backoffice. Confirmar 12 pruebas en PostgreSQL/Uvicorn, 3 pruebas TinyDB y 6 en Node. Si las de PostgreSQL aparecen como `SKIP`, revisar el DSN dedicado y el opt-in.

```powershell
$env:TRACKFLOW_SSE_QA_DDL_DML = 'YES'
try {
  python -B -m pytest -p no:cacheprovider -q tests/test_rfp_sse_postgres.py tests/test_rfp_sse_http.py tests/test_auth_tinydb_concurrency.py
} finally {
  Remove-Item Env:TRACKFLOW_SSE_QA_DDL_DML
}
node --disable-warning=ExperimentalWarning --test tests/test_rfp_sse_frontend.mjs
```

3. Para validar el navegador en una segunda base desechable, aplicar mediante `psql -X -v ON_ERROR_STOP=1` estos DDL de `infra/sql/` en orden: `rfp_intake.sql`, `rfp_sse.sql`, `rfp_response_generation.sql`, `rfp_approval.sql`, `rfp_approval_extension.sql`, `rfp_approval_monthly_storage_volume.sql`, `rfp_approval_indicative_validation_bundle.sql` y `rfp_sse_commercial.sql`. Seguir la configuración de entorno, los comandos Webpack y el worker determinista del [runbook](rfp-sse.md). Provisionar en el TinyDB de QA cuatro usuarios activos con UUID propios y contraseñas distintas generadas localmente. Usar el rol `admin` para cargas, `user` para los dos comerciales y `manager` para el usuario sin grant. Para cada comercial crear un principal sintético en `rfp_principals`, con su UUID de usuario en `trackflow_user_id` y entorno `qa`, vincularlo en `rfp_identity_bindings` con `auth_source=local`, `issuer=urn:trackflow:qa:local`, `subject` igual al UUID de usuario y entorno `qa`, y asignar su grant en `rfp_sse_commercial_grants`. La identidad sin grant puede tener principal y vinculación, con la fila de grant ausente. El fixture `sessions` de `tests/test_rfp_sse_http.py` muestra las inserciones mínimas. Aplicar el bootstrap con el archivo TinyDB fuera de servicio.
4. Abrir `http://127.0.0.1:18163/backoffice/rfps` y reproducir A, B y C con perfiles de navegador separados y PDFs versionados de `tests/fixtures/rfp_intake/`. Ejecutar el worker determinista sobre cada UUID salido de una carga real. Para B, interrumpir sólo la conexión de ese perfil, cargar desde el administrador, restaurar y verificar recuperación y selección. Para C, comprobar dos streams, rechazo sin grant y cierre al salir. En la pestaña de red, comprobar una respuesta `text/event-stream` y su heartbeat mediante Next.js. Para aislar el transporte, comparar snapshot y stream en los puertos 18162 y 18163 con el mismo bearer de QA y cursor, usando un cliente HTTP local con buffering desactivado. Mantener el JWT en el entorno privado del cliente y fuera de las trazas compartidas.

## Inventario y propuesta de commits

Rama `feature/sse-notifications`, base `f1012c7f55a4cb051599f29f0ac8eeabce313317`. Los 38 pathspecs de las tres agrupaciones constituyen el inventario de entrega. Comandos para ejecutar desde la raíz sólo al autorizar la entrega Git. Completar cada grupo antes de preparar el siguiente.

### 1. Persistencia, API y autorización

Mensaje `feat(rfp): publish durable commercial SSE notifications`

```powershell
git add -- 'data/pipelines/rfp_intake/contracts.py' `
  'data/pipelines/rfp_intake/graph.py' `
  'data/pipelines/rfp_intake/runtime.py' `
  'infra/sql/rfp_sse.sql' `
  'infra/sql/rfp_sse_commercial.sql' `
  'scripts/rfp_sse_retention.py' `
  'services/api/auth_database.py' `
  'services/api/auth_security.py' `
  'services/api/auth_services.py' `
  'services/api/main.py' `
  'services/api/rfp_approval_auth.py' `
  'services/api/routes/rfp.py' `
  'services/api/routes/rfp_notifications.py' `
  'services/rfp_intake/intake_events.py' `
  'services/rfp_intake/notifications.py' `
  'services/rfp_intake/sink.py' `
  'services/rfp_intake/tasks.py' `
  'tests/test_auth_tinydb_concurrency.py' `
  'tests/test_rfp_e2e_isolated.py' `
  'tests/test_rfp_hito9_academic_e2e.py' `
  'tests/test_rfp_platform.py' `
  'tests/test_rfp_sse_http.py' `
  'tests/test_rfp_sse_postgres.py'
```

### 2. Integración con el Backoffice

Mensaje `feat(backoffice): consume and reconcile commercial RFP notifications`

```powershell
git add -- 'tests/test_rfp_approval_backoffice_contract.py' `
  'tests/test_rfp_sse_frontend.mjs' `
  'uis/backoffice/app/backoffice/rfps/page.tsx' `
  'uis/backoffice/components/rfp/CommercialRfpIntake.tsx' `
  'uis/backoffice/lib/rfpApi.ts' `
  'uis/backoffice/lib/rfpFeedState.ts' `
  'uis/backoffice/lib/rfpLabels.ts' `
  'uis/backoffice/lib/rfpSse.ts' `
  'uis/backoffice/lib/useRfpNotifications.ts' `
  'uis/backoffice/types/rfp.ts'
```

### 3. Runbooks y evidencia de aceptación

Mensaje `docs(rfp): record SSE acceptance and delivery procedure`

```powershell
git add -- 'docs/operations/rfp-approval-e2e.md' `
  'docs/operations/rfp-response-generation.md' `
  'docs/operations/rfp-sse.md' `
  'docs/operations/rfp-sse-delivery.md' `
  'memory-bank/progress.md'
```

Excluir los 28 JSON de `pipelines/eval/rag/` y `uis/backoffice/next-env.d.ts`. Los 29 coinciden byte a byte con el índice. Excluir y preservar `.qa-orchestrator/` y los tres PDF de `pipelines/raw/`, que coinciden con los fixtures versionados. Excluir configuración privada, TinyDB real o de QA, dependencias, compilaciones, logs, capturas y demás artefactos de ejecución. En particular, `TrackFlow-SSE-QA` y `TrackFlow-QA-Local` se conservan en el perfil privado de Windows, fuera del repositorio. Usar `git diff --cached --check` y `git diff --cached --name-only` al revisar cada grupo de staging.

## Retiro seguro de los servicios de aceptación

La última comprobación mantenía operativos FastAPI en `18162`, Backoffice en `18163` y el contenedor PostgreSQL `trackflow-sse-acceptance-272fc38a2729` en `64417`. Relay y worker ordinarios quedaron fuera de esta sesión. Los tickets y sus cursores, el TinyDB, el inventario local, los logs y los PDF/Markdown quedan como evidencia. La carpeta `TrackFlow-SSE-QA` contiene material privado, incluido `session.json` y las credenciales. Mantenerla fuera de Git.

Cuando se solicite apagar los servicios, cerrar las sesiones del navegador y ejecutar el bloque desde la raíz del repo. El bloque valida nombre de sesión y puerto de PostgreSQL y la identidad de ambos procesos antes de terminar sus árboles. Si un PID fue reutilizado, aborta con los procesos intactos. PostgreSQL queda en ejecución para preservar su base.

```powershell
@'
import importlib.util
from pathlib import Path

path = Path.home() / "TrackFlow-SSE-QA" / "qa.py"
spec = importlib.util.spec_from_file_location("trackflow_qa", path)
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)
cfg = qa.load()
qa.check(cfg["name"] == "trackflow-sse-acceptance-272fc38a2729", "Sesion inesperada")
qa.check(cfg["port"] == 64417, "Puerto PostgreSQL inesperado")
qa.container_ok(cfg)

owned = []
for key in ("web", "api"):
    entry = cfg[key]
    stamp = qa.proc_stamp(entry["pid"])
    if stamp is not None:
        qa.check(stamp == entry["stamp"], f"PID de {key} reutilizado")
        owned.append((key, entry))
for key, entry in owned:
    stamp = qa.proc_stamp(entry["pid"])
    if stamp is None:
        continue
    qa.check(stamp == entry["stamp"], f"PID de {key} reutilizado")
    qa.cmd(["taskkill.exe", "/PID", str(entry["pid"]), "/T", "/F"])
    print(f"Servicio QA detenido: {key}")
'@ | .\.venv\Scripts\python.exe -B -
Get-NetTCPConnection -State Listen -LocalPort 18162,18163 -ErrorAction SilentlyContinue
```

La última consulta debe quedar vacía. El contenedor fue creado con `--rm` y datos en `tmpfs`. El comando privado `qa.ps1 stop` incluye `docker stop` y perdería la base con los tickets y eventos. El retiro de PostgreSQL requiere autorización explícita y, si se necesita conservar su contenido, un respaldo privado verificado antes de detenerlo. Los archivos privados y artefactos requieren autorización independiente para cualquier eliminación.
