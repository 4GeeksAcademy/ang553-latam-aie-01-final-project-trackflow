# AGENTS.md

## Objetivo

- Este archivo define las reglas operativas para cualquier agente que trabaje en este monorepo.
- Todo trabajo debe mantenerse alineado con la empresa TrackFlow, el contexto del repo y el estado actual documentado en `memory-bank/`.

## Lectura obligatoria al inicio de cada sesion

- Leer siempre estos archivos antes de proponer o ejecutar cambios:
  - `CONTEXT.md`
  - `memory-bank/projectbrief.md`
  - `memory-bank/techContext.md`
  - `memory-bank/progress.md`
  - `README.md`
- Leer ademas el `README.md` de la carpeta que se vaya a modificar, si existe.
- Si el trabajo afecta varias carpetas, revisar el `README.md` de cada una de ellas antes de cambiar archivos.

## Reglas generales

- No inventar otra empresa distinta a TrackFlow.
- No usar un contexto de negocio ajeno al repositorio.
- No duplicar logica de negocio ya existente.
- La logica TypeScript existente en `src/` debe reutilizarse mediante imports cuando aplique.
- No crear APIs fuera de `services/`.
- No confundir:
  - `agents/` y `skills/` existentes en el repo
  - con `.agents/`, que es configuracion operativa separada

## Flujo obligatorio antes de cada commit

- Paso 1. Leer el contexto obligatorio:
  - `CONTEXT.md`
  - archivos de `memory-bank/`
  - `README.md` raiz
  - `README.md` de la carpeta afectada, si existe
- Paso 2. Revisar el alcance de cambios:
  - confirmar que el cambio pertenece a TrackFlow
  - confirmar que la ubicacion elegida respeta la arquitectura del monorepo
  - detectar si ya existe logica reutilizable en `src/`, `packages/` o en la app correspondiente
- Paso 3. Hacer cambios minimos y justificados:
  - evitar cambios estructurales innecesarios
  - evitar duplicacion de logica
  - mantener coherencia con hitos y memory-bank
- Paso 4. Ejecutar validaciones disponibles:
  - typecheck
  - build
  - lint
  - o las validaciones especificas de la carpeta modificada, si existen
- Paso 5. Revisar el diff:
  - confirmar que solo cambian archivos esperados
  - confirmar que no se modificaron archivos sensibles por accidente
- Paso 6. Actualizar `memory-bank/progress.md` si cambia el estado del proyecto:
  - implementado
  - pendiente
  - proximos pasos
- Paso 7. Preparar commit solo si todo esta correcto:
  - contexto revisado
  - alcance validado
  - cambios minimos
  - validaciones ejecutadas
  - diff revisado

## Archivos y areas que no deben modificarse sin confirmacion explicita

- `CONTEXT.md`
- `README.md`
- `package.json`
- `package-lock.json`
- `tsconfig.json`
- la estructura base del monorepo
- la logica de negocio existente en `src/` si el trabajo es solo de integracion
- cualquier archivo dentro de `milestones/`
- `apps/talent-pipeline-tracker/AGENTS.md`, salvo que se pida explicitamente

## Reglas por estructura del monorepo

- `uis/`
  - usar para interfaces de usuario
  - futuras implementaciones como `website` o `backoffice` deben vivir aqui
- `services/`
  - usar para APIs y servicios backend
- `packages/`
  - usar para paquetes compartidos y codigo reutilizable versionable
- `src/`
  - tratar como ubicacion actual de tipos y utilidades TypeScript compartidas de la raiz
- `memory-bank/`
  - usar para mantener continuidad de contexto entre sesiones

## Criterios de seguridad de cambios

- Si un cambio altera arquitectura, dependencias raiz o estructura base, pedir confirmacion explicita antes de modificar.
- Si una tarea parece requerir mover o reescribir logica existente de `src/`, confirmar primero el objetivo.
- Si existe ambiguedad entre el contexto oficial del repo y una adaptacion propuesta en `company-choice.md`, priorizar el contexto oficial del repo salvo instruccion contraria.

## Cierre esperado de cada sesion

- Dejar el estado del trabajo claro.
- No marcar como implementado algo que solo este definido en contexto.
- Si cambia el avance real del proyecto, reflejarlo en `memory-bank/progress.md`.
