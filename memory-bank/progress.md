# Progress: TrackFlow

## Estado actual

- AUTH-01/AUTH-02 completados: registro, login, perfil y rutas protegidas.
- AUTH-03 en progreso en `feature/password-reset`.

## Completado en AUTH-03

- Backend de recuperación (`POST /auth/forgot-password`, `POST /auth/reset-password`) implementado y validado.
- `POST /auth/change-password` backend implementado y validado.
- Frontend `/account/change-password` implementado, lint y build OK.

## Pendiente

- `/forgot-password` frontend.
- `/reset-password` frontend.
- Enlace "Forgot Password" en `/login`.
- QA E2E con email real.

## Siguiente paso

- Implementar frontend de recuperación de contraseña.

## Bloqueadores

- Ninguno actualmente.