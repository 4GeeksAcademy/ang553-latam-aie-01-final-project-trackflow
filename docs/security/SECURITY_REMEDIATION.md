# TrackFlow · Remediación de seguridad · Evidencia y trazabilidad final

27 de noviembre de 2026 · OWASP Top 10:2021.

## Correcciones y verificación

| ID | Categoría y cambio | Antes → después y evidencia reutilizada |
| --- | --- | --- |
| R01 | A01/A04/A08/A09 · Telemetría | Identidad y procedencia insuficientes para métricas → lectura admin/manager y señales de navegador separadas de eventos operativos `service=api`. Validaciones de lectores, productores y agregación en 130 PASS focales compartidos con R02.  |
| R02 | A01 · CSV de incidencias | Último resultado compartido en memoria → último resultado exitoso por usuario y contexto autorizado, acotado por proceso. Pruebas de concurrencia, aislamiento, CSV y comportamiento sin resultado. Evidencia manual con 100/95/5 y 2/2/0 (total/válidos/inválidos), satisfacción 3.06 y 5.00. Aislamiento de exportaciones corroborado técnicamente. |
| R03 | A04/A08 · Reporte semanal | Extracción no reutilizaba la política de procedencia → agrega fuentes operativas válidas. Pruebas PostgreSQL, extracción y publicación con scheduler. |
| R04 | A01 · Catálogo de proveedores | JWT común con permisos de mutación sin desglose → admin/manager lectura completa, alta y tarifa/estado; solo admin elimina; user proyección básica. Ambos alias, UI, contratos y regresión con 300 PASS de backend en la selección 2C, typecheck y lint focal.  |
| R05 | A06 · Redis y Next/Talent | Redis 7.4.1 → 7.4.12 por digest; versión y comportamiento Celery verificados. Talent Next 16.2.7 → 16.4.5 y sharp 0.35.5, con locks y build verificados.  |
| R06 | A05/A06/A08 · Builds y runtime | Contexto Compose/COPY incompatible → contexto acotado y builds Website/Backoffice. API de requisitos abiertos → lock con hashes, bases fijadas y backend bcrypt verificado en imagen. |
| R07 | A02/A07/A09 · JWT, secreto y reset | Vencimiento, propósito y secreto insuficientemente estrictos → verificación común HTTP/SSE/WS y política de longitud; reset devuelve respuesta uniforme, intenta invalidar enlace si falla el envío y minimiza logs. Fixtures sintéticos compatibles para pruebas y benchmark. 473 PASS de regresión focal, 10 PASS con PostgreSQL para transportes y 28 PASS de transportes cliente en 3B.  |
| R08 | A01/A04/A07 · MCP | Descubrimiento retenía herramientas generales → adaptador CX conserva solo las tres lecturas, falla cerrado y mantiene scopes separados en el MCP general. Suite MCP de 4A con 66 PASS, fixtures de scope y evidencia manual de stock. |
| R09 | A01/A07 · Replay, HTTP y WS | Se verificó la separación de historial ordinario y proyecciones adicionales de Knowledge con ownership/OIDC; el replay y las publicaciones conservan las revalidaciones. 2B verificó contrato y ajustó auxiliares de pruebas, 3B endureció sesión compartida y 3C revisó publicación. |
| R10 | A03/A04/A08 · Knowledge, CX, corpus y memoria | Knowledge carecía de validación al publicar → síntesis y referencias validadas, 503 sanitizado ante salida inválida y guardrails compartidos. Corpus canónico y memoria por consentimiento y ámbito verificados con fixtures y PostgreSQL/Qdrant aislados. |
| R11 | A04/A09 · Support, presupuesto y trazas | Se adelantó el timeout SQL y se unificó admisión/plazo por proceso para HTTP/WS. Se acotaron trazas y se mantuvo sanitización. Pruebas de presupuesto, cancelación, reconexión y PostgreSQL.  |
| R12 | A01/A04/A08/A09 · HITL/RFP | Controles ya existentes de grants, locks, idempotencia, decisiones y pendientes comerciales revisados y acreditados. E2E de RFP y ensayos de concurrencia con SQLite/PostgreSQL. La aceptación humana confirma Intake y Response deterministas. HITL, SSE y documento final conservan evidencia técnica sin cierre manual completo. |
| R13 | A02/A06/A07/A08 · Python | Se retiraron python-jose, ecdsa y transitivas de API/worker, conservando JWT local en PyJWT 2.15.1. Locks reconstruidos y cambios del decodificador trasladados a consumidores y fixtures; auditoría e interoperabilidad acreditadas.  |
| R14 | A06/A08 · JavaScript | Cambios focales de cuatro locks dentro de rangos, incluido Handlebars 4.7.10. Auditoría npm consultada en 3D con cero filas en las tres apps. El árbol raíz de desarrollo conserva 18 moderadas.  |
| R15 | A06/A08 · Imágenes y tooling | Digests para bases y builder, Go 1.26.11 para supercronic, Qdrant 1.20.2, Nginx exclusivo de MCP QA 1.30.8. Se acreditan consultas por digest, builds, smoke de Nginx y continuidad Qdrant. |
| R16 | A02/A05/A06/A08 · Contenedores | Overlay opt-in con aislamiento y permisos, UI no-root y rootfs de solo lectura, montajes explícitos para datos. Builds exactos de worker/scheduler pasaron en QA. 250 backend, 49 OIDC/SSE/WS con persistencia y E2E RFP/HITL verificados en 4A. [Servidor](SERVER_HARDENING.md) |

Los recuentos corresponden a selecciones distintas y en parte solapadas. Se conservan comandos, fixtures, incidentes y resultados originales en los informes enlazados. Las intervenciones de regresión y estabilidad ampliaron evidencia funcional. El diff de producto contiene las remediaciones y sus pruebas asociadas.

## Críticas publicadas y aplicabilidad

| Elemento | Evidencia de corrección | Aplicabilidad efectiva y límite |
| --- | --- | --- |
| Redis CVE-2025-49844 / GHSA-4789-qfc9-5f9q · crítica 9.9 | 7.4.1 → 7.4.12 (parche de la rama desde 7.4.6); imagen y Celery acreditados. | Requiere acceso al servicio y permisos pertinentes. La existencia de un listener público o de la condición explotable en TrackFlow no se acreditó. [Aviso Redis](https://github.com/redis/redis/security/advisories/GHSA-4789-qfc9-5f9q) |
| Next/Talent GHSA-2464-fhr4-w9xw · crítica 9.5 | Next 16.2.7 → 16.4.5 y sharp 0.35.5; Next corregido desde 16.4.1 según el informe. | Condicionada al optimizador de imágenes y al runtime/formato descritos por el mantenedor. La exposición efectiva en Talent no se observó. Website y Backoffice ya tenían la versión Next corregida. [Aviso Vercel](https://github.com/vercel/next.js/security/advisories/GHSA-2464-fhr4-w9xw) |
| Next/Talent GHSA-vcvr-r3jv-pc5j · crítica 9.5 | La actualización cubre el rango afectado. | Requiere uso del procesamiento OG descrito; la ruta necesaria no se identificó en el código Talent revisado. [Aviso Vercel](https://github.com/vercel/next.js/security/advisories/GHSA-vcvr-r3jv-pc5j) |
| python-jose CVE-2026-85394 / GHSA-3qf3-8w2g-rqmx · crítica publicada | Retirado de ambas resoluciones; PyJWT 2.15.1 preserva contrato HMAC. | El caso requiere condiciones de selección de algoritmo y claves asimétricas. JWT local usa HMAC y clave simétrica según la política revisada.  |
| Handlebars CVE-2026-106445 y CVE-2026-106446 · críticas publicadas | Cierre Jest 4.7.9 → 4.7.10. | Tooling de desarrollo; las condiciones incluyen plantillas o AST no confiables.  |
| Builder Go y Nginx exclusivo de MCP QA · filas críticas de escáner | 3D acredita cambio de digest y reducción a cero filas críticas/altas en esas consultas; 4A acredita builds. | Recuentos por paquete/binario, con repeticiones. Separar exposición de build, binario incorporado y gateway QA. |

Redis y Next/Talent son los dos casos principales para el apartado académico de severidad publicada con verificación de remediación. No se ejecutaron exploits ni se asignó una puntuación de TrackFlow. Los identificadores adicionales y recuentos corresponden al conjunto documentado en 3A y 3D, con su fecha y sus condiciones.

## Riesgos residuales y condiciones de cierre

- **Infraestructura efectiva.** SSH, firewall, permisos y TLS se aplicaron y verificaron en el laboratorio focal. El arranque en frío recuperó SSH, UFW y HTTPS y verificó la misma cuenta sintética con su perfil conservado. El antecedente de soft lockup limita la evidencia de estabilidad prolongada. Para otro destino siguen pendientes OIDC, proxy, red y alertamiento según su perfil. 4A resolvió el build exacto de worker y scheduler en QA. [Servidor](SERVER_HARDENING.md)
- **Críticas condicionadas que permanecen en inventario.** Pillow 12.3.0 en el marcador Python 3.10 y procesamiento FPX requieren una decisión si se propone ese runtime. QA Python 3.12 usa 13.0.0 y la API revisada no incluye Pillow. CVE-2025-68121 de Go TLS corresponde a `gosu` de la imagen PostgreSQL QA y la función observada solo cambia UID/GID. Mantener esos límites al seleccionar los artefactos de destino.
- **Otras dependencias.** Requests ligado a Resend, pip de la base API, paquetes de SO, npm integrado, ramas legacy y moderadas de Jest requieren seguimiento por aplicabilidad. Registrar nuevo digest o mitigación si cambia el despliegue.
- **Datos y agéntico.** CSV es caché por proceso. Admisión y trazas tienen límites por proceso. El clasificador estructural de síntesis no acredita fidelidad semántica general. Retención, clasificación y restauración de datos reales quedan condicionadas al destino. El esquema incompleto de telemetría del perfil QA inicial se registró como incidencia de entorno.
- **Integraciones externas.** CRUD real Talent, Resend y FourGeeks real tienen bloqueos de endpoint, cuenta autorizada y/o consumo. El alcance de la aceptación humana figura en el informe OWASP. [Auditoría final](OWASP_TOP10_AUDIT.md)

- **Recuperación del entorno unificado de aceptación.** Las verificaciones aisladas acreditan componentes y reconstrucción funcional de identidad, PKCE, canje y lectura MCP con nuevas sesiones. El Backoffice histórico y todas sus integraciones no se han restaurado juntos. El estado exacto H2, claves de firma y sesiones anteriores queda fuera de la recuperación acreditada.

Las confirmaciones manuales incorporadas abarcan acceso y sesión, Knowledge y CX, Agent Memory, Inventory y MCP, Suppliers, Telemetry y Reporting, incidencias y CSV, aislamiento entre clientes y RFP Intake/Response. El acta distingue estas observaciones de los resultados de las pruebas técnicas.

## Revisión de secretos y alcance

Se contrastaron los 69 archivos completos con el inventario y las copias del ZIP. Se verificaron el diff de cada archivo de trabajo contra HEAD y los cuatro archivos sin seguimiento como adiciones. La tabla siguiente clasifica los cambios por control o evidencia de QA asociados. Las cuentas de línea se obtuvieron de texto normalizado y pueden diferir de `git diff --numstat` por el algoritmo. Los cambios de formato de locks y el inventario se incluyeron en el alcance.

Se buscaron firmas de claves privadas, tokens de servicios, JWT, literales de autorización y URLs con userinfo en los 69 contenidos; además, asignaciones sensibles y tokens de alta entropía en líneas introducidas. Las coincidencias se resolvieron con contexto de código: referencias a variables, fixtures de autenticación, credenciales sintéticas de pruebas PostgreSQL y datos de integridad de builds. La coincidencia de alta entropía identificada pertenece a la prueba de longitud de secretos. Las URLs detectadas pertenecen al baseline en documentación y fixtures. La revisión no detectó secretos reales ni archivos ajenos al alcance en los 69 archivos. Su alcance es el conjunto de cambios respaldados y sus contenidos, con heurísticas y contraste manual; no se extendió al historial completo, a configuraciones privadas, a datos de ejecución ni a validación de credenciales.

| Ruta | Líneas incorporadas / retiradas | Trazabilidad |
| --- | ---: | --- |
| `.env.example` | 4 / 3 | R07,R16 |
| `agents/customer_support/mcp_tools.py` | 10 / 7 | R08 |
| `agents/customer_support/output_guardrails.py` | 7 / 0 | R10 |
| `agents/customer_support/tracing.py` | 13 / 3 | R11 |
| `apps/talent-pipeline-tracker/package-lock.json` | 690 / 664 | R05,R14 |
| `apps/talent-pipeline-tracker/package.json` | 2 / 2 | R05 |
| `docker-compose.yml` | 14 / 10 | R05,R15,R16 |
| `docs/agent-memory.md` | 6 / 3 | R10 |
| `infra/docker-compose.deploy.yml` | 119 / 0 | R16 |
| `infra/scheduler/Dockerfile` | 6 / 5 | R15,R16 |
| `mcps/trackflow/qa/codespaces/compose.yml` | 1 / 1 | R15 |
| `memory-bank/progress.md` | 49 / 0 | R01–R16 |
| `memory-bank/techContext.md` | 4 / 3 | R06,R07 |
| `package-lock.json` | 13 / 13 | R14 |
| `pipelines/extraction.py` | 9 / 5 | R03 |
| `pyproject.toml` | 0 / 1 | R13 |
| `scripts/benchmark_cache_candidates.py` | 1 / 1 | R07 |
| `services/Dockerfile` | 8 / 6 | R06,R13,R16 |
| `services/api/auth_security.py` | 46 / 26 | R07,R13 |
| `services/api/auth_services.py` | 3 / 4 | R07,R13 |
| `services/api/auth_settings.py` | 36 / 35 | R07,R13 |
| `services/api/chat/producer.py` | 98 / 78 | R09,R10,R11 |
| `services/api/chat/router.py` | 16 / 23 | R09,R07 |
| `services/api/email_service.py` | 2 / 3 | R07,R13 |
| `services/api/main.py` | 30 / 21 | R02,R04 |
| `services/api/models.py` | 4 / 2 | R04 |
| `services/api/requirements.lock` | 2799 / 0 | R06,R13 |
| `services/api/requirements.txt` | 2 / 2 | R06,R13 |
| `services/api/routes/auth.py` | 20 / 81 | R07,R13 |
| `services/api/routes/rag.py` | 22 / 1 | R10 |
| `services/api/routes/suppliers.py` | 50 / 15 | R04 |
| `services/api/routes/support.py` | 76 / 19 | R08,R10,R11 |
| `services/api/routes/telemetry.py` | 18 / 5 | R01,R03 |
| `services/api/telemetry_storage.py` | 8 / 3 | R01,R03 |
| `services/task_queue/Dockerfile` | 2 / 2 | R13,R15 |
| `services/telemetry/analysis.py` | 9 / 5 | R01,R03 |
| `tests/conftest.py` | 1 / 1 | R07,R13 |
| `tests/pipelines/test_customer_support_agent.py` | 20 / 0 | R10 |
| `tests/pipelines/test_customer_support_mcp_tools.py` | 31 / 1 | R08 |
| `tests/test_auth_forgot_password.py` | 90 / 59 | R07,R13 |
| `tests/test_auth_login.py` | 1 / 1 | R07,R13 |
| `tests/test_auth_me.py` | 1 / 1 | R07,R13 |
| `tests/test_auth_reset_password.py` | 1 / 1 | R07,R13 |
| `tests/test_auth_session_policy.py` | 140 / 0 | R07,R13 |
| `tests/test_auth_token_purpose.py` | 5 / 5 | R07,R13 |
| `tests/test_global_serialization_contracts.py` | 76 / 5 | R01,R02,R04 |
| `tests/test_inventory_orders.py` | 1 / 1 | R07,R13 |
| `tests/test_nightly_scheduler_infrastructure.py` | 10 / 2 | R15,R16 |
| `tests/test_rag_api.py` | 35 / 0 | R10 |
| `tests/test_rfp_e2e_isolated.py` | 1 / 1 | R05,R12 |
| `tests/test_rfp_hito9_academic_e2e.py` | 1 / 1 | R05,R12 |
| `tests/test_special_http_contracts.py` | 10 / 6 | R01,R02,R04 |
| `tests/test_suppliers.py` | 73 / 2 | R04 |
| `tests/test_support_api.py` | 56 / 2 | R08,R10,R11 |
| `tests/test_telemetry.py` | 1 / 0 | R01,R03 |
| `tests/test_telemetry_analysis.py` | 40 / 0 | R01,R03 |
| `tests/test_telemetry_report.py` | 27 / 1 | R01,R03 |
| `tests/test_websocket_chat.py` | 12 / 0 | R09,R10,R11 |
| `tests/test_websocket_chat_postgres.py` | 5 / 4 | R09,R10,R11 |
| `tests/test_weekly_extraction.py` | 39 / 3 | R03 |
| `uis/Dockerfile` | 24 / 3 | R06,R15,R16 |
| `uis/Dockerfile.dockerignore` | 29 / 0 | R06 |
| `uis/backoffice/app/suppliers/page.tsx` | 26 / 5 | R04 |
| `uis/backoffice/components/suppliers/SupplierList.tsx` | 19 / 14 | R04 |
| `uis/backoffice/package-lock.json` | 146 / 132 | R14 |
| `uis/backoffice/types/suppliers.ts` | 4 / 4 | R04 |
| `uis/start.sh` | 10 / 1 | R06,R16 |
| `uis/website/package-lock.json` | 146 / 132 | R14 |
| `uv.lock` | 60 / 114 | R06,R13 |

## Estado verificado para revisión

El alcance técnico incluye 69 rutas, con 65 modificaciones y cuatro nuevas. Se verificó la correspondencia byte a byte con el checkpoint del expediente, el índice vacío y la ausencia de errores de espacios según git diff --check. La verificación de secretos corresponde al conjunto respaldado y a sus cambios, con revisión de coincidencias de ejemplo y fixtures. La publicación sigue pendiente de autorización.
