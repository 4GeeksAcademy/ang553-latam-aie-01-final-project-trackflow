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
  - rutas implementadas: `/` (dashboard), `/login`, `/register`, `/account/profile`, `/incidents`, `/suppliers`.
  - layout y estilos propios, diferenciados de la web publica.
  - capa de integracion en `lib/operationalSnapshot.ts`.
  - integra tipos y logica de negocio mediante imports directos desde `src/`.
  - no duplica funciones del Hito 2.
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
  - aplicacion Next.js + TypeScript para la web publica.
  - rutas: `/` (landing), `/application` (formulario Hito 1 migrado).
  - `components/layout/`, `components/sections/`, `components/forms/`.

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
- El formulario en `/application` se implemento con flujo UX completo sin backend: validaciones, warning dinamico de bajo volumen, contador de comentarios, foco al primer campo invalido, submit local con exito y reset de estado/UI.
- Se mantuvieron ids/names y mensajes del formulario original para conservar paridad funcional con Hito 1.
- El backoffice consume resultados reales del Hito 2 en UI (inventario, stock bajo, validaciones, carrier recomendado y categorias) importando desde `src/utils/*` y `src/types/models.ts`.
- `uis/backoffice/lib/operationalSnapshot.ts` funciona como capa de integracion entre la logica compartida de `src/` y los componentes visuales del dashboard.
- `src/package.json` define el alcance ESM de `src/` para resolver la compatibilidad de imports con las aplicaciones Next.js sin modificar ni duplicar la logica de negocio.

## Backend: servicios API (FastAPI)

### Stack backend
- **FastAPI** como framework web, servido con **Uvicorn**.
- **TinyDB** como base de datos embebida (documentos JSON).
- **passlib (bcrypt)** para hashing de contraseñas.
- **python-jose** para generacion y validacion de JWTs.

### Persistencia de autenticacion
- Usuarios y perfiles almacenados en `services/api/data/auth.json`.
- Tablas: `users` (con `hashed_password`), `profiles`.
- Contraseñas nunca almacenadas en claro — solo hash bcrypt.

### Autenticacion / autorizacion
- `POST /auth/login` recibe credenciales via OAuth2 form y devuelve un JWT access token.
- `GET /auth/me` devuelve el usuario autenticado (requiere Bearer token).
- `OAuth2PasswordBearer(tokenUrl="/auth/login")` como esquema de seguridad.
- `get_current_user` como dependencia FastAPI que valida el JWT y retorna `UserInDB`.
- Roles disponibles: `admin`, `manager`, `user`.
- Registro publico via `POST /users` crea usuarios con `role=user`.
- CRUD de usuarios (`/users`) protegido por rol: admin puede gestionar cualquier usuario; cada usuario puede editar sus propios datos (excepto `role`/`is_active`).
- CRUD de perfiles (`/profiles/me`) para datos adicionales del usuario autenticado.

### Organizacion del codigo
- `auth_settings.py` — configuracion JWT desde variables de entorno.
- `auth_database.py` — conexion TinyDB y tablas `users`, `profiles`.
- `auth_models.py` — modelos Pydantic v2 (UserInDB, ProfileInDB, request/response models, Role enum).
- `auth_security.py` — `hash_password()`, `verify_password()`, `create_access_token()`, `get_current_user()`.
- `auth_services.py` — CRUD de usuarios y perfiles sobre TinyDB.
- Routers en `routes/auth.py`, `routes/users.py`, `routes/profiles.py`.
- Todos integrados via `main.py` con CORS para desarrollo local y Codespaces.

## Autenticacion frontend (uis/backoffice)

- Token JWT almacenado en `localStorage` bajo la clave `trackflow_access_token` (`lib/auth.ts`).
- `AuthContext` / `AuthProvider` (`lib/AuthContext.tsx`) provee estado global: `user`, `isLoading`, `isAuthenticated`, `setSession()`, `refreshUser()`, `logout()`.
- `authFetch` (`lib/authFetch.ts`) es un wrapper de `fetch` que inyecta el Bearer token automaticamente y redirige a `/login` al recibir un 401.
- `AuthGuard` (`components/layout/AuthGuard.tsx`) es un componente cliente que bloquea el renderizado hasta verificar la sesion y redirige a `/login` si no hay autenticacion.
- `authApi.ts` es la capa HTTP: `getCurrentUser()`, `login()`, `register()`, `getMyProfile()`, `updateMyProfile()`.
- Paginas de autenticacion: `/login`, `/register`, `/account/profile`.
- Tipos compartidos en `types/auth.ts` (AuthUser, LoginCredentials, RegisterPayload, UserProfile, etc.).
- `Providers.tsx` monta `AuthProvider` en el root layout.
- En 401, `authFetch` remueve el token y redirige a `/login`.

### Recuperación de contraseña (password reset)
- `POST /auth/forgot-password` recibe email, responde genéricamente (anti-enumeración).
- `POST /auth/reset-password` recibe token + nueva contraseña, valida y actualiza.
- Password reset usa JWT específico con claim `type: "password_reset"` para diferenciarlo de access tokens.
- Expiración corta configurable (`RESET_TOKEN_EXPIRE_MINUTES`, 30 min por defecto).
- Single-use mediante estado server-side en TinyDB: se persiste hash SHA-256 del `jti`, no el JWT completo.
- Token se invalida **antes** de cambiar la contraseña — si falla el update, el token ya queda inservible.
- Resend es el servicio de email; `RESEND_API_KEY` y `FRONTEND_URL` vienen de entorno.
- `send_password_reset_email()` construye enlace: `{FRONTEND_URL}/reset-password?token={token}`.
- `reset-password` rechaza tokens inválidos, expirados o ya usados → HTTP 400.
- Tabla `password_reset_tokens` agregada a `auth.json` en `auth_database.py`.
- Dependencia `resend>=0.8.0,<1.0.0` agregada en `pyproject.toml` y `requirements.txt`.

### Token purpose en autenticacion de sesion
- Los nuevos access tokens JWT deben incluir `type: "access"`.
- Los tokens de recovery/reset deben incluir `type: "password_reset"`.
- `get_current_user` valida el proposito del token antes de aceptar sesion.
- Por compatibilidad temporal, access tokens legacy sin claim `type` se aceptan como sesion valida.
- Tokens con otros `type` (por ejemplo `password_reset`) se rechazan para autenticacion de sesion.
- Motivo: evitar reutilizacion cruzada de tokens (por ejemplo, usar tokens de recuperacion de password como credenciales de acceso).

## Testing automatizado (decision tecnica reutilizable)

### Backend testing
- El backend usa `pytest` con `pytest-cov` como estrategia base de pruebas y cobertura.
- Las pruebas de backend viven en `tests/` a nivel raiz del repo.
- Los tests deben ejecutarse con aislamiento de TinyDB para evitar contaminacion de estado entre casos.

### TypeScript testing
- Las utilidades TypeScript de raiz se validan con `Jest` + `ts-jest`.
- La configuracion de Jest se centraliza en la raiz del repo.
- Los tests TypeScript viven en `tests/` a nivel raiz del repo.
- El entorno de ejecucion para estas pruebas es `node`.

## Change-password flow (validado)

- `POST /auth/change-password` requiere autenticación vía `get_current_user`.
- Recibe `current_password` y `new_password` (min 8 chars).
- Verifica current password contra hash bcrypt existente; 400 si incorrecta.
- Hashea nueva password y actualiza solo al usuario autenticado por `current_user.id`.
- Frontend `/account/change-password` protegido por `AuthGuard`.
- `authFetch` inyecta Bearer token automáticamente.
- Validación local de confirmación (new === confirm) antes de llamar API.
- Feedback de éxito/error con limpieza de campos sensibles.

## Frontend password recovery (validado)

- `/forgot-password` usa fetch público a `/auth/forgot-password`.
- Respuesta de éxito anti-enumeración genérica (mismo mensaje exista o no el email).
- Formulario queda bloqueado (disabled + isDone) tras éxito.
- Doble submit prevenido con guard `if (submitting) return`.
- `/login` enlaza a `/forgot-password` con link "Forgot your password?".
- `/reset-password` toma token del query string via `useSearchParams()`.
- Si falta token: no llama API, muestra error + link a forgot-password.
- Usa fetch público a `/auth/reset-password` (no authFetch).
- Validación local new/confirm antes de llamar API.
- Invalid/expired/used se trata como un único error genérico "Invalid or expired password reset link" + link a forgot-password.
- Éxito redirige a `/login` (no login automático).
- Token/password no se persisten en localStorage.
- Componente `ResetPasswordForm` envuelto en `<Suspense>` (requisito Next.js para `useSearchParams()`).

## Restricciones tecnicas para futuras implementaciones

### Error handling conventions

1. **Frontend API boundaries**
   - Normalizar network errors en la capa API.
   - No mostrar `statusText`, raw backend detail ni errores de parsing al usuario.
   - `ApiError` puede conservar `statusCode` internamente mientras el mensaje visible permanece controlado.

2. **Authentication hydration**
   - 401 representa sesión inválida y puede limpiar token/redirigir.
   - Network/5xx no deben tratarse automáticamente como sesión inválida.
   - Un fallo transitorio debe terminar loading y ofrecer recovery explícito, actualmente Retry.

3. **Backend exception scope**
   - Preferir excepciones específicas (`JWTError`).
   - Si una librería como TinyDB no ofrece una excepción común útil, `except Exception` solo alrededor de la llamada concreta de persistencia.
   - No envolver endpoints completos o funciones principales innecesariamente.

4. **Rollback**
   - Un fallo secundario de cleanup/rollback no debe sustituir la excepción principal.
   - Registrar el fallo secundario server-side.

5. **Logs**
   - No registrar PII innecesaria como email completo.
   - Detalle técnico puede permanecer server-side cuando sea útil, nunca filtrarse al cliente.

6. **Safe file export**
   - Para outputs que no deben quedar parciales: temporary file in destination directory → write/flush/fsync/close → `os.replace()` → best-effort cleanup.

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
- `services/api/` es el backend FastAPI del proyecto (auth, incident analysis).
- `packages/` debe usarse para codigo compartido reutilizable entre apps y servicios.
- `src/` debe considerarse la ubicacion actual de utilidades y tipos TypeScript de la raiz.
- En la raiz no hay un workspace runner configurado segun `README.md`.
- No asumir tecnologias de backend adicionales en la raiz mientras no aparezcan explicitamente en el repo.
- En `src/` no existe `README.md` actualmente, asi que cualquier cambio ahi debe apoyarse en la estructura existente y en los contextos de hitos.

## Implicaciones por hito

- **Hito 1**: Sitio web responsive con Tailwind, landing en `/` y formulario funcional en `/application` (migrados y validados).
- **Hito 2**: Utilidades TypeScript puras en `src/` (inventario, envios, scoring, calculos, validaciones). No duplicar.
- **Hito 3**: `apps/talent-pipeline-tracker/` confirma Next.js para interfaces operativas. Backend FastAPI con auth (AUTH-01/AUTH-02) implementado en `services/api/`.
- **Hito 4**: Infraestructura AI-ready (`memory-bank/`, `AGENTS.md`, skills). Web publica y backoffice en `uis/`. Logica Hito 2 integrada visualmente sin duplicacion. Auth frontend/backend implementados.
