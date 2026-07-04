# Tech Context: TrackFlow

## Stack tecnologico confirmado

- TypeScript esta presente en la raiz del monorepo y en las aplicaciones principales.
- En la raiz, `package.json` define `typescript` y scripts `typecheck` y `build` con `tsc`.
- El `tsconfig.json` raiz usa:
  - `target: ES2020`
  - `module: NodeNext`
  - `moduleResolution: NodeNext`
  - `strict: true`
  - `noEmit: true`
- `src/package.json` define `type: module` para permitir que las aplicaciones Next.js consuman la logica compartida de `src/` como ESM.
- Next.js, React y TypeScript estan presentes en:
  - `apps/talent-pipeline-tracker/`
  - `uis/website/`
  - `uis/backoffice/`
- `uis/website/` y `uis/backoffice/` usan:
  - Next.js 16
  - React 19
  - TypeScript
  - App Router
  - ESLint
  - Tailwind CSS v4
  - `@tailwindcss/postcss`
- Cada aplicacion mantiene su propio:
  - `package.json`
  - `package-lock.json`
  - `tsconfig.json`
  - configuracion de Next.js
  - configuracion de ESLint
  - configuracion de PostCSS
  - `.gitignore`

## Estructura tecnica actual del monorepo

- `uis/backoffice/`
  - aplicacion independiente Next.js + TypeScript para la interfaz interna.
  - ruta implementada actualmente:
    - `/` (dashboard operativo)
  - layout y estilos propios, diferenciados de la web publica.
  - componentes principales:
    - `components/layout/`
    - `components/dashboard/`
  - capa de integracion en `lib/operationalSnapshot.ts`.
  - integra tipos y logica de negocio mediante imports directos desde `src/`.
  - no duplica funciones del Hito 2.
  - muestra resultados calculados directamente en la interfaz.
  - build y lint validados.
- `src/`
  - contiene codigo TypeScript incluido por el `tsconfig.json` raiz.
  - estructura actual observada:
    - `src/types/`
    - `src/utils/`
- `apps/talent-pipeline-tracker/`
  - aplicacion existente basada en Next.js.
  - usa estructura tipo App Router con carpeta `app/`.
  - incluye `components/`, `lib/`, `services/`, `types/` y `public/`.
- `uis/website/`
  - aplicacion Next.js + TypeScript creada para la web publica.
  - rutas implementadas actualmente:
    - `/` (landing migrada desde `index.html`)
    - `/application` (formulario de Hito 1 migrado y validado)
  - estado actual:
    - build y lint validados
    - cambios integrados en `main` y reflejados en Git
  - organizacion por componentes:
    - `components/layout/`
    - `components/sections/`
    - `components/forms/`
- `uis/backoffice/`
  - aplicacion Next.js + TypeScript creada para interfaz interna.
  - ruta implementada actualmente:
    - `/` (dashboard operativo inicial)
  - layout interno diferenciado de la web publica.
  - integra logica de `src/` mediante imports directos (sin duplicacion).
  - build y lint validados.

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
- `uis/website/` sigue la misma convencion tecnica de app Next.js aislada (scripts, tsconfig estricto, eslint y tailwind v4).
- La landing publica se migro a componentes React reutilizables manteniendo contenido y orden del Hito 1.
- El formulario en `/application` quedo implementado con flujo UX completo sin backend:
  - validaciones de campos simples y grupos
  - warning dinamico para bajo volumen
  - contador dinamico de comentarios
  - foco al primer campo invalido
  - submit local con mensaje de exito
  - reset integral de estado y UI
- Se mantuvieron ids/names y mensajes del formulario original para conservar paridad funcional con Hito 1.
- El backoffice consume resultados reales del Hito 2 en UI (inventario, stock bajo, validaciones, carrier recomendado y categorias) importando desde `src/utils/*` y `src/types/models.ts`.
- `uis/backoffice/lib/operationalSnapshot.ts` funciona como capa de integracion entre la logica compartida de `src/` y los componentes visuales del dashboard.
- `src/package.json` define el alcance ESM de `src/` para resolver la compatibilidad de imports con las aplicaciones Next.js sin modificar ni duplicar la logica de negocio.
## Restricciones tecnicas para futuras implementaciones

- No duplicar la logica del Hito 2.
  - Debe importarse desde su ubicacion original en `src/`.
- Antes de modificar una carpeta, leer primero su `README.md` si existe.
- `uis/` contiene las interfaces independientes del proyecto:
  - `website` para la experiencia publica.
  - `backoffice` para la operacion interna.
  - futuras interfaces deben seguir esta misma separacion por responsabilidad.
  - No mostrar en la interfaz nombres internos de funciones, hitos o detalles de implementacion que pertenezcan al codigo.
- Los resultados de la logica compartida deben transformarse para presentarse con lenguaje de negocio en la UI.
- No versionar carpetas generadas como `.next/` o `node_modules/`.
- Los `.gitignore` de cada aplicacion deben conservar las exclusiones de archivos generados y archivos de entorno.
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
  - estado actual:
    - landing migrada y funcional en `/`.
    - formulario migrado y validado funcionalmente en `/application`.
- Hito 2:
  - contiene utilidades TypeScript puras para inventario, envios, scoring, calculos y validaciones.
  - la logica permanece en su ubicacion original dentro de `src/`.
  - parte de esta logica ya esta integrada en `uis/backoffice` mediante imports directos.
  - no debe duplicarse en nuevas aplicaciones o servicios.
- Hito 3:
  - ya existe una implementacion en `apps/talent-pipeline-tracker/`.
  - confirma el uso de Next.js para interfaces operativas internas.

- Hito 4:
  - infraestructura AI-ready completada con `memory-bank/`, `AGENTS.md`, reglas y skills.
  - web publica implementada en `uis/website`.
  - formulario completo disponible en `/application`.
  - backoffice interno implementado en `uis/backoffice`.
  - logica del Hito 2 integrada visualmente sin duplicacion.
  - build y lint validados en ambas aplicaciones.
## Dudas e inconsistencias tecnicas detectadas

- El `README.md` raiz dice que el template no incluye apps ejecutables ni configuracion global de workspace, pero en el repo actual si existe una app funcional en `apps/talent-pipeline-tracker/`.
- El contexto de Hito 3 menciona que el backend ya esta listo, pero en la estructura actual no se identifica claramente un servicio concreto en `services/` asociado a esa app.
- `src/` contiene codigo utilitario compartido y ahora tambien `src/package.json` para compatibilidad ESM, pero no tiene `README.md`.
