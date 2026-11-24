# Hito 9 — cierre académico integrado

Fecha de corte: 2026-11-24. Este informe distingue la integración académica de
la validación del proveedor externo y de la preparación productiva.

## Resumen ejecutivo

La ruta productiva de Parte 3 conecta el handoff persistido de Parte 2 con
revisiones departamentales, assessments de los tres triggers, ramas LangGraph
independientes, decisiones autenticadas, propuesta global, fan-in y
FinalDocument. La última aprobación puede materializar automáticamente la
propuesta sin suplantar a un responsable: el manager debe delegar expresamente
esa operación al iniciar el run y su grant `manage_approval_run` se vuelve a
validar bajo lock antes de escribir.

El Backoffice consume ahora la bandeja y los endpoints HITL reales. Un usuario
solo ve revisiones actuales de los departamentos para los que conserva un grant
efectivo; antes de decidir consulta preview seguro, evidencia y procedencia. El
cliente no envía principal ni authorization id.

El E2E académico integrado de ModaViva terminó con **PASS y exit code 0**.
Utilizó el PDF oficial, PostgreSQL, Redis, relay, Celery, API, LangGraph,
autenticación y persistencia reales en infraestructura QA aislada. La frontera
LLM de Parte 1 fue `controlled_local_stub` y Parte 2 fue determinista. Esto no
acredita FourGeeks real end-to-end ni declara TrackFlow listo para producción.

## Matriz de requisitos

| Requisito oficial | Estado | Evidencia principal | Límite restante |
| --- | --- | --- | --- |
| Recibir una RFP y conservar el handoff de Partes 1/2 | Acreditado en el E2E académico | PDF ModaViva, HTTP, worker y handoff persistido | P1 usó `controlled_local_stub`. |
| Un agente/revisión por departamento aplicable | Acreditado | Warehouse y Reverse independientes en PostgreSQL/checkpoints | No acredita operación multi-instancia. |
| Aprobaciones humanas independientes e interrumpibles | Acreditado | Threads namespaced, HTTP 422/403 y dos decisiones aplicadas | Actores QA sintéticos. |
| Identidad y autorización departamental | Acreditado en QA aislado | Grants persistidos, inbox y endpoints autenticados | No sustituye auditoría productiva. |
| Detección de `volume-vs-capacity`, `returns-sla-breach` y `currency-mismatch` | Cumplido y probado offline | `services/rfp_approval/assessment_coordinator.py`; `tests/test_rfp_approval_assessment_coordinator.py` | Evidencia con los datos reales de ambos PDFs. |
| Información insuficiente distinta de contradicción | Cumplido y probado offline | Missing capacity produce `insufficient_information`, no conflicto fabricado | Persistencia PostgreSQL de ese recorrido en E2E. |
| Arbitraje como nodo real y árbitros oficiales | Implementado y con gate PostgreSQL previo aprobado | `pipelines/rfp_approval/arbitration.py`; suites de arbitraje | Integrarlo en un escenario oficial que realmente dispare conflicto. |
| Propuesta global coherente después de las aprobaciones | Acreditado | Delegación revalidada y una propuesta persistida | Propuesta indicativa, no cotización vinculante. |
| Fan-in y FinalDocument único | Acreditado | Coordinación automática y un documento | FourGeeks real no fue la frontera P1. |
| Reconciliación después de interrupción | Acreditado contractualmente | Replay HTTP 409 y reconcile HTTP 200 sin duplicados | No se afirma un despliegue productivo multi-instancia. |
| Trazabilidad, hashes, eventos y replay | Acreditado | Manifest, JSONB y `payload_hash` verificados | Evidencia privada permanece fuera de Git. |
| Backoffice aprobar/rechazar/solicitar cambios | Acreditado por build, contratos y UI01 | Bandeja real y FinalDocument descargable | UI01 es QA, no producción. |
| Documento final en Markdown y proyección pública | Acreditado | Documento indicativo con pendientes globales/departamentales | Luna no formó parte del E2E académico integrado. |

Clasificación global: la funcionalidad obligatoria y el recorrido académico
integrado están acreditados. FourGeeks real end-to-end y operación productiva
permanecen fuera de esta evidencia.

## Secuencia operativa

1. `POST /rfps` recibe el PDF.
2. `POST /rfps/{ticket_id}/response` ejecuta Parte 2.
3. `POST /rfps/{ticket_id}/approval` deriva Parte 3 desde el handoff. El manager
   puede activar `automate_proposal_materialization=true` únicamente con
   `manage_approval_run` vigente.
4. `POST .../business-inputs` registra hechos faltantes por su responsable.
5. `POST .../assess-and-start` persiste assessments y abre solo ramas libres.
6. `GET /rfps/approval/inbox` y `GET .../review` muestran trabajo autorizado.
7. `POST .../decision` reanuda exactamente una rama con identidad autenticada.
8. La última aprobación ejecuta el nodo de coordinación. Si la delegación sigue
   vigente, importa la propuesta exacta, reconstruye fan-in y finaliza.
9. `POST .../reconcile` recupera coordinación incompleta sin otra decisión.
10. `GET .../document` entrega la proyección pública del documento persistido.

## Seguridad del cierre automático

- El opt-in no se deriva de ser admin ni de aprobar un departamento.
- El run conserva principal, binding, rol, grant y versión del manager.
- La materialización revalida ambiente, identidad, asignación oficial,
  revocación, expiración y versión bajo lock.
- La operación solo importa contenido global sin cambios. Una corrección
  comercial sigue requiriendo input humano autorizado y evidencia nueva.
- Runs antiguos, no delegados o con autoridad obsoleta permanecen esperando.
- La finalización sigue comprobando fuentes actuales, hashes, conflictos y
  aprobaciones aplicadas antes de crear un documento único.

## Estado del Backoffice

La página existente conserva la recepción administrativa y añade la bandeja de
aprobación. Usuarios no admin autenticados acceden a la bandeja, pero su
contenido lo determina PostgreSQL, no el rol visual. El panel muestra preview
seguro, evidencia, procedencia, hash y bundle; exige acknowledgement y motivo
para rechazo/cambios. Las tres acciones usan el endpoint HITL real.

El build de producción de Backoffice y TypeScript terminó correctamente. La
revisión manual separada de UI01 confirmó un ticket `terminado`, Warehouse
aprobado, Reverse corregido y aprobado, Approval Run `completed` y un
FinalDocument visible y descargable. UI01 permaneció separado del E2E oficial
y del entorno académico desechable.

## E2E oficial

`docs/operations/rfp-approval-e2e.md` define el preflight, flujo API-only,
evidencia y aceptación para:

- ModaViva: Warehouse + Reverse, EUR.
- Luna Cosmetics: Warehouse + Last Mile, USD.

Los inputs comerciales autorizados pueden enriquecer la propuesta. Cuando no
existen, el flujo académico conserva pendientes allowlisted y exige
acknowledgement exacto; nunca convierte órdenes/SKU a m³ ni inventa capacidad,
tarifas, cotizaciones o porcentajes. El E2E académico integrado fue ejecutado;
los escenarios oficiales con FourGeeks real no quedaron acreditados de punta a
punta.

## Resultado de QA

- Focal API/read model/contratos/Backoffice: **71 PASS**.
- Estados públicos Parte 3/Backoffice: **23 PASS**.
- Partes 1 y 2 RFP: **156 PASS, 16 SKIP**.
- Runner E2E oficial: **4 PASS offline, 2 SKIP de red**; seis recopilados.
- Packaging/configuración: **35 PASS**; `services.rfp_approval*` incluido.
- Parte 3 completa: **424 PASS, 85 SKIP, 0 FAIL, 0 ERROR**.
- `git diff --check`: sin errores de whitespace; advertencia preexistente de
  conversión LF/CRLF en `uv.lock`.
- Ruff: no disponible; no se instaló.
- TypeScript: `tsc --noEmit` y `next build --webpack` **PASS** en copia temporal
  con las dependencias locales exactas; no se instalaron paquetes.
- E2E académico integrado ModaViva: **PASS, exit code 0** con Parte 1
  `controlled_local_stub`, Parte 2 determinista e infraestructura real.
- Finalización integrada: una propuesta, un FinalDocument, dos decisiones
  aplicadas; replay HTTP 409, reconcile HTTP 200 y hashes verificados.
- Revisión manual UI01: ticket terminado y FinalDocument descargable.

## Riesgos y bloqueos

1. FourGeeks real no completó el pipeline end-to-end; las pruebas focales de
   Classification y Metadata no sustituyen esa evidencia.
2. Parte 2 usa el proveedor determinista en el gate académico.
3. El checkpointer operativo actual es SQLite; un despliegue multi-instancia
   requerirá el checkpointer PostgreSQL ya previsto por la arquitectura.
4. La evidencia QA no constituye una certificación de producción.

## Pasos de demostración real

1. Crear infraestructura QA aislada y aplicar P1 → P2 → P3 → S1 → volumen
   mensual → bundle indicativo.
2. Persistir identidades/grants QA separados de producción.
3. Levantar API, worker y runtime de checkpoints con configuración fail-closed.
4. Seguir el runbook para ModaViva; capturar interrupts, decisiones, propuesta,
   documento, manifest y replay.
5. Repetir para Luna solamente con autorización separada, sin reutilizar IDs ni
   datos comerciales.
6. Reiniciar API entre aprobación y reconcile; comprobar ausencia de duplicados.
7. Ejecutar el Backoffice ya compilado y completar la prueba visual contra esa
   API.
8. Conservar stdout/stderr, exit code, timestamps, hashes y evidencia SQL de
   solo lectura fuera del repositorio.

## Borrador de descripción del PR

### Qué cambia

- Implementa contratos, persistencia, identidad/autorización, artifacts e
  inputs comerciales de Parte 3.
- Añade HITL LangGraph por departamento, arbitraje, fan-in, finalización y
  reconciliación.
- Conecta el handoff de Parte 2 mediante inicialización transaccional y
  assessments oficiales.
- Añade delegación explícita y revocable para materializar la propuesta global
  al completar aprobaciones.
- Expone entrypoints autenticados y conecta el Backoffice a decisiones reales.
- Añade runbook del E2E oficial y suites offline/integración con guardrails.

### Cómo se validó

El E2E académico integrado terminó PASS con exit code 0. Usó infraestructura y
persistencia reales, `controlled_local_stub` en Parte 1 y modo determinista en
Parte 2. Verificó HITL, fan-in, FinalDocument, replay, reconcile, hashes e
idempotencia. FourGeeks real no fue acreditado end-to-end.

### Riesgos conocidos

FourGeeks end-to-end pendiente; SQLite no acredita operación multi-instancia;
el resultado académico no implica preparación productiva.

## Veredicto

**PASS académico integrado** y **GO para preparar el PR** después del
saneamiento Git y las regresiones focales. La descripción del PR debe conservar
la limitación FourGeeks y evitar cualquier afirmación de producción.
