# TrackFlow Context First Rule

## Nombre de la regla

- TrackFlow Context First Rule

## Alcance de aplicacion

- Siempre activa para todo el monorepo.

## Proposito

- Obligar al agente a trabajar con el contexto real de TrackFlow antes de proponer o ejecutar cambios.
- Mantener consistencia entre el contexto del negocio, el banco de memoria y la estructura tecnica del repositorio.

## Regla principal

- Antes de proponer o ejecutar cambios, el agente debe leer:
  - `CONTEXT.md`
  - `AGENTS.md`
  - `memory-bank/projectbrief.md`
  - `memory-bank/techContext.md`
  - `memory-bank/progress.md`
  - `README.md` de la carpeta afectada, si existe

## Restricciones

- No inventar otra empresa distinta a TrackFlow.
- No duplicar logica de negocio existente.
- No modificar archivos protegidos sin confirmacion.
- No crear APIs fuera de `services/`.
- No confundir `.agents/` con `agents/` o `skills/`.
- No asumir que una carpeta existe si no esta en el repo.

## Cuando detenerse y preguntar

- Si falta contexto.
- Si hay conflicto entre archivos.
- Si el cambio requiere modificar:
  - `package.json`
  - `tsconfig.json`
  - `README.md` raiz
  - `CONTEXT.md`
  - cualquier archivo dentro de `milestones/`
- Si la tarea pide mover o duplicar logica existente.

## Criterio operativo

- El contexto oficial del repo tiene prioridad sobre supuestos o adaptaciones no implementadas.
- Si el trabajo afecta una carpeta sin `README.md`, el agente debe apoyarse en:
  - la estructura existente
  - `AGENTS.md`
  - `memory-bank/`
  - los contextos de hitos aplicables
