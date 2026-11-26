# TrackFlow WebSocket chat

## Estado y alcance

La pantalla `/backoffice/support` usa WebSocket para ejecutar el flujo Support
existente y entregar su respuesta validada en un único evento de contenido,
seguido por el evento de finalización. LangGraph consume SSE de FourGeeks de
forma asíncrona en las ramas de conocimiento de WebSocket. El Support HTTP
conserva su transporte síncrono.

WS-07 también acreditó en QA el opt-in de unidades sintéticas y
cobertura semántica independiente del estado del turno. La
aceptación humana distinguió cobertura completa, parcial, nula e
indeterminada y verificó la recuperación del parcial al recargar.
La aplicación externa QA aislada acreditó tracking B2B por empresa
y B2C por despacho con cuentas sintéticas y Keycloak. La publicación
de tokens de prosa natural, la equivalencia académica de las unidades
y el acceso externo de producción siguen pendientes.

Al interrumpir, el coordinador señala el abort al nodo asíncrono y espera la
limpieza local antes de confirmar la interrupción e iniciar la redirección.
Las operaciones síncronas ya iniciadas pueden demorar la confirmación hasta
su retorno. El abort evita aplicar una decisión cuando la clasificación de
consentimiento todavía está pendiente. Las decisiones que ya se confirmaron
conservan su contrato transaccional.

El historial de chat es independiente de Agent Memory. Los pares completados
del mismo hilo se acotan y se incluyen como JSON no confiable únicamente en
la generación de conocimiento. El mensaje actual sigue dirigiendo routing,
búsqueda, herramientas y consentimiento. Las sesiones antiguas conservan su
identificador y usan los mismos bits UUID en el vínculo interno con Support.

## Servicios, puertos y configuración

- Backoffice Next.js: `http://localhost:3001`.
- API FastAPI/WebSocket: `http://localhost:8000` y `ws://localhost:8000/chat/ws`.
- PostgreSQL: `DATABASE_URL`, con `infra/sql/chat_websocket.sql` aplicado solo
  sobre el ambiente autorizado. El flujo compartido requiere además el
  esquema existente de `infra/sql/agent_memory.sql`.
- Qdrant/FourGeeks/MCP/OIDC: los mismos servicios del Support API existente.
- JWT local HS256: el mismo token del Backoffice.
- OIDC RS256: Authorization Code + PKCE ya configurado para Support.

Variables frontend relevantes:

- `NEXT_PUBLIC_API_URL`: base HTTP opcional.
- `NEXT_PUBLIC_CHAT_WS_URL`: endpoint WebSocket explícito recomendado fuera de
  localhost, siempre `wss://` bajo HTTPS.

El fallback local conecta el navegador directamente a `:8000`. El rewrite
`/chat/:path*` sirve la creación HTTP de sesiones. El upgrade WebSocket a través
del rewrite de Next.js **NO ESTÁ VALIDADO** y no se considera evidencia; en
Codespaces o detrás de un proxy se debe configurar `NEXT_PUBLIC_CHAT_WS_URL` con
el endpoint `wss://` autorizado y comprobar el upgrade del proxy.

Arranque de desarrollo existente:

```powershell
docker compose --profile rag up -d --build api interfaces qdrant
```

No aplicar DDL ni arrancar proveedores contra datos no desechables. Para la
integración PostgreSQL focal:

```powershell
$env:TRACKFLOW_CHAT_TEST_DATABASE_URL = "<PostgreSQL QA desechable>"
$env:TRACKFLOW_CHAT_TEST_ALLOW_DDL = "1"
uv run pytest tests/test_websocket_chat_postgres.py -q
```

La prueba crea y elimina únicamente un schema aleatorio `chat_test_<uuid>`.
La repetición ampliada con restart/reconcile completó **1 PASS en 2.45 s**,
exit 0, sobre PostgreSQL 16 efímero en `tmpfs`; el contenedor fue eliminado
después de verificar su identidad. Esta evidencia cubre persistencia y
reconciliación single-process, no una topología multi-worker.

## Contrato y seguridad

Eventos oficiales servidor → cliente:

- `token_chunk`
- `generation_interrupted`
- `generation_completed`
- `session_snapshot`

Eventos oficiales cliente → servidor:

- `user_message`
- `interrupt_requested`

Extensiones documentadas:

- `authenticate`: primer frame obligatorio, con `session_id`, `access_token`
  HS256 y `support_token` OIDC opcional. Los tokens nunca van en URL,
  `sessionStorage`, eventos ni logs.
- `error`: error controlado con código allowlisted, sin detalle del proveedor.
- `message_id` y `generation_id`: correlación semántica.
- `cursor`: watermark durable por sesión para snapshot → eventos vivos.
- `sequence`: orden de chunks dentro de una generación.

La conexión se suscribe antes de leer el snapshot. Después descarta eventos con
`cursor <= snapshot.cursor`, evitando pérdida o duplicación en esa frontera.
Cada conexión tiene su propia cola acotada. Un consumidor lento se expulsa sin
afectar a los demás.

En la topología confirmada de un único proceso API, una reconexión también
reconcilia una generación durable `active` que ya no tenga tarea productora en
memoria. La transición PostgreSQL conserva el parcial, marca generación,
mensaje y sesión como `interrupted`, crea un solo cursor/evento y permite el
siguiente turno. La segunda reconexión es un no-op. El coordinador serializa la
decisión para no interrumpir una tarea realmente viva del proceso actual.

El primer frame HS256 autentica al usuario y se verifica ownership por usuario y
cliente antes de entregar historial. El token OIDC se verifica por separado,
debe representar al mismo usuario interno y conserva el requisito
`admin|manager`. HS256 nunca sustituye los scopes/permisos OIDC/MCP.

## Escenarios manuales integrados preparados

Estos escenarios corresponden a Usuario + ChatGPT en un ambiente QA autorizado.
El escenario 1 queda bloqueado por el contrato de publicación. Los escenarios
2 y 3 se pueden verificar con la entrega terminal actual.

### 1. Streaming incremental real

1. Iniciar API, Backoffice, PostgreSQL, Qdrant, OIDC/MCP y proveedor autorizado.
2. Entrar en `/backoffice/support` con un manager QA autorizado para un único
   cliente activo, con JWT local y OIDC vinculados.
3. Seleccionar un cliente visible y enviar una consulta de política que requiera
   generación LLM.
4. Observar al menos dos `token_chunk` auténticos antes de
   `generation_completed`, sin fragmentar artificialmente una respuesta final.
5. Abrir un segundo navegador autorizado a la misma sesión y confirmar los
   mismos cursores sin una segunda invocación.

Resultado contractual: contenido incremental real, secuencias/cursor crecientes
y una sola generación. El consumo SSE fue acreditado en una sonda directa.
La publicación incremental en navegador sigue **BLOCKED** por los guardrails.

### 2. Interrupción y redirección

1. Durante una respuesta activa, escribir una consulta distinta en
   “Redirigir con una nueva consulta”.
2. Pulsar “Interrumpir y redirigir”.
3. Confirmar el aviso de interrupción. Si el abort precede a la validación
   final, el turno queda sin texto porque no se publicó contenido.
4. Confirmar `generation_interrupted` antes del primer chunk del turno nuevo.
5. Confirmar que no llega ningún chunk de la generación anterior.

Resultado contractual: parcial conservado y nueva respuesta independiente.
Cancelación local y persistencia implementadas. Está pendiente acreditar
el recorrido integrado contra el gateway real. El cese del cómputo remoto
es una observación separada.

### 3. Desconexión y continuidad

1. Conservar el `session_id` visible y completar al menos un turno.
2. Interrumpir la red o reiniciar la pestaña durante/tras el siguiente turno.
3. Restaurar conectividad.
4. Confirmar reconexión con backoff, mismo `session_id`, snapshot sin duplicados
   y estados terminales correctos.
5. Para el caso de reinicio del API durante generación, confirmar que el
   parcial reaparece una vez como `interrupted` y que un turno nuevo puede
   completarse sin otro evento de interrupción.
6. Hacer una pregunta dependiente del turno anterior y comprobar contexto
   efectivo, no solo restauración visual.

Resultado esperado: rehidratación visual y una respuesta de conocimiento que
use una referencia del turno previo cuando la consulta actual todavía active
esa ruta y recupere evidencia pertinente. La persistencia se comprobó en
PostgreSQL desechable y el uso del historial en un grafo con proveedor
controlado. La continuidad de navegador a FourGeeks sigue pendiente.

## Respuestas para el Pull Request

1. **¿Por qué WebSocket y no SSE?** El chat necesita mensajes de usuario,
   interrupción y redirección sobre el mismo canal bidireccional mientras el
   servidor publica chunks. SSE es apropiado para las notificaciones RFP
   unidireccionales, pero no resuelve esta interacción bidireccional.
2. **¿Cómo reciben varios clientes los mismos eventos sin duplicar al agente?**
   Un coordinador mantiene una generación activa por `session_id`. El productor
   publica una vez en `chat.<session_id>` y Pub/Sub copia el evento a una cola
   independiente por conexión. Suscribirse nunca invoca al agente.
3. **¿Abort o LangGraph HITL `interrupt()`?** El coordinador detiene la
   publicación, espera que termine el trabajo local en curso, persiste el
   parcial `interrupted`, publica la interrupción y después crea otro turno.
   LangGraph HITL pausa un grafo para intervención humana. En la rama
   asíncrona, el abort solicita el cierre local del stream HTTP.

## Limitaciones conocidas

- Streaming FourGeeks directo: acreditado por el usuario con HTTP 200 y 6
  deltas en 2 lecturas. Se canceló la tarea antes de observar finalización,
  con respuesta cerrada y cero deltas posteriores a la solicitud de abort.
- Cese del cómputo remoto: no observado.
- Guardrails equivalentes sobre prosa libre incremental: BLOCKED.
- B2B/B2C: acreditado en QA para tracking. La política de vinculación
  y revocación operativa sigue pendiente.
- El productor WebSocket entrega una respuesta completa validada. La
  generación queda restringida a managers con un único cliente activo, el
  de la sesión. La capacidad MCP actual conserva el alcance completo del
  titular del token y no permite delimitar al cliente del chat en la llamada.
  Admin/global y managers multicliente reciben un error controlado en WS.
- Upgrade WebSocket mediante Next rewrite: NO MEDIDO.
- Reconciliación de huérfanos está limitada a un solo proceso API. Con varios
  workers/procesos, un proceso no puede distinguir el huérfano de una tarea
  viva en otro proceso; esa topología está BLOCKED hasta disponer de lease o
  coordinación distribuida.
- Aceptación funcional humana I15 e I16 completada en WS-07. El resto
  de esta guía conserva el historial técnico de los gates previos.

## Gate FourGeeks y decisión de acceso

Support HTTP usa un POST síncrono a `/chat/completions`. El flujo WebSocket
consume SSE asíncrono del mismo proveedor. Para ejecutar una prueba real,
habilitar una clave FourGeeks de QA en el entorno del proceso mediante el
mecanismo de secretos aprobado. Usar el gateway y modelo de QA autorizados
(`FOUR_GEEKS_BASE_URL` y `RAG_GENERATION_MODEL` si difieren de los defaults).
La prueba mínima debe registrar solo metadatos: dos deltas de contenido antes
del evento terminal observado, cancelación de la tarea, cierre local de la
respuesta HTTP y ausencia de publicaciones posteriores para la generación.
El estado del cómputo interno del proveedor requiere evidencia del proveedor.

### Sonda de transporte para la tercera iteración

Con la credencial QA inyectada al proceso y el gateway autorizado, ejecutar una
sola vez desde la raíz del worktree:

```powershell
uv run --offline --python 3.12 python -m scripts.fourgeeks_stream_probe --execute
```

La sonda pide un máximo de 96 tokens con datos sintéticos y emite contadores,
estado HTTP y resultado de cierre. Requiere deltas de contenido en al menos dos
lecturas de transporte y cancela con una lectura pendiente, antes de observar
un evento terminal. Una respuesta agrupada o una finalización que gana la
carrera se informa como resultado inconcluso para cancelación. El resultado
`local_cancellation_observed` acredita cancelación de la tarea de la sonda y
cierre local en HTTPX. La verificación de eventos WebSocket después de
interrumpir pertenece a la prueba integrada del agente.

La ejecución inicial de Codex devolvió `missing_credential`, código de
salida 2, y cero peticiones. La ejecución posterior del usuario en Docker
acreditó el transporte real y consta en el expediente externo.

### Prueba adicional del abort en LangGraph

El usuario ejecutó desde PowerShell de QA con la credencial inyectada:

```powershell
$env:TRACKFLOW_FOURGEEKS_AGENT_QA = "1"
uv run --offline --python 3.12 pytest 'tests/pipelines/test_fourgeeks_agent_abort_live.py::test_fourgeeks_agent_abort[live]' -q
```

Este caso usa el grafo y el cliente asíncrono reales, recuperación
sintética y una sola generación de hasta 96 tokens. Verifica que se abre
una respuesta del gateway, el abort se propaga al grafo y la respuesta
queda cerrada. La prueba de navegador incluye además identidad, persistencia,
turno independiente y reconexión. La publicación de deltas crudos requiere
resolver previamente el contrato de guardrails.

La validación de la respuesta completa bloquea la publicación anticipada de
texto libre. Algunas reglas dependen de un sufijo que puede llegar después de
un prefijo aparentemente inocuo, como una credencial cuyo valor sigue a un
separador con espacio variable. También se verifica la coherencia entre
campos de la respuesta final. Mantener la barrera completa hasta disponer de
una alternativa con garantías equivalentes. Una prueba directa del gateway
puede acreditar transporte sin exponer el contenido al Backoffice.

Por ejemplo, un prefijo sintético terminado en `Bearer ` puede pasar la
validación aislada y convertirse en un secreto bloqueado cuando llegan los
caracteres siguientes. Los espacios entre un marcador y su valor pueden
tener longitud variable. Además, la aceptación final depende de la longitud,
la estructura y relaciones entre ruta, componentes, propuesta y consentimiento.
Un buffer fijo de tokens o una validación por chunk no acreditan equivalencia.
Las alternativas para decisión humana son conservar el resultado completo
validado o definir unidades de publicación estructuradas, con proyecciones
autorizadas y garantías comprobables antes de emitir cada unidad. Cualquier
cambio del contrato de texto libre o de la semántica de rechazo final requiere
una decisión explícita.
Las unidades estructuradas tendrían que derivarse de campos autorizados y
tener una semántica independiente, incluso después de un abort. La ruta
actual produce prosa libre y decide ensamblado, fuentes y consentimiento
para la respuesta completa. Adoptar unidades exigiría un contrato de salida
distinto y una decisión académica sobre si sus emisiones satisfacen el
requisito de deltas auténticos.

Las identidades internas `admin|manager` pueden atender casos etiquetados
B2B y B2C cuyo acceso esté autorizado. En particular, el rol manager queda
sujeto a su membresía activa de cliente. Para ejecutar Support también se
requiere el token OIDC con `incidents:read` e `inventory:read`, vinculado al
mismo usuario interno que el JWT local. B2B/B2C describe el tipo de caso y
carece de una política de acceso autónomo para clientes o destinatarios.
La decisión pendiente es confirmar si la evaluación acepta que personal
autorizado atienda ambos tipos de casos. Si exige acceso directo, TrackFlow
debe aprobar la identidad, alcance por cliente y alcance por envío o
destinatario antes de habilitarlo.

## Decisión de publicación y aceptación de Iteración 04

La prueba real del usuario con LangGraph obtuvo **1 PASS en 5.38 s**.
Las assertions comprueban HTTP 200, al menos un lote de bytes con la
respuesta abierta y el turno activo, propagación del abort, cierre local,
una petición observada, traza `cancelled` y ausencia del checkpoint. La
recuperación es sintética. La sonda directa anterior acredita los deltas
de contenido. El cese del cómputo remoto requiere evidencia del proveedor.

Se mantiene la entrega terminal. La validación actual puede sustituir por
completo una respuesta rechazada. Incluso tras un prefijo inocuo, una
continuación puede introducir un carácter de control, superar el límite
de longitud, completar un patrón sensible o invalidar una relación entre
ruta, fuentes y consentimiento. Emitir el prefijo hace imposible conservar
el rechazo completo en todos los receptores. Un buffer fijo tampoco acota
los separadores de longitud variable que reconocen algunos patrones.

| Alternativa | Garantías | Lógica actual y contrato | Streaming visible | Abort | Complejidad y regresión |
| --- | --- | --- | --- | --- | --- |
| Validación acumulada con retención | Retener hasta el veredicto conserva el rechazo completo. Liberar prefijos antes del veredicto no lo garantiza. | Compatible con lógica y contrato al retener todo. | Ningún delta antes del final. | Descarta buffer privado. | Baja con retención completa, alta para demostrar equivalencia de liberación anticipada. |
| Unidades independientemente validables | Requieren tipos cerrados, proyecciones autorizadas y controles entre unidades. Una unidad ya aceptada tendría que conservar su validez aunque falle otra. | Cambia la semántica de rechazo total y el formato. | Unidades en tiempo real. El criterio de prosa token por token necesitaría revisión. | Retiene solo unidades aceptadas y emitidas. | Alta por nuevos contratos, renderer y pruebas de composición. |
| Hechos estructurados separados de prosa | Pueden emitirse hechos con autorización y semántica propia. La prosa conserva su barrera. | Cambia la proyección y cuándo se comunica cada dato. | Hechos tempranos y prosa terminal. | Mantiene hechos emitidos y descarta prosa en retención. | Media o alta. Riesgo de hechos fuera de contexto o de confundirlos con una respuesta completa. |
| Entrega terminal validada, adoptada | Conserva controles de texto, estructura y rechazo completo. | Mantiene lógica y contrato actuales. | SSE interno y una salida visible al finalizar. | Persiste el estado y el texto publicado, si lo hubo. | Baja. Sin cambios en el flujo de autorización. |

Reproducir deltas originales después de validación mantiene su procedencia,
pero sigue siendo entrega posgeneración. Dividir una respuesta completa
queda fuera del criterio de aceptación.

**Consulta propuesta para el evaluador sobre streaming**

> El briefing exige deltas visibles mientras se genera la respuesta. Los
> guardrails actuales exigen poder sustituir toda respuesta rechazada,
> incluso por una causa detectada al final. ¿Acepta la evidencia de SSE
> auténtico y cancelable con entrega visible tras validación? Si exige
> contenido temprano, ¿puede aprobarse un contrato de unidades tipadas y
> autorizadas que conserve las unidades ya aceptadas, o exige deltas de
> prosa del proveedor?

**Consulta propuesta para el evaluador sobre B2B/B2C**

> El briefing identifica como usuarios objetivo a clientes B2B y
> destinatarios B2C. El Backoffice requiere una identidad interna y el
> productor WS restringe la generación a un manager con un único cliente
> activo coincidente, JWT local y OIDC correlacionados. ¿La evaluación
> permite atender ambos tipos de caso mediante ese manager o exige acceso
> directo? Si exige acceso directo, TrackFlow debe aprobar por separado
> las identidades y el alcance por cliente B2B y por envío o destinatario B2C.

### Verificación reproducible de la integración

Con `TRACKFLOW_CHAT_TEST_DATABASE_URL` apuntando a PostgreSQL QA desechable,
desde la raíz del worktree:

```powershell
$env:TRACKFLOW_CHAT_TEST_ALLOW_DDL = "1"
uv run --offline --python 3.12 pytest 'tests/test_websocket_chat_postgres.py::test_websocket_agent_abort_and_reconnect_with_postgres_and_controlled_sse' -q
```

Esta prueba usa el endpoint WebSocket, PostgreSQL y LangGraph reales, con
OIDC, recuperación y gateway controlados. Comprueba cierre antes de
confirmar el abort, turno independiente, snapshot con el mismo cursor,
uso del historial al reconectar y ausencia del patrón sensible en socket
e historial. El caso browser + gateway real queda pendiente.

Para aceptación manual con el ambiente QA ya configurado, conservar
PostgreSQL migrado, Qdrant, OIDC, MCP y secretos por inyección de proceso.
Desde la raíz levantar la API local con un solo proceso:

```powershell
uv run --offline --python 3.12 uvicorn services.api.main:app --host 127.0.0.1 --port 8000
```

Desde `uis/backoffice`, con dependencias y configuración OIDC QA:

```powershell
npm run dev -- --port 3001
```

Ejecutar los escenarios 2 y 3 con un manager QA de cliente único. Para
continuidad, iniciar con un antecedente sintético que seleccione una de
dos políticas QA pertinentes y comparar la respuesta a la misma pregunta
tras reconectar y desde una sesión nueva. Registrar sesión, cursor,
generaciones y dependencia del antecedente, excluyendo el frame de
autenticación. Ejecutar el escenario 1 cuando se resuelva el contrato.
