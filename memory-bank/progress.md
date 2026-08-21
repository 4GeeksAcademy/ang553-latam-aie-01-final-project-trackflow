# Progress: TrackFlow

## Estado actual

- AUTH-01/AUTH-02 completados: registro, login, perfil y rutas protegidas.
- AUTH-03 en progreso en `feature/password-reset`.
- Backend de recuperación de contraseña completado localmente y validado.

## Completado en AUTH-03

- JWT específico de password reset con expiración corta.
- Persistencia TinyDB para single-use.
- Validación e invalidación de reset tokens.
- Configuración de Resend y helper de email.
- `POST /auth/forgot-password` implementado y validado.
- `POST /auth/reset-password` implementado y validado.

## Pendiente

- `POST /auth/change-password`
- Frontend de recuperación/cambio.
- QA E2E con email real.

## Siguiente paso

- Implementar `POST /auth/change-password`.

## Bloqueadores

- Ninguno actualmente.