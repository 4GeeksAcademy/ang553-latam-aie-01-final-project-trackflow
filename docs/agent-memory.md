# Memoria estructurada del agente Support (MEM-092)

## Alcance y arquitectura mínima

MEM-092 añade memoria episódica estructurada al mismo `ExternalSupportAgent`. La
respuesta principal sigue usando el grafo Support y el RAG existentes; la memoria
no es un segundo RAG, no escribe en Qdrant, no cambia sus colecciones y no usa el
checkpointer efímero como persistencia.

PostgreSQL conserva cuatro entidades explícitamente provisionadas:

- `agent_memory_conversation`: ownership de un `conversation_id` por `user_id`,
  sin transcripción ni contenido del mensaje.
- `agent_memory_proposal`: candidato estructurado, pendiente durante un máximo de
  24 horas y limitado a uno por identidad y conversación.
- `agent_memory_decision`: auditoría append-only de aprobación, rechazo, edición,
  descarte ambiguo o expiración. La idempotencia es única por actor.
- `agent_memory_entry`: versión consolidada activa, expirada o sustituida. Toda
  entrada referencia la decisión aprobatoria que la autorizó.

La FK compuesta `proposal(conversation_id,user_id) ->
conversation(id,user_id)` impide atribuir una propuesta a la conversación de otra
identidad. Un trigger PostgreSQL revisable comprueba además que la decisión
autorizante esté aprobada y corresponda a la misma propuesta, ámbito, categoría,
clave canónica y contenido.

## Flujo completo

1. `POST /support/query` construye un único `AccessContext`, valida el tenant y
   crea o verifica el binding de conversación. El servidor genera el UUID si el
   cliente no lo aporta y lo devuelve en la respuesta.
2. Antes de responder, el grafo recupera solo entradas activas, no expiradas y
   visibles para ese `AccessContext`. La relevancia está cerrada por categoría y
   términos de consulta; no usa embeddings adicionales.
3. La respuesta principal se produce normalmente. El contexto recuperado se
   renderiza como datos `user_reported`, delimitados y no confiables; nunca como
   instrucciones ni como hechos verificados.
4. Después de la respuesta, un prefiltro determinista decide si existe un
   candidato permitido. Solo entonces puede ejecutarse una autoevaluación
   estructurada y acotada por el presupuesto común.
5. Un resultado válido vuelve a pasar por `MemoryStore`, que valida contenido,
   identidad, tenant, ámbito y clave canónica antes de persistir una propuesta.
6. La UI presenta la propuesta. Los botones usan
   `POST /support/memory/decision`; si el usuario escribe el siguiente turno,
   `POST /support/query` lo clasifica mediante JSON Schema frente a la propuesta
   recuperada por el servidor. Aprobar consolida; rechazar o descartar no crea
   entrada; editar crea una propuesta sustituta que exige otra confirmación.
7. Conversaciones posteriores pueden leer solo entradas aprobadas. Un fallo de
   lectura, evaluación o persistencia de propuesta no elimina la respuesta
   principal.

## Decisiones funcionales solicitadas por 4Geeks

### Qué recordar

La taxonomía es cerrada:

- `carrier_coverage`: correcciones duraderas de cobertura o asignación de un
  transportista. Se prefiere ámbito cliente; el ámbito global exige administrador
  y una intención global explícita.
- `recurring_incident`: patrón recurrente con al menos dos ocurrencias, no un
  incidente o paquete individual.
- `b2b_report_preference`: preferencia mensual de reporte B2B, siempre ligada a
  un cliente.

### Cómo detectar un candidato

El prefiltro determinista evita una llamada al modelo para consultas ordinarias y
rechaza texto citado o con señales de prompt injection. Un manager con múltiples
clientes necesita un `client_id` explícito y autorizado. Si hay candidato, el
evaluador debe devolver una estructura tipada compatible con la categoría; JSON
inválido, timeout, proveedor ausente o presupuesto insuficiente significan «sin
propuesta».

### Cómo aprobar o rechazar

Las acciones confiables son `approve`, `reject`, `edit`, `ambiguous` y
`topic_change`. Con propuesta pendiente, el siguiente mensaje se clasifica con
el gateway FourGeeks y un JSON Schema estricto. Una respuesta puede separar una
decisión clara de una segunda pregunta; la decisión se registra primero y la
pregunta restante atraviesa una vez el flujo Support normal. El fallback textual
solo acepta confirmaciones o rechazos completos y exactos, nunca coincidencias
por substring; cualquier otra respuesta se descarta como ambigua y queda
auditada, en vez de conservar silenciosamente la propuesta. La edición no confía
en categoría, ámbito, origen o clave enviados
por el navegador: el backend los deriva de la propuesta original y revalida el
contenido.

Las transiciones terminales son auditables y usan CAS/versionado. Reintentar la
misma acción con `(actor_user_id,idempotency_key)` devuelve el resultado previo;
reutilizar la clave para otra decisión produce conflicto. Ambigüedad, silencio,
cambio de tema y expiración nunca activan memoria.

### Consolidación y uso posterior

Una aprobación crea o sustituye la entrada con la misma combinación de ámbito,
cliente, categoría y clave canónica. La versión anterior queda `superseded`; no se
borra. Contenido idéntico se deduplica. La recuperación posterior solo considera
`active`, `expires_at > now()` y visibilidad tenant/global autorizada.

Las entradas permanecen `verification_status=user_reported`. Esto significa
«aceptado por el usuario como contexto», no «verificado por TrackFlow». No deben
elevarse a `system_verified`, cambiar routing, autorizar herramientas ni sustituir
las fuentes operativas/RAG.

### Olvido y limpieza

- Propuesta pendiente: 24 horas. Limita decisiones basadas en contexto ya viejo.
- Cobertura de transportista: 30 días. Obliga a reconfirmar cambios operativos.
- Incidencia recurrente: 14 días. Evita perpetuar tendencias transitorias.
- Preferencia B2B: 180 días. Es más estable, pero no indefinida.
- Capacidad: 100 entradas activas por bucket `(scope, client_id, category)`.

`MemoryStore.expire_and_clean(session)` es transaccional e idempotente: registra
una decisión `expired` para propuestas vencidas, expira entradas por TTL y marca
como expiradas las más antiguas que exceden el cap. No elimina propuestas,
decisiones, autorizaciones ni la cadena de supersesión. Debe invocarse desde un
scheduler u operador de infraestructura; MEM-092 no instala otro scheduler.

```python
from services.api.agent_memory.service import MemoryStore

expired_proposals, expired_entries = MemoryStore().expire_and_clean(session)
```

La sesión debe apuntar a la base TrackFlow provisionada. Los locks y updates
condicionales resuelven carreras entre workers.

El invocador operacional soportado reutiliza exactamente esa operación:

```powershell
uv run --python 3.12 python -m scripts.agent_memory_cleanup --execute
```

Sin `--execute` termina con código 2 antes de abrir la base. Rechaza un target
que no sea PostgreSQL, no aplica DDL ni crea tablas y devuelve únicamente los
contadores `expired_proposals` y `expired_entries`. Un fallo termina con código
1 y un diagnóstico sanitizado. La capacidad puede configurarse deliberadamente
con `--max-active-per-scope-category`, que solo acepta enteros positivos.

La periodicidad pertenece al mecanismo de mantenimiento ya disponible en cada
despliegue. Para aceptación QA basta una invocación manual contra la base
desechable, seguida de otra invocación que debe producir cero cambios y así
demostrar idempotencia.

## Identidad, tenant y datos prohibidos

Solo `admin` y `manager` usan Support memory. Para memoria específica de cliente,
el manager puede leer entradas vigentes de los clientes activos de sus memberships.
Ambos roles pueden recuperar entradas globales activas, aprobadas y pertinentes
mediante `MemoryStore.list_active`. La creación y resolución de propuestas
globales exige admin con acceso global (`_authorize_scope`, `_authorize_row_scope`).
Una conversación pertenece a una identidad. Otra identidad recibe el mismo 404
sanitizado que un recurso inexistente.

No se persisten transcripciones, prompts o respuestas crudas. También se rechazan
en propuesta, edición, aprobación, auditoría y logs:

- direcciones o ubicaciones B2B/B2C;
- rutas internas de almacén;
- incidentes de un único paquete o tracking;
- negociaciones comerciales activas;
- secretos, contraseñas, credenciales, API keys, JWT o tokens.

Se guarda únicamente contenido estructurado permitido, un ID de interacción y un
SHA-256 del origen. La respuesta API no expone digest, clave canónica ni campos de
auditoría internos.

## API aditiva

`POST /support/query` conserva `question` y añade campos opcionales:

- request: `conversation_id`, `client_id`, `memory_scope`;
- response: `conversation_id`, `memory_proposal`, `memory_evaluation`,
  `memory_read_status` y `memory_consent`.

Clientes anteriores continúan siendo válidos. La propuesta pública contiene solo
ID, conversación, ámbito, cliente, categoría, contenido estructurado,
`user_reported` y expiración.

`POST /support/memory/decision` requiere `conversation_id`, `proposal_id`,
`idempotency_key` y `action`; `edited_content` solo se acepta para `edit` y
`user_text` para `topic_change`. Ambos endpoints reutilizan OIDC,
`require_support_reader` y `build_access_context`. Los errores de recurso,
conflicto, política y base se presentan como 404, 409, 422 y 503 sanitizados.

## DDL, despliegue y recuperación

No hay `create_all` ni side effects al importar. En una base vacía o migración
revisada, aplicar en este orden:

1. `infra/sql/incident_query_security.sql`;
2. `infra/sql/agent_memory.sql`.

`agent_memory.sql` usa una transacción y falla sin dejar un esquema parcial. Antes
de desplegar se requiere backup y revisión DBA. Para recuperación, restaurar las
cuatro tablas como un conjunto, manteniendo FKs, decisiones y cadenas de
supersesión; restaurar solo entries rompería la evidencia de consentimiento. El
DDL todavía no se ha aplicado a una base persistente del proyecto.

La prueba PostgreSQL es opt-in y rechaza `DATABASE_URL`, hosts no loopback y bases
sin nombre inequívoco de QA:

```powershell
$env:TRACKFLOW_MEMORY_TEST_DATABASE_URL = `
  "postgresql+psycopg2://user:password@127.0.0.1:55432/trackflow_mem092_test"
.venv\Scripts\python.exe -m pytest tests/test_agent_memory_postgres_integration.py -q
```

Debe ejecutarse únicamente contra una base QA vacía y desechable.

Evidencia final de cierre: el arnés completo pasó 11 pruebas en PostgreSQL 16
sobre una base fresh, exclusiva, loopback y alojada en `tmpfs`
(`11 passed in 2.52s`, exit 0). El contenedor de cierre se preservó para revisión
y no comparte almacenamiento con la base de aceptación. Esta evidencia valida
DDL, triggers, concurrencia, idempotencia, aislamiento y cleanup, pero no
significa que la migración se haya aplicado a una base persistente.

## Presupuesto, fallos y limitaciones

Support conserva el presupuesto compartido de 80 segundos, el statement timeout
PostgreSQL acotado y la reserva de respuesta. El binding de conversación termina
antes de instalar `SET LOCAL`, de modo que un commit no anula el timeout. La
autoevaluación ocurre después de la respuesta principal y solo si queda presupuesto
suficiente. Fallos de evaluador, estructura, persistencia o recuperación son
fail-closed para memoria y conservan la respuesta principal segura.

Limitaciones actuales:

- el JSON Schema estricto del evaluador de memoria sí fue aceptado en vivo por
  FourGeeks y produjo una propuesta válida; el esquema corregido del clasificador
  de consentimiento todavía no se revalidó E2E contra el proveedor;
- la periodicidad de cleanup debe configurarse en la infraestructura de despliegue;
- la aceptación real completó los ciclos de aprobación y rechazo y una edición
  con reconfirmación pendiente; el descarte ambiguo corregido está cubierto por
  QA automatizada, pero su repetición E2E posterior al correctivo sigue pendiente;
- el build Turbopack presenta un panic ambiental; Webpack compila correctamente;
- no hay despliegue, migración persistente ni PR de MEM-092.

## Evidencia de aceptación técnica

La aceptación real y las consultas de metadatos de la base QA acreditaron:

- ciclo A: propuesta `carrier_coverage` aprobada, decisión auditable, una memoria
  activa y recuperación observada en otra conversación;
- ciclo B: propuesta `carrier_coverage` rechazada, decisión auditable y ninguna
  memoria creada;
- edición B2B: propuesta original cerrada como `edited`, propuesta sustituta
  `pending` y ninguna activación automática;
- ambigüedad: el defecto que conservaba la propuesta como `pending` fue
  corregido y sus regresiones pasan, pero no se repitió el E2E después del
  correctivo por el bloqueo de autenticación Keycloak.

El cierre ejecutó 375 pruebas automatizadas distintas: 326 de memoria, Support,
LangGraph, RAG, MCP y contratos frontend; 11 de integración PostgreSQL real; y
38 de OIDC. También pasaron ESLint focalizado, TypeScript `--noEmit`,
`compileall` y `git diff --check`. Este resultado es aceptación técnica con
observaciones, no certificación académica ni despliegue productivo.

## Ejecución periódica de cleanup

El despliegue debe invocar el CLI desde su mecanismo de mantenimiento existente;
MEM-092 no añade un scheduler ni un servicio residente. La cuenta operativa debe
recibir `DATABASE_URL` desde el almacén de secretos del entorno, con acceso a la
misma base PostgreSQL provisionada para la API. No se debe incluir la URL en el
comando, logs ni archivos versionados.

Una ejecución PowerShell no interactiva desde la raíz del repositorio es:

```powershell
Set-Location C:\ruta\al\checkout\trackflow
uv run --python 3.12 python -m scripts.agent_memory_cleanup --execute
if ($LASTEXITCODE -ne 0) {
  throw "Agent-memory cleanup did not complete."
}
```

Cadencia recomendada: cada hora cuando se necesite que las propuestas de 24
horas se cierren con un retraso operacional máximo aproximado de una hora. Una
cadencia diaria solo es adecuada si el propietario del despliegue acepta que la
retención efectiva pueda extenderse hasta el siguiente pase. El mecanismo debe
evitar solapamientos deliberados, conservar el código de salida y registrar
únicamente los contadores sanitizados. Repetir el comando es seguro: la operación
es transaccional e idempotente.

Antes de habilitar la tarea periódica:

1. aplicar y revisar `infra/sql/agent_memory.sql` en la base objetivo;
2. comprobar que `DATABASE_URL` apunta al entorno correcto sin imprimirla;
3. ejecutar un pase supervisado y confirmar código 0;
4. ejecutar un segundo pase y comprobar el no-op esperado cuando no haya nuevos
   vencimientos;
5. configurar alerta por código distinto de cero, sin reintentos agresivos ni
   volcado de contenido.

## QA reproducible

### Preparación segura en Windows PowerShell

El runbook de aceptación funciona tanto en Windows PowerShell 5.1 como en
PowerShell 7. No debe depender de `[Convert]::ToHexString`, que no está
disponible en todos los hosts 5.1. Los secretos efímeros se generan sin
imprimirlos ni escribirlos en archivos:

```powershell
function New-TrackFlowQaSecret {
  param([ValidateRange(16, 128)][int]$ByteCount = 32)
  $bytes = New-Object byte[] $ByteCount
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($bytes)
  }
  finally {
    $rng.Dispose()
  }
  -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

$env:POSTGRES_PASSWORD = New-TrackFlowQaSecret
$env:QA_KEYCLOAK_ADMIN_PASSWORD = New-TrackFlowQaSecret
$env:QA_KEYCLOAK_MCP_CLIENT_SECRET = New-TrackFlowQaSecret
$env:QA_KEYCLOAK_PASSWORD = New-TrackFlowQaSecret
$env:QA_TRACKFLOW_LOCAL_PASSWORD = New-TrackFlowQaSecret
$env:JWT_SECRET_KEY = New-TrackFlowQaSecret
```

La aceptación debe ejecutarse en una PowerShell nueva, con PostgreSQL y Qdrant
en `tmpfs`, nombres de contenedor exactos, puertos loopback y un directorio bajo
`[IO.Path]::GetTempPath()`. Antes de aplicar DDL se verifica que la base esté
vacía y que `DATABASE_URL` no apunte a un almacén empresarial. La API, MCP y
Backoffice heredan el entorno desde esa misma sesión; ningún secreto se incluye
en argumentos, logs, capturas ni evidencia.

```powershell
# Memoria, API y contratos UI
.venv\Scripts\python.exe -m pytest `
  tests/test_agent_memory_persistence.py `
  tests/test_agent_memory_consent.py `
  tests/test_agent_memory_recovery_cleanup.py `
  tests/test_agent_memory_cleanup_cli.py `
  tests/test_support_memory_api.py `
  tests/test_support_api.py `
  tests/test_support_frontend_contract.py -q

# Autoevaluación, Support y regresión RAG
.venv\Scripts\python.exe -m pytest `
  tests/test_agent_memory_graph_evaluation.py `
  tests/test_external_support_tools.py `
  tests/pipelines/test_customer_support_agent.py `
  tests/pipelines/test_customer_support_external_tools.py `
  tests/pipelines/test_customer_support_mcp_tools.py `
  tests/test_rag_api.py tests/pipelines/test_rag.py `
  tests/pipelines/test_rag_resilience.py tests/pipelines/test_rag_resume.py -q

# OIDC aislado
.venv\Scripts\python.exe -m pytest `
  tests/test_support_oidc.py `
  tests/test_operational_oidc.py -q

# PostgreSQL real: solo con una base loopback, vacía y desechable ya autorizada
$env:TRACKFLOW_MEMORY_TEST_DATABASE_URL = "<URL QA inyectada de forma segura>"
.venv\Scripts\python.exe -m pytest `
  tests/test_agent_memory_postgres_integration.py -q
Remove-Item Env:TRACKFLOW_MEMORY_TEST_DATABASE_URL

# Frontend
Set-Location uis/backoffice
.\node_modules\.bin\eslint.cmd app\backoffice\support\page.tsx lib\supportApi.ts lib\supportOidc.ts types\support.ts
.\node_modules\.bin\tsc.cmd --noEmit
.\node_modules\.bin\next.cmd build --webpack
```

Código relacionado: [paquete de memoria](../services/api/agent_memory/README.md),
[DDL](../infra/sql/agent_memory.sql), [ruta Support](../services/api/routes/support.py),
[grafo Support](../agents/customer_support/external_graph.py) y
[recuperación segura](../agents/customer_support/memory_context.py).
