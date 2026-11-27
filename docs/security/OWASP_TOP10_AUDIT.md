# TrackFlow · OWASP Top 10:2021 · Revisión final

27 de noviembre de 2026 · OWASP Top 10:2021.

## Resultado y método

Las 30 evaluaciones quedan consolidadas. Todas aplican a alguna superficie del carril indicado. Cada fila identifica el objetivo, el control acreditado y el límite de evidencia. Las rutas abreviadas `routes/`, `auth_*.py`, `operational_oidc.py`, `email_service.py` y `main.py` se refieren a `services/api/`. Los identificadores R01–R16 remiten al registro de remediación. Los resultados QA tienen el alcance de sus fixtures e imágenes; la exposición efectiva requiere evidencia del host. Se reutilizaron las pruebas originales y la evidencia manual disponible. [OWASP 2021](https://top10.owasp.org/2021/) · [Remediación final](SECURITY_REMEDIATION.md) · [Servidor](SERVER_HARDENING.md)

## Backend

| Categoría | Control esperado | Control y resultado a cierre | Evidencia del worktree y referencia de remediación | Riesgo residual o verificación pendiente |
| --- | --- | --- | --- | --- |
| A01 · Control de acceso roto | Autorizar cada recurso y productor | Reporte admin/manager, CSV por usuario, proveedores por rol; inventario y tracking conservan ámbito | `routes/telemetry.py:34`, `main.py`, `routes/suppliers.py:44–56` · R01–R04 | Cuotas CSV por proceso y alcance de destinos no ensayados; las dos identidades manuales conservan sus exportaciones |
| A02 · Fallos criptográficos | Firma y tránsito protegido | JWT con secreto obligatorio de longitud acorde al algoritmo, bcrypt y locks de runtime | `auth_settings.py:63`, `auth_security.py:32–44` · R06–R07 | HTTPS efectivo, entrega y rotación de claves requieren el host autorizado |
| A03 · Inyección | Parametrizar SQL y validar archivos | Filtros SQL ORM, RFP valida firma/tamaño, parser CSV tipado | `services/incidents/repository.py:40`, `services/rfp_intake/artifacts.py:17`, `services/api/main.py` | Revisar límite de carga efectivo de CSV y memoria en ingress; evidencia previa sin inyección acreditada |
| A04 · Diseño inseguro | Separar fuentes y acotar procesamiento | Agregados operativos internos; RFP usa límites, grants y estados de confirmación | `routes/telemetry.py:40`, `routes/rfp_notifications.py`, `services/telemetry/analysis.py` · R01,R03,R12 | Medir tamaño de ventana, saturación y límites en el destino si es necesario |
| A05 · Configuración insegura | Distinguir desarrollo y entrega | Overlay opt-in con listeners loopback, API no-root, rootfs y red de datos | `infra/docker-compose.deploy.yml`, `services/Dockerfile` · R16 | Aplicar host, verificar CORS, TLS, alcance de documentación API y proxy |
| A06 · Componentes vulnerables u obsoletos | Usar artefactos inventariados | Redis corregido, runtime API congelado, python-jose retirado y builds exactos QA | `docker-compose.yml`, `services/api/requirements.lock`, `uv.lock` · R05,R06,R13–R16 | Requests, pip, SO y variantes Python 3.10 quedan registrados; confirmar artefacto destino |
| A07 · Fallos de identificación y autenticación | Autenticar sesión por propósito y vigencia | JWT exige exp y propósito; reset uniforme y de uso único; verificar OAuth/OIDC por superficie | `auth_security.py:129–153`, `routes/auth.py`, `operational_oidc.py` · R07 | Rate limits del edge y entrega Resend quedan por acreditar |
| A08 · Fallos de integridad de software y datos | Probar procedencia de eventos y builds | Operativos desde API; frontend identificado por separado; hashes y digests donde corresponden | `routes/telemetry.py:91`, `services/api/requirements.lock`, `services/task_queue/Dockerfile` · R01,R03,R06,R14–R16 | Acreditar procedencia e inventario desplegados; migraciones persistentes fuera del QA |
| A09 · Fallos de registro y monitorización | Registrar con minimización y correlación | Auth y correo registran estados fijos; middleware correlaciona solicitudes; agregados operativos conservados | `routes/auth.py`, `email_service.py`, `main.py` · R01,R07 | Verificar redacción de rutas, retención y alertas del destino |
| A10 · SSRF | Fijar destinos de salida | OIDC/JWKS, proveedor y MCP usan configuración; alcance estático con destinos aprobados | `operational_oidc.py`, `services/rag/config.py`, `mcps/trackflow/trackflow_mcp/settings.py` | Egress, DNS y TLS del entorno pendientes; sin destino arbitrario observado desde entrada HTTP |

## Frontend

| Categoría | Control esperado | Control y resultado a cierre | Evidencia del worktree y referencia de remediación | Riesgo residual o verificación pendiente |
| --- | --- | --- | --- | --- |
| A01 · Control de acceso roto | Autorizar datos en API | Backoffice proyecta catálogo según rol; guarda de UI y API verificadas en QA; Talent conserva Bearer | `uis/backoffice/app/suppliers/page.tsx`, `uis/backoffice/lib/authFetch.ts`, `apps/talent-pipeline-tracker/services/candidatesApi.ts` · R04 | CRUD y controles de la API externa de Talent pendientes de autorización |
| A02 · Fallos criptográficos | Proteger credenciales y tránsito | Support mantiene token OIDC en memoria; Backoffice y Talent conservan JWT local en almacenamiento del navegador | `uis/backoffice/lib/supportOidc.ts`, `uis/backoffice/lib/auth.ts`, `apps/talent-pipeline-tracker/lib/auth.ts` | HTTPS/WSS del host y exposición condicionada al origen; alcance de almacenamiento local queda documentado |
| A03 · Inyección | Renderizar texto por contexto | React escapa texto en flujos revisados; JSON-LD de la página y Blob de descarga delimitados | `uis/website/app/page.tsx`, `uis/backoffice/app/backoffice/rfps/page.tsx` | Cobertura estática acotada; revaluar si cambia el render de HTML o Markdown |
| A04 · Diseño inseguro | Mantener estado por sesión e identidad | AuthProvider y PKCE; historial y contratos de reconexión; formulario Website con confirmación local | `uis/backoffice/lib/chatState.ts`, `uis/backoffice/lib/supportOidc.ts` · R02,R09,R16 | Incidents limpia la vista al cambiar sesión; se confirmó independencia en servidor |
| A05 · Configuración insegura | Controlar configuración, headers y edge | Builds dev/production; Backoffice y Website como UID 1000, caché tmpfs y rewrites QA | `uis/Dockerfile`, `uis/backoffice/next.config.ts`, `infra/docker-compose.deploy.yml` · R06,R16 | Verificar headers y CSP efectiva, proxy SSE/WS y certificado del destino |
| A06 · Componentes vulnerables u obsoletos | Resolver y acreditar dependencias | Talent Next 16.4.5 y sharp 0.35.5; locks npm posteriormente auditados; las tres apps sin filas en consulta 3D | Locks de Talent, Backoffice, Website y raíz · R05,R14–R15 | Tooling de desarrollo raíz y npm de imagen conservan avisos; certificar digest del destino |
| A07 · Fallos de identificación y autenticación | Acreditar login, callback y expiración | Local y callback Keycloak informados; PKCE/state/expiry y JWT local gestionado por authFetch | `uis/backoffice/lib/supportOidc.ts`, `uis/backoffice/lib/authFetch.ts` · R07,R09 | Logout del IdP de destino, correo real y credenciales Talent conservan límites |
| A08 · Fallos de integridad de software y datos | Integridad de activos y telemetría | npm ci, integridades de locks, contexto acotado y fuente navegador diferenciada | `uis/Dockerfile`, `uis/Dockerfile.dockerignore`, `uis/backoffice/lib/telemetry.ts` · R01,R06,R14–R16 | Registrar procedencia del build y artefactos utilizados en entrega |
| A09 · Fallos de registro y monitorización | Registrar señales con propósito | Contrato de telemetría del navegador y procedencia separada para agregados | `uis/backoffice/lib/telemetry.ts`, `services/api/routes/telemetry.py:91` · R01,R03 | Sanitización y alertas efectivas, especialmente si payloads incluyen identificadores |
| A10 · SSRF | Restringir proxy server-side | Rewrites seleccionan backend por configuración; Talent consulta su API desde el navegador | `uis/backoffice/next.config.ts`, `apps/talent-pipeline-tracker/services/candidatesApi.ts:11` | A10 aplica al proxy Backoffice; verificar upstream y salida del edge autorizado |

## Sistema agéntico

| Categoría | Control esperado | Control y resultado a cierre | Evidencia del worktree y referencia de remediación | Riesgo residual o verificación pendiente |
| --- | --- | --- | --- | --- |
| A01 · Control de acceso roto | Autorizar recurso, scope, tenant y replay | Ownership y OIDC por contenido, memoria con alcance activo, adaptador CX solo tres lecturas | `services/api/chat/router.py`, `services/api/agent_memory/service.py`, `agents/customer_support/mcp_tools.py` · R08–R10 | Configuración y revocación del IdP en el host destino pendientes |
| A02 · Fallos criptográficos | Proteger credenciales y conexiones | MCP exige HTTPS o loopback; provider, Qdrant e IdP usan destinos configurados | `agents/customer_support/mcp_tools.py`, `services/rag/config.py`, `services/rag/clients.py` · R08 | Certificados, trust store y egress del despliegue pendientes |
| A03 · Inyección | Desacoplar instrucciones, datos y permisos | Prompt y herramientas delimitados; Knowledge y CX validan publicación, referencias y síntesis | `agents/customer_support/output_guardrails.py`, `services/api/routes/rag.py:90`, `routes/support.py` · R08,R10 | Fiabilidad semántica y prompt injection residuales del modelo; evidencia con corpus y fixtures acotados |
| A04 · Diseño inseguro | Aplicar mínimo privilegio y aprobación | CX solo lectura, memoria con consentimiento, HITL con grants y pendientes legítimos, admisión compartida HTTP/WS | `agents/customer_support/mcp_tools.py`, `services/api/agent_memory/consent.py`, `services/api/routes/rfp.py`, `routes/support.py` · R08,R10–R12 | Admisión por proceso. Sin aceptación manual completa de HITL, SSE ni documento final |
| A05 · Configuración insegura | Separar laboratorio y despliegue | Perfiles QA separados; Qdrant y Redis internos en overlay, proveedor determinista en QA | `infra/docker-compose.deploy.yml`, `services/api/chat/qa_units.py` · R15–R16 | Comprobar exposición y que el perfil destino no habilite fixtures QA |
| A06 · Componentes vulnerables u obsoletos | Seguir SDKs, locks y runtimes | Locks de API, raíz y MCP; Qdrant actualizado, regresiones RAG/MCP y runtime | `uv.lock`, `mcps/trackflow/uv.lock`, `docker-compose.yml` · R13–R16 | Verificar imágenes, SO y proveedor del entorno operativo |
| A07 · Fallos de identificación y autenticación | Acreditar vigencia y revocación | Sesiones WS/HTTP verifican JWT local y OIDC según contenido; MCP valida scopes y token exchange | `services/api/chat/router.py`, `services/api/support_oidc.py`, `mcps/trackflow/trackflow_mcp/token_exchange.py` · R08–R09 | Realm específico, logout y configuración del target mantienen sus límites |
| A08 · Fallos de integridad de software y datos | Acreditar corpus, memoria y decisiones | Qdrant cotejado contra corpus canónico; consentimiento, referencia y publicación estructurados; HITL durable | `services/rag/service.py`, `services/api/agent_memory/consent.py`, `services/api/routes/rfp.py` · R10–R12,R15 | Clasificación y retención del índice operativo y de artefactos de proveedores |
| A09 · Fallos de registro y monitorización | Emitir trazas mínimas y observables | Eventos categóricos, sanitización y límites de trazas; acciones RFP persistentes | `agents/customer_support/tracing.py`, `agents/customer_support/guardrail_observability.py`, `services/api/routes/rag.py` · R10–R12 | Alertas, cobertura durante saturación y retención quedan por comprobar en destino |
| A10 · SSRF | Autorizar destinos de red | MCP, RAG, Qdrant e IdP consumen URLs configuradas; validación MCP de esquema/host | `agents/customer_support/mcp_tools.py`, `services/rag/config.py`, `services/rag/clients.py` · R08 | Revisar DNS, Host, TLS y salida del entorno; sin selección arbitraria observada en el flujo revisado |

## Aceptación humana y evidencia técnica

Las observaciones corresponden a los recorridos confirmados por Usuario + ChatGPT en el Backoffice unificado. El laboratorio focal de Linux tiene evidencia técnica propia, con persistencia del hardening verificada tras arranque en frío.

| Recorrido | Resultado manual confirmado por Usuario + ChatGPT | Evidencia técnica o límite |
| --- | --- | --- |
| Identidad y sesión | Login local, autenticación Keycloak, consulta MCP, logout y protección de rutas. | Las pruebas técnicas de identidad, scopes y transportes se conservan en el expediente. |
| Knowledge y CX | El reintento devolvió 30 días de plazo de devolución, excepción contractual y referencias canónicas. | La corrección inicial fue de configuración y fixture QA. La evidencia técnica de recuperación y validación de referencias se conserva. |
| Agent Memory | Propuesta, rechazo, aceptación y recuperación observados. | El flujo manual acredita esos pasos en QA. Las pruebas de consentimiento y aislamiento tienen su propia trazabilidad. |
| Inventory y MCP | Entrada de 4 unidades, de 10 a 14. Salida por pérdida de 4, de 14 a 10. Inventory History mostró ambos movimientos. MCP confirmó 14 y luego 10. | El incidente HTTP 500 del perfil inicial se conserva como evidencia histórica. La aceptación posterior acredita los movimientos en el entorno unificado. |
| Suppliers | Alta, ajuste de tarifa y suspensión de un proveedor sintético. La vista de usuario mostró la proyección restringida. | Pruebas de ambos alias y permisos por rol. |
| Telemetry y Reporting | Acceso habilitado para roles autorizados y denegación para roles sin permiso. | Pruebas separadas de procedencia de eventos y agregación. |
| Incidents | Creación, consulta, cierre e historial de versiones de una incidencia sintética. | La misma incidencia se utilizó para observar el aislamiento entre clientes. |
| CSV de incidencias | Manager obtuvo 100 registros totales, 95 válidos y 5 inválidos, con satisfacción 3.06. User obtuvo 2 totales, 2 válidos y 0 inválidos, con satisfacción 5.00. | El backend corroboró el aislamiento de las exportaciones. La caché sigue siendo por proceso. |
| Aislamiento entre clientes | La segunda identidad quedó sin acceso al inventario, la incidencia y la consulta de producto del primer cliente. No se observó exposición de su memoria en el recorrido probado. | Observación limitada a las identidades y los datos sintéticos ensayados. |
| RFP | Intake y Response verificados con proveedor determinista. | HITL, SSE y documento final conservan su evidencia técnica o preparación del recorrido. La aceptación manual completa de esas etapas queda sin acreditar. |



## Aspectos de privacidad del briefing

El expediente acredita alcance y separación por identidad/scope, proyección de datos de proveedores, registros minimizados en flujos específicos y consentimiento, vigencia y revocación de memoria. El corpus QA cotejado corresponde a políticas compartidas. Para un destino con datos personales, siguen pendientes las evidencias sobre inventario y clasificación real, conservación y borrado en copias, ejercicio de derechos y tratamiento por terceros, según los aspectos pertinentes de RGPD y CCPA/CPRA. Este es un mapeo técnico del briefing.

## Matriz definitiva de cumplimiento académico

| Requisito del mandato | Estado | Evidencia o condición para el cierre |
| --- | --- | --- |
| Diez evaluaciones en cada uno de los tres carriles | PASS documental | 30 filas completas en este informe, A06/A08 actualizadas y referencias a pruebas y límites. |
| Correcciones críticas y evidencia reproducible | PASS para remediaciones documentadas por componente | Redis y Next/Talent tienen versión afectada, aviso del mantenedor, versión corregida y QA. Otros avisos críticos condicionados se detallan en remediación. La exposición efectiva de los dos casos en TrackFlow no está demostrada. Si se exige confirmación de explotabilidad en el despliegue, acordar explícitamente cómo se evalúa el criterio de dos correcciones. [Remediación](SECURITY_REMEDIATION.md) |
| Revisar y preservar el alcance del cambio | PASS dentro del worktree revisado | 69/69 archivos, trazabilidad individual, análisis focal de secretos sin hallazgos de credenciales reales y estado Git idéntico al checkpoint. [Remediación](SECURITY_REMEDIATION.md) |
| Regresiones y evidencia antes/después | PASS para casos ejecutados | Utilizar evidencias 2A–5B, conservando por separado los límites de la imagen histórica y los ensayos QA. |
| Hardening efectivo del servidor | PASS en laboratorio focal | Controles del host y persistencia tras apagado limpio y arranque en frío verificados. SSH, UFW, permisos, HTTPS y login con los mismos datos sintéticos acreditados. La estabilidad prolongada queda por acreditar. [Servidor](SERVER_HARDENING.md) |
| Aceptación humana | Evidencia incorporada / ratificación final pendiente | Recorridos confirmados incorporados al acta. HITL, SSE y documento final conservan su límite de aceptación manual. |
| Documentos técnicos y publicación del PR | PASS documental / publicación pendiente | Informes reconciliados y tres copias sanitizadas preparadas como candidatas a docs/security/. Propuesta de commits y PR a main. |
| Integraciones externas y mejoras opcionales | BLOCKED externo / NOT APPLICABLE al hito documentado | CRUD real de Talent, correo real y FourGeeks requieren precondiciones independientes. Los textos del mandato consultados no los enumeran expresamente como requisito del hito.  |

La consigna exige diez evaluaciones por carril, corrección de las críticas identificadas, evidencia defensiva antes y después, hardening del host y PR. Las correcciones de Redis y Next.js de Talent cuentan con severidad publicada y pruebas defensivas. La exposición efectiva en TrackFlow sigue sin acreditarse. Si el evaluador exige ese criterio, debe confirmarlo antes de la entrega. Están pendientes la ratificación del acta y la publicación autorizada del PR. Quedan fuera de la evidencia acreditada la recuperación integral del entorno unificado y la estabilidad prolongada de la VM.
