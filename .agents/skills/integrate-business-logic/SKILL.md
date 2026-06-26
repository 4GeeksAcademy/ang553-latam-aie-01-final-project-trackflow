# Integrate Business Logic

## Nombre de la skill

- Integrate Business Logic

## Objetivo unico

- Integrar logica TypeScript existente del proyecto TrackFlow en una nueva UI o servicio sin duplicar la logica original.

## Cuando usar esta skill

- Al crear una vista en `uis/website`.
- Al crear una vista en `uis/backoffice`.
- Al conectar formularios, validaciones, calculos, scoring, inventario, envios, transportistas o costos con logica existente en `src/`.
- Al crear servicios que necesiten reutilizar logica compartida.

## Cuando no usar esta skill

- Para crear logica de negocio nueva desde cero.
- Para cambiar contratos o modelos sin autorizacion.
- Para copiar utilidades de `src/` hacia otra carpeta.
- Para modificar `src/` si la tarea solo pide integracion.

## Contexto obligatorio antes de actuar

- Leer siempre:
  - `CONTEXT.md`
  - `AGENTS.md`
  - `memory-bank/projectbrief.md`
  - `memory-bank/techContext.md`
  - `memory-bank/progress.md`
  - `.agents/rules/trackflow-context-first.md`
  - `README.md`
- Leer tambien:
  - `README.md` de la carpeta destino, si existe
  - `uis/README.md` si el cambio va en una interfaz
  - `services/README.md` si el cambio va en un servicio
  - `packages/README.md` si el cambio afecta codigo compartido
- Si `src/README.md` no existe, apoyarse en:
  - la estructura real de `src/types/` y `src/utils/`
  - `milestones/HITO2_CONTEXT-trackflow.es.md`
  - `memory-bank/techContext.md`

## Inputs documentados

- Ruta de la UI o servicio donde se integrara la logica.
- Archivo o modulo existente de `src/` que se debe reutilizar.
- Funcion, tipo o utilidad requerida.
- Datos de entrada esperados.
- Resultado esperado en la interfaz o servicio.
- Restricciones especificas del contexto de TrackFlow.

## Pasos de ejecucion

- Leer el contexto obligatorio.
- Identificar la necesidad funcional.
- Buscar logica existente en `src/types/` y `src/utils/`.
- Confirmar si la logica ya existe.
- Importar la logica desde su ubicacion original.
- Evitar copiar funciones o modelos.
- Crear unicamente el codigo de integracion necesario.
- Validar TypeScript.
- Revisar diff.
- Actualizar `memory-bank/progress.md` si el estado del proyecto cambia.

## Reglas operativas de integracion

- Priorizar reutilizacion desde `src/`.
- Si existe un paquete compartido aplicable en `packages/`, reutilizarlo en lugar de duplicar implementaciones.
- No mover logica de negocio a `uis/`, `services/` o `apps/` solo para facilitar imports.
- No crear APIs fuera de `services/`.
- Mantener separacion clara entre:
  - logica de negocio
  - codigo de integracion
  - presentacion de UI
- Si la tarea parece requerir cambiar `src/`, detenerse y confirmar si el objetivo es integracion o extension real del core.

## Output esperado

- Una integracion que reutilice logica existente.
- Imports claros desde `src/` o paquete compartido aplicable.
- UI o servicio funcionando con datos procesados.
- Sin duplicacion de logica.

## Criterios de aceptacion

- El archivo `SKILL.md` existe en `.agents/skills/integrate-business-logic/`.
- La skill tiene un objetivo unico.
- La skill documenta inputs.
- La skill tiene criterios de aceptacion verificables.
- La logica reutilizada se importa desde `src/` o desde el paquete compartido correspondiente.
- No se copian funciones existentes a `uis/`, `services/` o `apps/`.
- No se modifica `src/` salvo que la tarea lo pida explicitamente.
- TypeScript no reporta errores despues de la integracion.
- El diff no muestra duplicacion de logica.
- La integracion respeta `CONTEXT.md`, `AGENTS.md` y `memory-bank/techContext.md`.

## Checklist final

- Contexto leido.
- Ruta correcta identificada.
- Logica existente localizada.
- Imports usados.
- Sin duplicacion.
- Validaciones ejecutadas.
- Diff revisado.
- Progress actualizado si aplica.

## Alineacion con TrackFlow

- Esta skill existe para apoyar el Hito 4 sin romper la base del Hito 2.
- Su uso es especialmente relevante para:
  - web publica de TrackFlow en `uis/website`
  - interfaces operativas en `uis/backoffice`
  - servicios que necesiten reutilizar modelos, validaciones, scoring o calculos existentes
- La skill protege la arquitectura actual del monorepo:
  - interfaces en `uis/`
  - servicios en `services/`
  - compartidos en `packages/`
  - logica TypeScript raiz en `src/`
