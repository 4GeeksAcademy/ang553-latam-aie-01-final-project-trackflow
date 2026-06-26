# Progress: TrackFlow

## Estado actual

- El repositorio mantiene una estructura de monorepo base para los hitos del proyecto.
- Ya existe contexto funcional suficiente para TrackFlow en `CONTEXT.md` y en los archivos de `milestones/`.
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

## Pendiente

- Implementar la web publica de TrackFlow como interfaz ubicada en `uis/website`.
- Implementar una interfaz operativa o administrativa en `uis/backoffice`.
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
  - carpeta reservada para interfaces futuras, aun sin `website` ni `backoffice`
- `services/`
  - carpeta reservada para APIs o workers, sin un servicio concreto identificado para TrackFlow en esta revision

## Proximos pasos

- Consolidar el inicio del Hito 4 usando el banco de memoria ya creado.
- Crear la estructura de agentes pendiente en la raiz antes de ampliar automatizaciones.
- Crear `uis/website` para materializar la web publica definida en Hito 1.
- Crear `uis/backoffice` para futuras interfaces operativas del Hito 4.
- Reutilizar la logica de `src/` mediante imports directos en las nuevas interfaces o servicios.
- Revisar el `README.md` de cada carpeta antes de introducir cambios estructurales.

## Inconsistencias detectadas

- El `README.md` raiz describe el repo como template base sin apps ejecutables globales, pero actualmente si existe una app real en `apps/talent-pipeline-tracker/`.
- El contexto de Hito 3 indica que el backend ya esta listo, pero en esta revision no se identifica un servicio concreto en `services/` asociado a esa aplicacion.
- El contexto general mezcla el escenario oficial Estados Unidos-Espana con una propuesta personal de adaptacion hacia Mexico en `company-choice.md`.
- En el contexto organizacional aparecen referencias distintas para liderazgo ejecutivo:
  - Thomas Harry como CEO en parte del contexto
  - Daniel Espinoza como CEO en otra parte del mismo material
