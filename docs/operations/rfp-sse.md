# Notificaciones SSE de intake RFP

El Backoffice de TrackFlow avisa al equipo comercial cuando una RFP válida entra al procesamiento departamental. El aviso identifica el ticket. El progreso técnico y los resultados de análisis siguen en el detalle del ticket.

## Frontera y contrato

La recepción del PDF crea el ticket en `analizando`. La notificación se registra después de confirmar la clasificación, extraer y validar la metadata, resolver el routing y preparar los extractos departamentales. PostgreSQL confirma juntos la decisión de intake y el evento. El worker guarda el hash del Markdown y los datos validados necesarios para saltar clasificación y extracción al reintentar. Un fallo posterior de análisis se refleja en el job y conserva el aviso de entrada.

```text
id: 125
event: rfp_ticket_created
data: {"ticket_id":"11111111-1111-4111-8111-111111111111","rfp_id":null,"client_name":"Ejemplo QA","client_country":"US","services_requested":["warehouse"],"status":"analyzing","created_at":"2026-11-24T17:00:00Z"}

```

El ID SSE es un cursor decimal que se conserva como cadena en el navegador. El estado `analyzing` pertenece a la proyección SSE. El campo `created_at` conserva la hora de creación del ticket, en UTC. El identificador documental, cliente y país pueden ser `null`. Los servicios contienen sólo los IDs soportados y solicitados, sin duplicados.

## Distribución, recuperación y transporte

Cada cliente abre su propia conexión autenticada. FastAPI consulta PostgreSQL por lotes acotados, devuelve la sesión al pool y luego entrega los eventos por el socket. La carga ociosa crece con el número de conexiones dividido por el intervalo de consulta. Para evaluar 50 usuarios, medir latencia de entrega, tasa de consultas, conexiones SQL ocupadas y memoria. Si el coste de sondeo lo justifica, un lector compartido por proceso puede reducir consultas a cambio de coordinar consumidores lentos y replay.

La carga inicial combina el listado con una marca de avance en una misma vista transaccional de PostgreSQL. El stream lee desde esa marca en orden de cursor, incluso si el evento se confirmó antes de que se abriera la conexión HTTP. El contador de cursores se actualiza en la transacción del evento y serializa la confirmación de los escritores. El navegador envía el último ID aplicado en `Last-Event-ID` al reconectar, fusiona filas por `ticket_id` y evita repetir avisos al reproducir eventos. Los tickets del snapshot no emiten avisos históricos. Una RFP en cola al desplegar puede emitir su primer aviso si cruza la frontera durable.

SSE cubre la comunicación unidireccional servidor → navegador de este caso. La carga del PDF y las operaciones del Backoffice siguen usando HTTP. WebSockets añadiría un canal bidireccional que el flujo de avisos no necesita. La recuperación depende del cursor y del registro durable, incluidos los reinicios del backend.

## API y permiso comercial

Las rutas usan el JWT local del Backoffice mediante `Authorization: Bearer`. FastAPI corta la conexión al expirar el JWT y vuelve a comprobar la identidad activa y el permiso en cada lectura del stream.

| Ruta | Respuesta |
| --- | --- |
| `GET /rfps/notifications/snapshot` | Listado mínimo autorizado y cursor de inicio. Acepta `limit` y `offset`. |
| `GET /rfps/notifications/stream` | SSE desde el cursor enviado en `Last-Event-ID`. |
| `GET /rfps/notifications/{ticket_id}` | Proyección mínima autorizada de un ticket. |

Un cursor ausente, inválido, purgado o posterior al último registro devuelve `409` con `detail.code = rfp_cursor_reset`. El cliente vuelve a cargar el snapshot antes de abrir otra conexión. Si el cursor vence con el stream abierto, el servidor cierra la conexión y comunica el reset por HTTP al reconectar.

El administrador conserva sus rutas de trabajo existentes. El usuario comercial requiere un principal activo, una vinculación local activa en el entorno correcto y un grant activo en `rfp_sse_commercial_grants`. El grant permite leer el snapshot, stream y detalle comercial, con el mismo alcance. La proyección excluye PDF, Markdown, rutas de archivo, handoffs y documentos finales. El permiso comercial deja intactas las restricciones de subida, generación, aprobación, arbitraje y administración. La instalación no crea identidades ni asigna grants.

## Migraciones y operación

Aplicar `infra/sql/rfp_sse.sql` después de `infra/sql/rfp_intake.sql` y antes de arrancar el worker de intake actualizado. Aplicar `infra/sql/rfp_sse_commercial.sql` después de provisionar el esquema de aprobación. Usar el cliente SQL del entorno con parada ante el primer error. Verificar el destino antes de aplicar DDL.

Configurar `TRACKFLOW_ENVIRONMENT` con el entorno que corresponda a las vinculaciones. El operador asigna el grant a un `principal_id` existente y previamente autorizado, junto a su `environment`. Revisar ambos valores, el usuario vinculado, la vigencia y el alcance antes de confirmar. La revocación consiste en fechar `revoked_at` para esa fila. Los grants de aprobación se administran de forma independiente.

La retención predeterminada es de siete días y se configura con `RFP_SSE_RETENTION_DAYS`. Con `DATABASE_URL` verificada para la operación, ejecutar `python -m scripts.rfp_sse_retention --apply`. Cada ejecución purga un lote consecutivo y avanza la marca de purga en la misma transacción. Repetir por lotes si queda trabajo pendiente. La decisión de intake permanece disponible después de la purga.

## Reproducción de la aceptación funcional

Usar un PostgreSQL desechable y un almacén TinyDB exclusivo de QA. Preparar un administrador para las cargas, dos sesiones comerciales con grant y una identidad sin grant. Usar PDF sintéticos del directorio `tests/fixtures/rfp_intake/`. Para cada caso, anotar hora UTC, identidad funcional, ticket y resultado. Guardar capturas sanitizadas fuera del repositorio y excluir JWT de capturas, trazas y logs.

| Servicio | Configuración de referencia para QA local |
| --- | --- |
| PostgreSQL | PostgreSQL 16 desechable en loopback. Aplicar los DDL de intake, respuesta, aprobación y SSE en orden de dependencia. Usar el puerto asignado al contenedor exclusivo. |
| FastAPI | Loopback `18162`. Configurar `DATABASE_URL`, `TRACKFLOW_ENVIRONMENT=qa`, `TRACKFLOW_AUTH_DATABASE_PATH` y `RFP_ARTIFACT_ROOT` exclusivos de QA. Inyectar el secreto JWT de QA por entorno. |
| Procesamiento | Ejecutar relay y worker del entorno aislado con Redis propio o usar un arnés determinista que invoque `execute_claimed_job()` para el job creado por la carga real. Compartir el directorio de artefactos con la API. |
| Backoffice | Loopback `18163`, `API_BACKEND_URL=http://127.0.0.1:18162` al construir y servir. Dejar `NEXT_PUBLIC_API_URL` vacío para usar el proxy de Next.js. |

Iniciar la API con `python -m uvicorn services.api.main:app --host 127.0.0.1 --port 18162`. Desde `uis/backoffice`, ejecutar `npm run build -- --webpack` y `npm run start -- --hostname 127.0.0.1 --port 18163` con el upstream anterior. El build con Webpack permite ejecutar este procedimiento en el host Windows donde falló el subproceso PostCSS de Turbopack.

Para probar sin proveedores externos, mantener apagados el relay y el worker ordinarios en **ese entorno QA**. Cargar `trackflow-request-1.pdf` como administrador, copiar el UUID devuelto y ejecutar el worker determinista de abajo una vez para ese UUID. La API y el worker deben recibir el mismo `RFP_ARTIFACT_ROOT`. El manual debe usar una base QA dedicada con nombre prefijado `trackflow_sse_qa_`, usuario `trackflow_sse_qa` y DSN `postgresql+psycopg2://` en loopback.

```powershell
$env:TRACKFLOW_SSE_QA_TICKET = '<UUID del ticket QA recién aceptado>'
@'
import os
from uuid import UUID
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlmodel import Session

target = make_url(os.environ["DATABASE_URL"])
assert target.drivername == "postgresql+psycopg2" and target.host == "127.0.0.1"
assert target.username == "trackflow_sse_qa" and target.database.startswith("trackflow_sse_qa_")
assert os.environ["TRACKFLOW_ENVIRONMENT"] == "qa" and os.environ["RFP_ARTIFACT_ROOT"]

from services.rfp_intake import tasks
from services.rfp_intake.models import rfp_jobs
from services.rfp_intake.store import claim_job
from tests.pipelines.test_rfp_intake import ConcurrentAnalyzer, MARKDOWN

class DeterministicGateway:
    settings = type("Settings", (), {"generation_timeout_seconds": 1})()
    def __init__(self, _):
        pass
    def close(self):
        pass

class DeterministicAnalyzer(ConcurrentAnalyzer):
    def analyze_department(self, **kwargs):
        kwargs["relevant_extract"] = kwargs["relevant_extract"][:len(MARKDOWN)]
        return super().analyze_department(**kwargs)

tasks.FourGeeksHTTPClient = DeterministicGateway
tasks.FourGeeksRFPAnalyzer = lambda *args, **kwargs: DeterministicAnalyzer()
tasks.load_settings = lambda: None
tasks.load_intake_fourgeeks_budget_settings = lambda **kwargs: None
engine = create_engine(target, hide_parameters=True)
with Session(engine) as session:
    assert session.execute(text("SELECT current_database(), current_user")).one() == (
        target.database, target.username,
    )
    job_id = session.execute(select(rfp_jobs.c.job_id).where(
        rfp_jobs.c.ticket_id == UUID(os.environ["TRACKFLOW_SSE_QA_TICKET"]),
        rfp_jobs.c.job_kind == "intake",
        rfp_jobs.c.status == "queued",
    )).scalar_one()
    job = claim_job(session, job_id, lease_seconds=120)
    assert job is not None
    session.commit()
tasks.execute_claimed_job(engine, job, tasks.get_rfp_settings())
'@ | python -B -
```

1. **Operación normal.** Abrir `/backoffice/rfps` en una sesión comercial. Con el administrador, cargar una RFP válida. Confirmar que el aviso aparece automáticamente durante el análisis y permite reconocer el ticket, el cliente disponible y los servicios. Cada sesión debe ver una sola fila para el ticket.
2. **Desconexión.** Mantener abierta la vista comercial y cortar su conectividad. Cargar una segunda RFP válida desde la sesión administradora. Restablecer la conexión comercial y comprobar que se recupera el alta una sola vez, sin cambiar la selección actual.
3. **Permisos.** Abrir dos sesiones comerciales y cargar otra RFP válida. Confirmar que ambas reciben el mismo ticket. Confirmar que el usuario sin grant recibe `403` en el stream y que la sesión comercial carece de acciones administrativas. Cerrar sesión y comprobar que cesa el stream.

La suite técnica de persistencia y transporte directo se ejecuta mediante `python -m pytest -q tests/test_rfp_sse_postgres.py tests/test_rfp_sse_http.py`, con `TRACKFLOW_SSE_QA_DDL_DML=YES` y `TRACKFLOW_SSE_QA_DATABASE_URL` apuntando a PostgreSQL desechable en loopback. Los fixtures acotan el usuario, el prefijo de base y el esquema temporal.

## Evidencia técnica y límites

### Correctivo de aceptación P01 y P02

La API serializa las operaciones de autenticación que usan el handle
`JSONStorage` compartido, incluidas las transacciones de lectura y escritura.
Operar con un proceso API por archivo TinyDB y ejecutar los escritores offline
fuera de la ventana de servicio. El bloqueo pertenece al proceso. Si el almacén
falla antes de iniciar el SSE, la API responde `503`. Si falla durante el
stream, lo cierra para permitir replay con el último cursor aplicado.

La vista comercial actualiza la fila del ticket a partir de la consulta de
detalle existente. La selección permanece activa y una respuesta atrasada o el
evento histórico de alta no revierte el estado terminal.

El `500` observado en Next.js coincidió con `ECONNREFUSED` de FastAPI. Los logs
demostraron además que la carrera de TinyDB interrumpía streams. La salida del
proceso anterior de FastAPI sigue sin causa confirmada. El supervisor privado
de la sesión QA registra el código de salida del proceso restaurado.

La selección focal del correctivo pasó 13 pruebas de backend, 6 pruebas Node y la
verificación en Chromium con cursor realmente purgado. En la navegación
Dashboard → RFPs el snapshot cargó tickets existentes. El cambio de estado
se reflejó con dos GET de detalle, sin nuevas consultas después del estado
terminal. La revisión independiente de concurrencia y JWT obtuvo 9 pruebas aprobadas.

### Evidencia del gate técnico inicial

La revisión independiente obtuvo 12 pruebas aprobadas en PostgreSQL y Uvicorn aislados, 235 en la regresión focal y 5 en el parser y la fusión del frontend. TypeScript, ESLint focal y el build Webpack pasaron. El lint global conserva cuatro errores y una advertencia de archivos preexistentes fuera del cambio.

El proxy Next.js entregó el primer evento en 1,03 s y otro a los 94,08 s con heartbeats entre ambos. Chromium comprobó dos sesiones comerciales, dos cargas desde el Backoffice, replay tras una interrupción provocada del stream, conservación de la selección, deduplicación del alta propia y rechazo de una identidad sin grant. El worker usó el analizador determinista con el PDF y las persistencias reales. La capacidad de 50 conexiones no se midió.

Comandos de la revisión focal en Windows:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q tests/test_rfp_sse_postgres.py tests/test_rfp_sse_http.py
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q tests/pipelines/test_rfp_intake.py tests/test_rfp_platform.py tests/test_rfp_response_api.py tests/test_rfp_approval_api.py tests/test_auth_token_purpose.py tests/test_auth_me.py tests/pipelines/test_rfp_response_graph.py tests/pipelines/test_rfp_approval_hitl.py tests/test_rfp_approval_authorization.py tests/test_rfp_response_backoffice_contract.py tests/test_rfp_approval_backoffice_contract.py
node --disable-warning=ExperimentalWarning --test tests/test_rfp_sse_frontend.mjs
```

En `uis/backoffice`, con el upstream QA indicado y `NEXT_PUBLIC_API_URL` vacío:

```powershell
.\node_modules\.bin\tsc.cmd --noEmit --incremental false
.\node_modules\.bin\eslint.cmd app/backoffice/rfps/page.tsx components/rfp/CommercialRfpIntake.tsx lib/rfpApi.ts lib/rfpFeedState.ts lib/rfpLabels.ts lib/rfpSse.ts lib/useRfpNotifications.ts types/rfp.ts
npm.cmd run build -- --webpack
```

## Aceptación funcional aprobada

Usuario + ChatGPT aprobaron SSE Parte 1 en el Backoffice auténtico con el analizador determinista. Estado `ACEPTACION_FUNCIONAL_APROBADA_EN_QA`. La entrega Git queda pendiente.

| Escenario | Ticket sintético | Resultado aprobado por Usuario + ChatGPT |
| --- | --- | --- |
| A | `d012d2c4-3d97-4533-ba37-cf70ef353763` | La carga administrativa apareció como aviso comercial tras la validación, con los datos del cliente y servicios. El detalle comercial pudo abrirse. |
| B | `5fc553e6-86cc-4fd3-826c-d5c77709a92e` | La sesión recuperó el alta al reconectar, deduplicó la fila y conservó la selección anterior. |
| C | `156ebb90-f4d2-4dc4-8d3c-43c6d5d644a6` | Los dos comerciales recibieron el alta. La identidad sin grant quedó restringida y cerrar sesión cerró el stream correspondiente. |

El cierre técnico corroboró por lectura de PostgreSQL los tres tickets en `analisis_completo`, sus jobs en `succeeded`, un evento por ticket con cursores 1, 2 y 3 respectivamente, y dos grants activos de QA. Los PDF y Markdown de los tres tickets siguen en el directorio privado de artefactos. FastAPI y Backoffice respondieron HTTP 200 en sus rutas de comprobación. Los resultados de navegador de la tabla corresponden a la aceptación de Usuario + ChatGPT. El gate técnico aporta evidencia separada de transporte directo, proxy y recuperación de cursor purgado.

El [plan de entrega SSE Parte 1](rfp-sse-delivery.md) reúne la rúbrica disponible, las respuestas de diseño, los comandos de validación y el procedimiento para detener API y Backoffice preservando PostgreSQL. La aceptación usa el analizador determinista. FourGeeks real, capacidad para 50 conexiones y despliegue de producción quedan fuera de la validación acreditada.
