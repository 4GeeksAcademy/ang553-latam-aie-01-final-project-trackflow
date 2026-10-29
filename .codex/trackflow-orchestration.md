# TrackFlow — Contrato operativo de orquestación

> Convención del proyecto. Codex no debe asumir que este documento se carga automáticamente: toda tarea futura que pretenda aplicarlo debe ordenar expresamente su lectura.

## Propósito y alcance

Este contrato regula el trabajo delegado de desarrollo en TrackFlow. Complementa, sin sustituir, `AGENTS.md`, `.agents/rules/trackflow-context-first.md`, el contexto oficial y las instrucciones específicas de cada tarea. Ante un conflicto, prevalecen las instrucciones de mayor autoridad y el alcance aprobado por el usuario.

No concede autorización para implementar el borrador de plan ni para modificar archivos protegidos.

## Autoridad y responsabilidades

### Usuario + ChatGPT

- Definen y aprueban arquitectura, alcance, fases, dependencias y criterios de aceptación.
- Pueden aprobar el plan completo de ejecución, incluidas todas sus fases y gates, en una sola autorización explícita.
- Autorizan cambios de alcance y decisiones arquitectónicas pendientes.
- Controlan las operaciones Git reservadas y la aprobación final del hito.
- Deciden cuándo se inicia Project Learnings y cuándo puede limpiarse evidencia temporal.

### Orquestador

- Verifica baseline, instrucciones activas, alcance y archivos protegidos antes de delegar.
- Divide solo el plan aprobado en unidades de trabajo trazables.
- Coordina implementadores, revisa entregas, ejecuta o repite QA autorizado y conserva evidencia.
- Inspecciona directamente los archivos y diffs; no acepta afirmaciones del implementador como única prueba.
- Solicita correcciones ordinarias dentro del alcance y registra incidentes, decisiones y limitaciones.
- Presenta al usuario el estado real distinguiendo entre implementado, probado, bloqueado y pendiente.

### Implementador

- Escribe, prueba y corrige únicamente dentro del alcance delegado.
- Respeta el baseline, los archivos permitidos y protegidos, y reutiliza la lógica existente.
- Informa cambios, comandos, resultados, fallos, skips, incertidumbres y riesgos sin ocultarlos.
- No amplía arquitectura, contratos ni alcance y devuelve el control al orquestador cuando necesita autoridad adicional.

## Fases y avance

- El número de fases es variable. Lo determina exclusivamente el plan aprobado según dependencias, riesgos y criterios de aceptación.
- Cada fase debe declarar entradas, entregables, archivos permitidos, validaciones y condición de salida.
- Una vez aprobado el plan completo, el orquestador puede avanzar automáticamente entre todas las fases autorizadas cuando sus gates pasan y no existe una condición de detención.
- No se solicitará autorización humana rutinaria entre fases ya autorizadas. Solo se detendrá el avance ante un bloqueo material, una desviación de alcance o una decisión expresamente reservada.
- El orquestador no puede inventar fases, ampliar el alcance, adelantar trabajo no autorizado ni convertir un borrador en autorización.
- Una fase incompleta permanece `bloqueada` o `pendiente`; no se presenta como terminada por conveniencia del calendario.

## Ubicación obligatoria del pipeline de TrackFlow

- El único paquete Python activo del pipeline de este hito es `pipelines/`, en la raíz del repositorio.
- Los módulos existentes de extracción, transformación, publicación, control de corridas y coordinación permanecen en `pipelines/`.
- El futuro flow de Prefect 3 y su entrada CLI se implementarán en `pipelines/pipeline.py`.
- El comando preferido de ejecución será `python -m pipelines.pipeline` y deberá verificarse durante la fase CLI.
- El orquestador debe transmitir explícitamente esta regla a todo subagente implementador antes de delegar trabajo del pipeline.
- No se creará ni mantendrá una segunda implementación bajo `data/pipelines/`.
- No se crearán wrappers, symlinks, copias ni adaptadores de compatibilidad bajo `data/pipelines/`.
- No se moverán nuevamente la extracción, transformación, publicación ni coordinación existentes.
- No se modificará ningún archivo dentro de `data/` durante el desarrollo de este hito. Sus documentos pueden leerse como fuentes históricas o de diseño.
- Los endpoints de reporting se implementarán bajo `services/reporting/` y reutilizarán el paquete raíz sin duplicar lógica empresarial en routers.
- Cualquier cambio posterior de ubicación requiere aprobación explícita de Usuario + ChatGPT.

Codex no carga automáticamente este contrato. Toda sesión futura y toda delegación relacionada con el hito deberán ordenar expresamente su lectura antes de proponer o ejecutar cambios.

## Corrección autónoma

Dentro de una fase autorizada, el implementador y el orquestador pueden:

1. investigar un fallo reproducible;
2. corregir su causa dentro de los archivos y contratos permitidos;
3. repetir la validación afectada;
4. ejecutar la regresión proporcional al riesgo; y
5. documentar diagnóstico, corrección y resultado.

No está permitido ocultar fallos, borrar o debilitar pruebas, cambiar contratos empresariales, relajar criterios o introducir mocks engañosos para obtener un resultado aparente. Un fallo que exige autoridad adicional activa la detención obligatoria.

## Detención obligatoria

El agente debe detener el avance y pedir decisión ante:

- un cambio arquitectónico no autorizado;
- la necesidad de modificar archivos protegidos sin permiso explícito;
- un cambio no autorizado de contratos empresariales o semántica de métricas;
- riesgo de pérdida, corrupción o modificación irreversible de datos;
- acceso requerido a credenciales, secretos o bases no autorizadas;
- un fallo crítico irresoluble dentro del alcance;
- imposibilidad de demostrar un criterio de aceptación obligatorio;
- contradicción material entre instrucciones activas que no pueda resolverse por jerarquía;
- baseline Git distinto del aprobado o cambios ajenos que interfieran con el trabajo.

La detención debe identificar el bloqueo, la evidencia disponible, las alternativas reales y la autorización necesaria.

## Control de Git

El usuario conserva el control de:

- creación y cambio de ramas;
- staging;
- commits;
- push;
- merge.

Los agentes no ejecutarán esas operaciones salvo autorización explícita posterior y específica. Nunca se implementará directamente en `main`. La inspección de estado, historial y diff es admisible cuando la tarea la autoriza.

## QA y estándares de evidencia

- El orquestador revisa directamente cada archivo cambiado y el diff completo.
- Cada fase tiene QA automatizado obligatorio y proporcional a sus entregables y riesgos.
- La validación manual integral de UX, CLI, API y comportamiento se concentra en la fase final.
- Las verificaciones manuales intermedias solo son gates obligatorios cuando resultan necesarias para impedir un riesgo real o resolver una dependencia material.
- Las pruebas con mocks verifican contratos simulados, no integración real.
- `SKIP` no equivale a `PASS`; `XFAIL`, bloqueos de entorno y pruebas no ejecutadas se reportan por separado.
- Una prueba que termina deliberadamente en fallo para impedir certificación no demuestra integración.
- Las validaciones deben asociarse a un comando, alcance, resultado y evidencia reproducible.
- El QA automatizado y el manual se separan; una inspección manual no sustituye una prueba automatizable.
- No se declara PostgreSQL, Prefect, HTTP o concurrencia real como probado mediante fakes o inspección estática.
- Los estados mínimos son: `implementado`, `probado`, `bloqueado` y `pendiente`.

## Expediente temporal de evidencias

Durante la implementación se mantendrá evidencia incremental y compacta de:

- baseline y alcance autorizado;
- cambios y diffs verificables;
- incidentes, síntomas y diagnósticos;
- decisiones técnicas y alternativas reales;
- pruebas ejecutadas, resultados, skips y bloqueos;
- correcciones y regresiones repetidas;
- limitaciones y riesgos residuales;
- resultado y control de salida de cada fase.

Cada registro debe referenciar archivos, cambios o pruebas verificables. No se copiarán secretos, tokens, cadenas de conexión ni credenciales.

El expediente temporal residirá fuera del repositorio en una ubicación que el usuario autorice explícitamente. Se propone `C:\angel\trackflow-review-temp` como destino pendiente de esa autorización; este contrato no crea ni presupone la carpeta. Hasta que exista autorización, la evidencia queda limitada a los resultados visibles de la sesión.

El expediente debe incluir evidencia incremental por fase, diffs verificables —incluidos archivos nuevos sin seguimiento—, pruebas y skips, incidentes y diagnósticos, decisiones técnicas, limitaciones y resultado final. Sus reportes deben poder exportarse para revisión de ChatGPT sin incluir secretos.

## Project Learnings

Durante el desarrollo se registra evidencia; no se redactan anticipadamente las notas definitivas.

Tras validar el hito se ejecutarán, en este orden y respetando los prompts proporcionados por el usuario:

1. inventario auditado de experiencia;
2. blueprint documental;
3. redacción de cinco archivos Markdown finales.

Los tres prompts proporcionados por el usuario son la autoridad documental. El blueprint del paso 2 debe ser revisado y aprobado antes de ejecutar el paso 3.

Los cinco documentos se entregarán al usuario fuera del repositorio y fuera de su bóveda Obsidian. No se escribirán automáticamente en ella.

La evidencia temporal solo podrá eliminarse después de confirmar entrega, revisión y aprobación del material necesario para Project Learnings, y únicamente con autorización del usuario. Esa limpieza nunca incluirá código, tests, historial Git ni documentación oficial.

## Eficiencia operativa

- Evitar lecturas redundantes, ciclos de QA duplicados sin justificación y reportes más extensos de lo necesario para demostrar el resultado.
- Reutilizar evidencia vigente cuando el baseline y el alcance no hayan cambiado.
- La eficiencia nunca autoriza relajar criterios de aceptación, omitir gates obligatorios ni presentar evidencia insuficiente como éxito.

## Cierre de una fase

Una fase puede cerrarse cuando:

- todos los entregables autorizados existen y fueron inspeccionados;
- los criterios obligatorios tienen evidencia suficiente;
- las validaciones aplicables pasan o sus bloqueos fueron aceptados explícitamente;
- no hay cambios fuera de alcance;
- el estado Git y los riesgos residuales se reportan; y
- el usuario conserva la decisión final sobre Git y cierre del hito.
