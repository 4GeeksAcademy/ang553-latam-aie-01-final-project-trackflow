# TrackFlow — Plan de finalización

**DRAFT — AWAITING USER AND ARCHITECT APPROVAL**

## Naturaleza del documento

Este documento es un borrador de planificación basado en el repositorio observado. No autoriza implementación, cambios de dependencias, ejecución de DDL, acceso a bases de datos ni avance de fase. El número y contenido final de fases requieren aprobación del usuario y ChatGPT.

## Base técnica observada

El paquete activo `pipelines/` ya contiene:

- extracción semanal de solo lectura con ventana ISO y evidencia auxiliar;
- transformación de cuatro KPIs, deduplicación y diagnósticos de calidad;
- validación, reconciliación y UPSERT de publicación;
- almacenamiento persistente de corridas y control semanal;
- lease con fencing token y coordinación transaccional;
- tratamiento conservador de commits inciertos y recuperación por metadatos.

También existen DDL contractuales bajo `infra/sql/` y pruebas unitarias/contractuales para extracción, transformación, publicación, run store, concurrencia y coordinación.

La implementación actual no equivale a una finalización integral:

- no existe un flujo Prefect 3 que una el ciclo completo;
- no se observó una CLI operativa del pipeline semanal;
- el FastAPI actual expone reporting de telemetría, no el reporting semanal de desempeño aquí planificado;
- la mayoría de garantías transaccionales del pipeline se validan con fakes o contratos estáticos;
- las pruebas PostgreSQL aisladas están condicionadas y algunas protecciones terminan en `SKIP` o fallo deliberado;
- no están demostrados de extremo a extremo locks, fencing, atomicidad, concurrencia, recuperación y persistencia real;
- el Memory Bank conserva estados históricos contradictorios, por lo que el código y las pruebas deben prevalecer como evidencia técnica, sin modificar dicho banco fuera de una autorización específica.

El único paquete activo es `pipelines/`, resultado de una reestructuración autorizada por el docente y confirmado como decisión arquitectónica permanente por Usuario + ChatGPT. El flow Prefect 3 y la CLI se implementarán en `pipelines/pipeline.py`; el comando preferido será `python -m pipelines.pipeline`. A1 menciona históricamente `data/pipelines/pipeline.py`, y la aceptación de la ruta activa por el evaluador continúa pendiente, pero esa divergencia no autoriza wrappers, copias, symlinks ni una segunda implementación. Durante este hito no se modificarán archivos dentro de `data/`.

La evidencia deberá distinguir tres niveles que no son intercambiables:

1. pruebas técnicas con fixtures sintéticos;
2. integración real con PostgreSQL aislado; y
3. cobertura y certificación de datos empresariales reales.

Los datos sintéticos no demuestran que los productores reales estén completos. Continúan abiertas la cobertura de `stock_threshold_triggered` e `inventory_discrepancy_detected` y la equivalencia comercial entre movimientos `dispatch` y pedidos.

## Fuentes y niveles de autoridad

La trazabilidad usa las fuentes en este orden. El indicador histórico `0/28` no se interpreta como 28 requisitos diferentes.

| Nivel | Fuente | Disponibilidad y uso |
|---|---|---|
| A1 — consigna oficial | **Hito — Pipeline de Desempeño de Negocio Resiliente (Parte 2 de 3)** | Proporcionada por el usuario mediante texto y capturas, y consolidada en C1.3. Es autoridad para Prefect 3, resiliencia, caché, idempotencia, CLI, ruta literal y evaluación. |
| A2 — BRIEFING oficial | **BRIEFING — TrackFlow (Pipeline de Desempeño de Negocio)** | Contrato empresarial transcrito directamente por el usuario en C1.3. Su enlace de referencia no pudo leerse directamente en esta sesión; no se afirma lectura remota del archivo. |
| A3 — diseño aprobado de Parte 1 | `data/pipelines/PIPELINE_DESIGN.md` | Disponible. Define KPIs, tabla, arquitectura y contratos de los tres endpoints; no debe confundirse con la rúbrica literal de Parte 2. |
| A4 — implementación observable | `pipelines/`, `infra/sql/`, `services/api/` y `tests/` | Disponible. Demuestra código y contratos de prueba existentes, no por sí solo cumplimiento oficial ni integración real. |
| A5 — contexto histórico | `memory-bank/techContext.md` y `memory-bank/progress.md` | Disponible, pero contiene estados acumulativos; se contrasta con A4. |

Referencia indicada para A2: `https://github.com/4GeeksAcademy/ai-engineering-syllabus/blob/main/content/contexts/06-telemetry-data-pipelines/data-pipelines/trackflow-pipeline-BRIEFING.md`. La URL devolvió 404 al consultarla durante C1.3; el plan usa estrictamente la transcripción del usuario como fuente oficial proporcionada.

Los requisitos A1/A2 se distinguen de salvaguardas adicionales del diseño y la implementación, como fencing tokens, reconciliación, cobertura certificada y tratamiento conservador de commits inciertos. Estas protecciones fortalecen el pipeline, pero no se presentarán como requisitos literales de la rúbrica salvo que A1 los exija.

## Contrato empresarial oficial A2

- **Entregable:** Reporte Semanal de Desempeño por Almacén y Cliente para Thomas y Ana, disponible la mañana del lunes.
- **Grano:** una fila por `(warehouse, client_id, week_start)`; `week_start` es lunes ISO UTC, `warehouse` es `los_angeles` o `zaragoza` y `client_id` es texto, sin imponer UUID.
- **Fuente:** `telemetry_events` exclusivamente de lectura.
- **Eventos exclusivos:** `inbound_order_created`, `outbound_order_created`, `stock_threshold_triggered` e `inventory_discrepancy_detected`. No se incorporarán otros eventos empresariales.
- **Campos calculados persistidos:** `inbound_units_count`, `outbound_orders_count`, `stockout_events_count`, `discrepancy_events_count` y `discrepancy_rate`.
- **Fórmula:** `discrepancy_rate = discrepancy_events_count / outbound_orders_count`, o `0` cuando el denominador sea cero, sin redondeo arbitrario ni reinterpretación.
- **Destino:** `reporting.weekly_warehouse_client_performance`, con unicidad `(warehouse, client_id, week_start)` y el esquema contractual ya presente en `infra/sql/reporting_weekly_warehouse_client_performance.sql`.
- **HTTP:** los tres endpoints obligatorios viven bajo `services/reporting/`; los routers no duplican lógica empresarial.
- **Protecciones:** nunca mezclar clientes, nunca escribir en `telemetry_events`, no modificar `services/telemetry/analysis.py` ni `GET /telemetry/report`.

El esquema A2 que debe preservarse contiene `id UUID`, `warehouse TEXT`, `client_id TEXT`, `week_start DATE`, los cinco campos calculados, `computed_at TIMESTAMPTZ` y `UNIQUE (warehouse, client_id, week_start)`. C1.3 no autoriza modificarlo.

Contrato HTTP oficial:

- `GET /reporting/weekly-warehouse-client-performance`: acepta `week_start` opcional; cuando se omite devuelve la última semana calculada/publicada apropiada. Responde con `week_start` y `entries`, cada entrada con las dos dimensiones y los cinco campos calculados.
- `GET /reporting/pipeline-runs/latest`: devuelve estado y metadatos de la última corrida.
- `POST /reporting/pipeline-runs`: solicita una ejecución manual del flow; no es un endpoint meramente hipotético ni de solo consulta. Su aceptación no implica que los KPIs ya estén publicados.

### Contrastes semánticos obligatorios

**Throughput outbound.** A2 exige contar `outbound_order_created`, cuya descripción empresarial se refiere a pedidos preparados y despachados. A3 justifica consultar `StockExit.exit_type` porque el productor actual emite el mismo evento para `dispatch` y `loss`; solo los `dispatch` confirmados contribuyen. Esta clasificación evita contar pérdidas como throughput y es compatible con la intención empresarial, pero existe una diferencia literal con contar todos los eventos. Además, no está certificada la relación uno-a-uno entre `StockExit dispatch` y pedido comercial. El algoritmo no se cambia en C1.3: Usuario + ChatGPT deben confirmar si prevalece el conteo literal A2 o la clasificación conservadora A3.

**Tasa de discrepancia.** Aunque la descripción conceptual puede sugerir pedidos asociados a discrepancias, el contrato computacional A2 es inequívoco: conteo agregado de eventos de discrepancia dividido por conteo outbound, o cero. Se conserva esa fórmula basada en eventos; no demuestra asociación individual entre una discrepancia y un pedido y no se sustituirá por otra métrica.

## Matriz de trazabilidad de requisitos

Estados permitidos: **No implementado**, **Implementado sin validación real**, **Validado con mocks o pruebas unitarias**, **Validado mediante integración real**, **Bloqueado** y **Pendiente de confirmar fuente**. Ninguna fila se considera cumplida solo porque exista código.

| ID | Requisito | Fuente exacta y autoridad | Estado actual | Archivo o función responsable | Evidencia existente | Prueba de aceptación necesaria | Fase | Dependencia o bloqueo |
|---|---|---|---|---|---|---|---|---|
| RQ-01 | Prefect 3 con al menos un `@flow` y tres o más `@task`; extracción, transformación y carga son tasks independientes. | A1 oficial; A3 § Prefect Orchestration; decisión arquitectónica permanente. | No implementado | `pipelines/pipeline.py`, reutilizando el núcleo raíz | No hay dependencia Prefect ni decoradores; existe un test estático que afirma su ausencia en el coordinador. | Inspección de `pipelines/pipeline.py`, tests del flow, ≥3 tasks y ejecución controlada. | F3 | Dependencias Prefect autorizadas; aceptación del evaluador pendiente. |
| RQ-02 | Operaciones externas con `retries > 0`, `retry_delay_seconds` y justificación; nunca reintentar automáticamente un COMMIT incierto. | A1 oficial; A3 Prefect; salvaguarda A4 para commit incierto. | No implementado | Tasks externas futuras; `pipelines/coordinator.py` conserva `commit_uncertain` | Manejo conservador de commit ya existe fuera de Prefect. | Tests por task de retries/delay y prohibición de retry post-COMMIT incierto. | F3 | Política y versión Prefect. |
| RQ-03 | Task opcional ejecutada con `return_state=True`; su fallo se tolera sin ocultarlo. | A1 oficial. | No implementado | Flow Prefect futuro | No existe implementación. | Test de fallo opcional, state retornado, evidencia visible y continuidad controlada. | F3 | Definir cuál task es verdaderamente opcional. |
| RQ-04 | Caché de transformación mediante `cache_key_fn` y `cache_expiration`, reutilizando resultados seguros de la última hora. | A1 oficial; A3 diseño de orquestación. | No implementado | Task de transformación que delegue en `pipelines/weekly_performance.py` | No hay caché Prefect. | Tests de clave estable, expiración de una hora y no reutilización ante inputs, semana, reglas o evidencia diferentes. | F3 | Confirmar API exacta en la versión Prefect 3 seleccionada. |
| RQ-05 | ETL idempotente apoyado en `UNIQUE (warehouse, client_id, week_start)`. | A1 oficial; A2 destino; A3 Load. | Validado con mocks o pruebas unitarias | `pipelines/publication.py`; DDL oficial | UPSERT, reconciliación y tests de insert/update/no-op. | Integración PostgreSQL aislada con repetición y preservación de identidad. | F2 | Base desechable autorizada. |
| RQ-06 | Registrar por corrida inicio, fin, registros procesados, estado y errores. | A1 oficial; A3 run history. | Validado con mocks o pruebas unitarias | `pipelines/run_store.py`; `reporting_pipeline_control.sql` | Modelos/SQL y tests de run store; referencias Prefect opcionales. | Integración real para éxito, bloqueo y fallo con conteos y errores. | F2/F3 | Alinear conteos oficiales con campos actuales. |
| RQ-07 | CLI completa mediante `python -m pipelines.pipeline`; A1 conserva como referencia histórica `python data/pipelines/pipeline.py`. | A1 oficial; decisión arquitectónica permanente. | No implementado | `pipelines/pipeline.py` | No existe todavía la entrada CLI raíz; no se creará compatibilidad bajo `data/`. | Ejecución reproducible desde raíz con `python -m pipelines.pipeline`, inspección del módulo y códigos de salida. | F4 | Runner estable; aceptación del evaluador pendiente. |
| RQ-08 | Documentar el comando preferido y la frecuencia semanal, explicando la divergencia histórica. | A1 oficial; A2 frecuencia de lunes; decisión arquitectónica permanente. | No implementado | Documentación futura del módulo raíz | No existe documentación del comando completo. | Revisión de comando, argumentos, frecuencia, ejemplos, seguridad, estados y nota de ruta. | F4 | RQ-07. |
| RQ-09 | Escribir en la tabla oficial sin alterar telemetría técnica. | A1 oficial; A2 contrato y restricciones; A3 Load. | Validado con mocks o pruebas unitarias | `pipelines/publication.py`; DDL oficial | Tabla y DDL correctos; pruebas contractuales; separación técnica inspeccionada. | PostgreSQL real y regresión que proteja `telemetry_events`, `services/telemetry/analysis.py` y `/telemetry/report`. | F2/F6 | PostgreSQL aislado. |
| RQ-10 | Producir los cuatro KPIs y cinco campos calculados con grano, dimensiones, eventos y fórmula A2. | A1 valores coherentes; A2 contrato empresarial; A3 Aggregation. | Validado con mocks o pruebas unitarias | `pipelines/weekly_performance.py` | Suite unitaria de normalización, deduplicación, suma, conteos y tasa. | Integración aislada y evidencia separada de cobertura empresarial; validar `client_id` texto y ambos almacenes. | F2/F6 | Decisión outbound; productores incompletos. |
| RQ-11 | GET semanal con `week_start` opcional y fallback a última semana publicada apropiada. | A1 endpoints; A2 contrato HTTP; A3 Endpoint A. | No implementado | `services/reporting/` futuro | Diseño y ejemplo contractual; no hay ruta. | HTTP/OpenAPI, forma `week_start` + `entries`, autorización, errores, decimal y PostgreSQL. | F5 | Publicación estable y permisos. |
| RQ-12 | GET de última corrida con estado y metadatos. | A1 endpoints; A2 contrato HTTP; A3 Endpoint B. | No implementado | `services/reporting/` + `pipelines/run_store.py` | Run store existe; endpoint no. | HTTP/OpenAPI para estados, ausencia de corridas, errores y autorización. | F5 | Modelo de respuesta y permisos. |
| RQ-13 | POST manual que solicita una ejecución del flow. | A1 endpoints; A2 contrato HTTP; A3 Endpoint C. | No implementado | `services/reporting/` + solicitud durable + Prefect | A3 define request por semana, `202` solo tras registro durable, ausencia de conflicto y programación confirmada; 202 no implica publicación. | Tests HTTP, auth, 202/409/422/503, duplicados, concurrencia, timeout y reconciliación. | F5/F6 | Prefect, almacenamiento durable y permisos. |
| RQ-14 | Correspondencia con `PIPELINE_DESIGN.md` sin duplicar ETL y con implementación exclusiva en el paquete raíz. | A1 oficial; A3 diseño aprobado; decisión arquitectónica permanente. | Implementado sin validación real | `pipelines/` y futuro `pipelines/pipeline.py` | El núcleo refleja gran parte de A3; la ubicación fue reestructurada con autorización docente. | Matriz diseño→código, ausencia de duplicación y revisión de desviaciones. | F1/F6 | Aceptación del evaluador pendiente. |
| RQ-15 | Registrar y gestionar la divergencia entre la ruta histórica A1 y la ubicación obligatoria `pipelines/pipeline.py`, sin crear compatibilidad en `data/`. | A1 oficial; autorización docente histórica en A5; decisión permanente de Usuario + ChatGPT. | Bloqueado | Evidencia documental y comunicación con el evaluador; no corresponde crear código en `data/` | La arquitectura interna ya está decidida; falta aceptación formal externa. | Confirmación del evaluador de que `pipelines/pipeline.py` y `python -m pipelines.pipeline` satisfacen la evaluación. | F1/F4 | Bloqueo externo; no se resuelve mediante wrapper, symlink o copia. |
| RQ-16 | Atomicidad, locks, fencing y concurrencia demostrados con PostgreSQL real aislado. | Diseño de publicación/concurrencia, A3; código/tests, A4. | Implementado sin validación real | `pipelines/publication.py`, `concurrency.py`, `coordinator.py` | Fakes y contratos; pruebas reales están condicionadas o funcionan como guardas de no certificación. | Dos conexiones reales, rollback, atomicidad, fencing, idempotencia y recuperación. | F2 | PostgreSQL desechable autorizado. |
| RQ-17 | Distinguir fixtures técnicos, integración real y certificación de cobertura empresarial. | A2 restricciones; A3 § Cobertura; salvaguarda adicional A4. | Bloqueado | Gates de cobertura en `weekly_performance.py` y `coordinator.py`; productores externos | El código rechaza certificación sintética; faltan productores y equivalencia comercial. | Evidencia de productores reales, reconciliación y cobertura del periodo; nunca inferir cero de ausencia. | F6 | `stock_threshold_triggered`, `inventory_discrepancy_detected`, `client_id` y `dispatch`. |

La matriz integra todas las comprobaciones oficiales visibles proporcionadas en C1.3 dentro de RQ-01 a RQ-17. No se añaden IDs artificiales: extracción/transformación/carga independientes pertenecen a RQ-01; reutilización por una hora a RQ-04; frecuencia documental a RQ-08; y las restricciones empresariales a RQ-09/RQ-10. El indicador `0/28` no se interpreta como 28 obligaciones separadas.

## Trabajo restante propuesto

Las seis fases siguientes se conservan provisionalmente porque sus dependencias técnicas continúan justificándolas. Son una propuesta, no una autorización ni una cantidad inmutable para TrackFlow o futuros proyectos. La aprobación puede comprender el plan completo y permitir avance automático por sus gates, o puede dividir, combinar o reordenar fases si se conservan dependencias y controles.

### Fase propuesta: baseline ejecutable y contrato integral

**IDs principales:** RQ-14 y RQ-15; confirma fuente y dependencias de RQ-01 a RQ-13.

**Contratos empresariales afectados:** ninguno en ejecución; fija la correspondencia A1/A2/A3 y las dos decisiones semánticas sin cambiar KPIs.

**Archivos autorizables previstos:** `.codex/trackflow-completion-plan.md` durante planificación. La implementación futura usará exclusivamente `pipelines/pipeline.py`; `data/` queda fuera del alcance de escritura.

**Riesgos/autorizaciones:** el evaluador podría exigir la ruta literal histórica. Ese riesgo se gestiona mediante confirmación externa, no creando una segunda superficie técnica.

**Objetivo:** establecer el inventario verificable del pipeline y un contrato de ejecución completo antes de añadir orquestación.

**Entregables:**

- mapa del ciclo `extract → transform → quality gate → lease → publish → reconcile/recover`;
- definición de entradas, salidas, estados y códigos de error del runner;
- matriz de requisitos/rúbrica frente a código y pruebas existentes;
- estrategia autorizada para configuración, secretos y conexiones de prueba;
- selección explícita de la semana objetivo y política de reintentos.

**Criterios de aceptación:**

- cada requisito restante apunta a código o prueba existente, o queda marcado como pendiente;
- no se duplican cálculos de `weekly_performance.py` ni coordinación existente;
- se distinguen reglas de negocio, control transaccional y orquestación;
- las decisiones no resueltas aparecen en la sección de autorizaciones.

**QA automatizado:** inspecciones contractuales existentes y suite focalizada que el plan aprobado autorice.

**Gate de salida:** matriz fuente→requisito→prueba cerrada; regla de ubicación transmitida a implementadores; evidencia de aceptación o riesgo explícito del evaluador; ninguna duplicación de ETL.

**QA manual intermedio:** solo si resulta necesario para resolver una dependencia arquitectónica material que impida continuar.

### Fase propuesta: PostgreSQL aislado y garantías reales

**IDs principales:** RQ-05, RQ-06, RQ-09, RQ-10 y RQ-16.

**Contratos empresariales afectados:** tabla oficial, constraint único, cinco campos calculados, publicación por semana y separación de `telemetry_events`.

**Archivos autorizables previstos:** tests de integración focalizados, configuración aislada aprobada e `infra/sql/` únicamente si una corrección contractual recibe autorización expresa; no producción.

**Riesgos/autorizaciones:** DDL y datos desechables, credenciales de prueba, teardown y fault injection requieren autorización y aislamiento verificable.

**Dependencia:** contrato integral aprobado y entorno desechable autorizado.

**Objetivo:** sustituir afirmaciones basadas en fakes por evidencia de integración PostgreSQL real sin reutilizar producción ni `DATABASE_URL` empresarial.

**Entregables:**

- mecanismo reproducible para una base PostgreSQL aislada y desechable;
- aplicación controlada de los DDL requeridos;
- pruebas reales de inserción, actualización, no-op e idempotencia;
- pruebas de atomicidad entre UPSERT, metadatos y liberación del lease;
- pruebas con conexiones concurrentes para locks y fencing;
- escenarios de rollback y recuperación de commit incierto en la medida técnicamente reproducible.

**Criterios de aceptación:**

- la suite rechaza explícitamente bases no aisladas y nunca reutiliza producción;
- las pruebas de integración terminan en `PASS`, no `SKIP`, para las garantías obligatorias;
- dos conexiones no pueden publicar simultáneamente la misma semana bajo el mismo derecho;
- los fallos antes del commit no dejan una publicación parcial;
- los casos que no puedan simular una desconexión real quedan documentados como limitación, no certificados.

**QA automatizado:** suite PostgreSQL aislada y regresión focalizada del pipeline.

**Gate de salida:** pruebas obligatorias de esos IDs en `PASS` real sobre PostgreSQL aislado, con skips y limitaciones separados.

**QA manual intermedio:** solo si la inspección del aislamiento o teardown es necesaria para impedir riesgo real sobre una base no desechable.

### Fase propuesta: runner y orquestación Prefect 3

**IDs principales:** RQ-01, RQ-02, RQ-03, RQ-04 y la integración Prefect de RQ-06.

**Contratos empresariales afectados:** secuencia semanal, metadatos de corrida e idempotencia; no modifica fórmulas ni tabla.

**Archivos autorizables previstos:** `pipelines/pipeline.py`, módulos existentes bajo `pipelines/` solo cuando sea necesario, tests Prefect y archivos Python de dependencias expresamente aprobados. Ningún archivo bajo `data/`.

**Riesgos/autorizaciones:** aceptación externa de la ruta raíz, API exacta de Prefect 3, caché de una hora y retries inseguros tras resultados inciertos.

**Dependencia:** contrato integral estable; las garantías PostgreSQL críticas deben estar probadas o su riesgo aceptado explícitamente.

**Objetivo:** envolver la lógica existente en un flujo Prefect 3 sin trasladar ni duplicar reglas empresariales.

**Entregables:**

- `@flow` y `@task` de Prefect 3 para el ciclo semanal, con fronteras claras;
- propagación de `prefect_flow_run_id` y `prefect_task_run_id` a `run_store`;
- retries seguros para operaciones externas y una política acotada de timeouts;
- `cache_key_fn` y `cache_expiration` definidos conforme a los requisitos originales y confirmados contra la fuente oficial antes de implementar;
- manejo explícito de estados bloqueados, superseded y commit incierto;
- observabilidad suficiente para correlacionar una corrida sin registrar secretos.

**Criterios de aceptación:**

- Prefect 3 llama a los módulos existentes y no recalcula KPIs ni reimplementa publicación;
- ningún retry automático repite un commit incierto o una escritura no idempotente;
- los detalles de claves de caché y expiración tienen trazabilidad a la fuente oficial aprobada;
- un fallo conserva un estado operacional consultable;
- la ejecución repetida de una semana ya publicada produce el resultado idempotente definido;
- las pruebas distinguen task orchestration de integración PostgreSQL.

**QA automatizado:** pruebas unitarias del flow/tasks, estados, retries y correlación, más integración focalizada autorizada.

**Gate de salida:** flow y tasks cumplen los contratos confirmados, la caché es trazable y ningún retry puede repetir una operación insegura.

**QA manual intermedio:** solo cuando sea necesaria para resolver un riesgo de observabilidad o estado que el QA automatizado no pueda discriminar. La validación manual integral de Prefect se reserva para la fase final.

### Fase propuesta: CLI operativa

**IDs principales:** RQ-07, RQ-08 y documentación del bloqueo externo RQ-15.

**Contratos empresariales afectados:** frecuencia semanal, selección de semana cerrada y entrada reproducible al flow.

**Archivos autorizables previstos:** `pipelines/pipeline.py`, su documentación aprobada y tests CLI; no se duplicará ETL ni se modificará `data/`.

**Riesgos/autorizaciones:** `python -m pipelines.pipeline` debe ser ejecutable desde la raíz; la diferencia frente al comando histórico debe permanecer documentada hasta la aceptación del evaluador.

**Dependencia:** runner estable.

**Objetivo:** proporcionar una entrada reproducible para operadores y automatización.

**Entregables:**

- comando para ejecutar una semana explícita;
- ejecución CLI reproducible del flow mediante `python -m pipelines.pipeline`;
- comandos o modos de dry-run/validación y consulta de estado, si son aprobados;
- salida estructurada y códigos de proceso estables;
- documentación de variables requeridas y seguridad.

**Criterios de aceptación:**

- fechas inválidas o semanas no cerradas se rechazan antes de escribir;
- la CLI no imprime secretos ni acepta bypasses de calidad o lease;
- el resultado identifica `run_id`, estado, conteos y limitaciones relevantes;
- los códigos de salida distinguen éxito, bloqueo, conflicto y error.

**QA automatizado:** parsing, validación, códigos de salida y delegación al runner mediante dobles controlados.

**Gate de salida:** `python -m pipelines.pipeline` documentado y reproducible, sin lógica ETL duplicada, con códigos de salida probados y divergencia histórica reportada.

**QA manual intermedio:** solo si es necesaria para desbloquear una dependencia material; la validación integral de comportamiento CLI queda en la fase final.

### Fase propuesta: reporting FastAPI semanal

**IDs principales:** RQ-11, RQ-12 y RQ-13.

**Contratos empresariales afectados:** forma del reporte semanal, última corrida y solicitud manual del flow bajo `services/reporting/`.

**Archivos autorizables previstos:** nuevo módulo `services/reporting/`, registro mínimo en la app FastAPI existente y tests HTTP; no `services/telemetry/analysis.py` ni el router de `/telemetry/report`.

**Riesgos/autorizaciones:** auth/roles, serialización decimal, fallback de semana, comunicación Prefect, persistencia durable y recuperación de programación incierta.

**Dependencia:** esquema y semántica de publicación estables; decisiones de acceso aprobadas.

**Objetivo:** exponer resultados publicados, consultar la última corrida y permitir que un usuario autorizado solicite manualmente el flow sin trasladar ETL u orquestación a los routers.

**Entregables:**

- `GET /reporting/weekly-warehouse-client-performance`;
- `GET /reporting/pipeline-runs/latest`;
- `POST /reporting/pipeline-runs`, que solicita una ejecución manual del flow y devuelve aceptación solo bajo el contrato durable y concurrente aprobado;
- modelos Pydantic nominales y OpenAPI explícito;
- filtros de semana, almacén y cliente según la rúbrica autorizada;
- autorización, paginación cuando aplique, manejo de errores y contratos HTTP verificables coherentes con `services/api/`;
- consultas de solo lectura y límites de respuesta.

**Criterios de aceptación:**

- la API sirve únicamente datos válidamente publicados;
- no declara una corrida exitosa basándose solo en estado técnico inconcluso;
- contratos HTTP y OpenAPI son explícitos y están probados;
- los tres endpoints obligatorios tienen autorización, estados de error y contratos de respuesta verificables;
- filtros y límites temporales son deterministas;
- el POST no ejecuta el ETL dentro del request, no confunde `202` con publicación y protege duplicados, concurrencia y programación incierta.

**QA automatizado:** contratos, autorización, filtros, errores y HTTP real mediante ASGI; integración PostgreSQL aislada para consultas críticas.

**Gate de salida:** los tres endpoints tienen contratos nominales, autorización y errores probados; el POST conserva aceptación no bloqueante, concurrencia y recuperación conforme a la fuente confirmada.

**QA manual intermedio:** solo si es necesaria para resolver una ambigüedad contractual material. La revisión integral de `/docs`, respuestas y comportamiento se concentra en la fase final.

### Fase propuesta: validación integral y preparación de cierre

**IDs principales:** cierre transversal RQ-01 a RQ-17, con énfasis en RQ-09, RQ-10, RQ-13, RQ-14, RQ-16 y RQ-17.

**Contratos empresariales afectados:** todos los contratos A2, sin modificar su semántica durante el cierre.

**Archivos autorizables previstos:** solo correcciones dentro del alcance completo previamente aprobado, suites y documentación operativa autorizada; Project Learnings continúa fuera de esta fase.

**Riesgos/autorizaciones:** cobertura empresarial incompleta, skips, divergencia de ruta y diferencias outbound deben permanecer visibles si no se resuelven.

**Dependencia:** todas las fases aprobadas anteriores completadas.

**Objetivo:** validar el recorrido completo y producir evidencia suficiente para aprobación, sin hacer commit ni iniciar Project Learnings automáticamente.

**Entregables:**

- matriz final de criterios y evidencia;
- ejecución end-to-end controlada desde CLI/Prefect hasta PostgreSQL y API;
- regresión backend pertinente y análisis de skips/fallos;
- revisión de seguridad, secretos, logs, diff y documentación;
- inventario de riesgos residuales y decisiones de cierre.

**Criterios de aceptación:**

- cada criterio obligatorio tiene evidencia reproducible;
- no quedan `SKIP` tratados como `PASS` ni mocks presentados como integración;
- el E2E demuestra una publicación válida y una repetición idempotente;
- la API refleja únicamente la publicación confirmada;
- no existen cambios fuera del alcance aprobado;
- el usuario recibe estado Git y conserva la autoridad sobre staging, commit, push y merge.

**QA automatizado:** suites focalizadas, integración PostgreSQL, contratos HTTP y E2E autorizado.

**Gate de salida:** cada ID obligatorio confirmado por A1 tiene evidencia reproducible; la cobertura técnica no se presenta como certificación empresarial.

**QA manual integral obligatoria:** inspección conjunta de UX aplicable, Prefect, CLI, API/OpenAPI, comportamiento end-to-end, evidencia y riesgos residuales.

## Decisiones que requieren autorización

- aceptación formal del evaluador para `pipelines/pipeline.py` y `python -m pipelines.pipeline` frente a la ruta/comando históricos de A1; no se autoriza una alternativa bajo `data/`;
- decisión empresarial sobre si `outbound_orders_count` cuenta literalmente todos los `outbound_order_created` de A2 o conserva la clasificación `dispatch`/`loss` de A3; en ambos casos debe documentarse la limitación movimiento→pedido;
- topología del PostgreSQL desechable, aplicación y limpieza de DDL;
- archivos de dependencias que podrán modificarse para incorporar Prefect 3 y herramientas CLI;
- detalles internos del flow, runner y CLI dentro de `pipelines/pipeline.py`, sin cambiar su ubicación obligatoria;
- política de retries, caché, timeouts y recuperación de commits inciertos;
- autorización por roles, filtros adicionales y detalles de error de los endpoints; la existencia del POST manual ya es obligatoria por A1/A2;
- alcance del E2E y credenciales temporales autorizadas;
- ubicación externa del expediente temporal de evidencias;
- autorización de `C:\angel\trackflow-review-temp` como ubicación propuesta para ese expediente;
- actualización posterior de documentación protegida y Memory Bank;
- autorización de operaciones Git y cierre del hito.

Conforme a `AGENTS.md`, cualquier cambio futuro en `package.json`, `package-lock.json`, `tsconfig.json`, `README.md`, `CONTEXT.md`, la estructura base, `milestones/` o lógica protegida requiere autorización expresa. También deberán autorizarse los archivos de dependencias Python si incorporar Prefect 3 altera `pyproject.toml`, `uv.lock` o requisitos del servicio. La decisión de ubicación no autoriza cambios estructurales bajo `data/`.

## Evidencia para Project Learnings

Durante las fases autorizadas se recopilarán, sin redactar aún los documentos finales:

- baseline, alcance y decisiones de arquitectura;
- mapa de dependencias y contratos del pipeline;
- diffs por fase y justificación de reutilización;
- diagnósticos y correcciones de fallos reales;
- resultados de suites unitarias, PostgreSQL, Prefect, CLI, HTTP y E2E;
- diferencias observadas entre mocks y sistemas reales;
- decisiones rechazadas y alternativas consideradas;
- límites no resueltos, riesgos residuales y aprendizajes operativos;
- evidencia del cierre y aprobación del usuario.

Después de validar el hito, esta evidencia alimentará en orden el inventario auditado, el blueprint documental y los cinco archivos Markdown finales. Esos entregables permanecerán fuera del repositorio y de la bóveda Obsidian del usuario.

Los prompts A, B y C proporcionados por el usuario son la autoridad documental. El blueprint producido por B debe revisarse y aprobarse antes de ejecutar C. La evidencia temporal no se eliminará antes de la validación final y la autorización expresa del usuario.

## Condición de activación

Ninguna fase puede comenzar mientras este documento conserve el estado de borrador. La activación requiere aprobación explícita del usuario y ChatGPT, alcance de fase definido, baseline confirmado y lectura expresa de `.codex/trackflow-orchestration.md` y de este plan.
