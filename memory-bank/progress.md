# Progress: TrackFlow

## Estado actual

- El repositorio mantiene una estructura de monorepo base para los hitos del proyecto.
- Ya existe contexto funcional suficiente para TrackFlow en `CONTEXT.md` y en `memory-bank/`.
- El banco de memoria para Hito 4 ya fue iniciado con:
  - `memory-bank/projectbrief.md`
  - `memory-bank/techContext.md`

## Implementado

- Contexto de negocio de TrackFlow documentado en el repo:
  - empresa
  - problema operativo
  - objetivos por hitos
- Web publica inicial definida a nivel de requerimientos en Hito 1:
  - sitio corporativo
  - formulario de captacion de leads
  - SEO, accesibilidad y responsive
  - uso de Tailwind indicado en el contexto
- Logica de negocio TypeScript del Hito 2 presente en la raiz:
  - `src/types/models.ts`
  - `src/utils/collections.ts`
  - `src/utils/search.ts`
  - `src/utils/transformations.ts`
  - `src/utils/validations.ts`
- Esa logica cubre el alcance funcional esperado del Hito 2:
  - inventario
  - envios
  - transportistas
  - costos
  - scoring
  - validaciones
- Aplicacion existente en `apps/talent-pipeline-tracker/`:
  - estructura propia de app
  - implementacion basada en Next.js
  - frontend interno alineado con el Hito 3
- App web publica inicial en `uis/website/`:
  - estructura Next.js + TypeScript completada, probada e integrada en Git
  - landing de Hito 1 migrada a `/` con componentes React reutilizables
  - SEO base y JSON-LD de organizacion migrados a App Router
- Ruta `/application` en `uis/website/`:
  - formulario de Hito 1 migrado y completado en React + TypeScript
  - validaciones de campos simples y de grupos implementadas
  - UX de formulario implementada: warning de bajo volumen, contador dinamico, foco al primer error, submit con mensaje de exito y reset integral
  - build y lint validados; cambios ya consolidados en Git
  - navegacion de ida y vuelta con `/` disponible

## Pendiente

- Integrar la logica TypeScript del Hito 2 desde `src/` sin duplicarla en nuevas carpetas.
- Completar la infraestructura de agentes en la raiz del monorepo.
- Crear `AGENTS.md` en la raiz.
- Crear `.agents/rules/`.
- Crear `.agents/skills/`.
- Definir y construir servicios o APIs en `services/` cuando los siguientes hitos lo requieran.

## Partes que ya existen en el repo

- `memory-bank/`
  - `projectbrief.md`
  - `techContext.md`
- `src/`
  - tipos y utilidades TypeScript compartidas de la raiz
- `packages/shared/`
  - paquete compartido de tipos
- `apps/talent-pipeline-tracker/`
  - aplicacion existente del Hito 3
- `uis/`
  - `website` ya creado con rutas `/` y `/application`
  - `backoffice` creado y funcional con dashboard operativo en `/`
- `services/`
  - carpeta reservada para APIs o workers, sin un servicio concreto identificado para TrackFlow en esta revision

## Proximos pasos

- Paso activo: revision final del Hito 4.
- Mantener estabilidad de `uis/website` (`/` y `/application`) y `uis/backoffice`.
- Reutilizar la logica de `src/` mediante imports directos en las nuevas interfaces o servicios.

## Inconsistencias detectadas

- El `README.md` raiz describe el repo como template base sin apps ejecutables globales, pero actualmente si existe una app real en `apps/talent-pipeline-tracker/`.
- El contexto de Hito 3 indica que el backend ya esta listo, pero en esta revision no se identifica un servicio concreto en `services/` asociado a esa aplicacion.
- El contexto general mezcla el escenario oficial Estados Unidos-Espana con una propuesta personal de adaptacion hacia Mexico en `company-choice.md`.

