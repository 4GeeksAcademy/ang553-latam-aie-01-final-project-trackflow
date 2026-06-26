# Tech Context: TrackFlow

## Stack tecnologico confirmado

- TypeScript esta presente en la raiz del monorepo y en `apps/talent-pipeline-tracker/`.
- En la raiz, `package.json` define `typescript` y scripts `typecheck` y `build` con `tsc`.
- El `tsconfig.json` raiz usa:
  - `target: ES2020`
  - `module: NodeNext`
  - `moduleResolution: NodeNext`
  - `strict: true`
  - `noEmit: true`
- Next.js esta presente en `apps/talent-pipeline-tracker/`.
- React y React DOM estan presentes en `apps/talent-pipeline-tracker/`.
- Tailwind esta presente en `apps/talent-pipeline-tracker/`:
  - dependencia `tailwindcss`
  - plugin `@tailwindcss/postcss`
  - instruccion explicita de uso en Hito 1

## Estructura tecnica actual del monorepo

- `uis/`
  - carpeta destinada a interfaces de usuario.
  - su README indica que aqui deben vivir apps como `website` o `backoffice`.
- `services/`
  - carpeta destinada a APIs y background workers.
- `packages/`
  - carpeta destinada a paquetes compartidos versionables.
  - existe `packages/shared/package.json` con el paquete `@repo/shared-types`.
- `src/`
  - contiene codigo TypeScript incluido por el `tsconfig.json` raiz.
  - estructura actual observada:
    - `src/types/`
    - `src/utils/`
- `apps/talent-pipeline-tracker/`
  - aplicacion existente basada en Next.js.
  - usa estructura tipo App Router con carpeta `app/`.
  - incluye `components/`, `lib/`, `services/`, `types/` y `public/`.

## Decisiones de arquitectura ya tomadas

- El repo sigue una organizacion de monorepo por responsabilidades:
  - interfaces en `uis/`
  - servicios en `services/`
  - paquetes reutilizables en `packages/`
  - utilidades y tipos TypeScript en `src/`
- La logica del Hito 2 vive en su ubicacion original dentro de `src/`.
- El `tsconfig.json` raiz incluye solo `src/**/*.ts`, lo que refuerza que la logica TypeScript compartida de la raiz se concentra en `src/`.
- `apps/talent-pipeline-tracker/` ya adopta una arquitectura separada por aplicacion, con su propio `package.json`, `tsconfig.json` y dependencias.
- El Hito 3 ya parte de un frontend montado sobre Next.js para una herramienta operativa interna.

## Restricciones tecnicas para futuras implementaciones

- No duplicar la logica del Hito 2.
  - Debe importarse desde su ubicacion original en `src/`.
- Antes de modificar una carpeta, leer primero su `README.md` si existe.
- `uis/` debe usarse para interfaces futuras como `website` y `backoffice`.
- `services/` debe usarse para APIs o servicios backend.
- `packages/` debe usarse para codigo compartido reutilizable entre apps y servicios.
- `src/` debe considerarse la ubicacion actual de utilidades y tipos TypeScript de la raiz.
- En la raiz no hay un workspace runner configurado segun `README.md`.
- No asumir tecnologias de backend adicionales en la raiz mientras no aparezcan explicitamente en el repo.
- En `src/` no existe `README.md` actualmente, asi que cualquier cambio ahi debe apoyarse en la estructura existente y en los contextos de hitos.

## Implicaciones por hito

- Hito 1:
  - requiere sitio web responsive, accesible y con SEO.
  - pide usar Tailwind.
- Hito 2:
  - requiere utilidades TypeScript puras para inventario, envios, scoring, calculos y validaciones.
  - no debe resolverse duplicando logica en nuevas carpetas.
- Hito 3:
  - ya existe una implementacion en `apps/talent-pipeline-tracker/`.
  - confirma el uso de Next.js para interfaces operativas internas.

## Dudas e inconsistencias tecnicas detectadas

- El `README.md` raiz dice que el template no incluye apps ejecutables ni configuracion global de workspace, pero en el repo actual si existe una app funcional en `apps/talent-pipeline-tracker/`.
- El contexto de Hito 3 menciona que el backend ya esta listo, pero en la estructura actual no se identifica claramente un servicio concreto en `services/` asociado a esa app.
- `src/` contiene codigo utilitario, pero no tiene `README.md`, aunque la consigna general del repo recomienda apoyarse en README por carpeta.
