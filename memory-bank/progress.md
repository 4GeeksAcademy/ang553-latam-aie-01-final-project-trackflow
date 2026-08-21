# Progress: TrackFlow

## Estado actual

- AUTH-01/AUTH-02 completados: registro, login, perfil y rutas protegidas.
- AUTH-03 completado en `feature/password-reset`.

## Completado en AUTH-03

- Backend de recuperación (`POST /auth/forgot-password`, `POST /auth/reset-password`) implementado y validado.
- `POST /auth/change-password` backend implementado y validado.
- Frontend `/account/change-password` implementado, lint y build OK.
- Frontend `/forgot-password` implementado con fetch público, mensaje genérico, anti-enumeración, y bloqueo tras éxito.
- Frontend `/reset-password` implementado con token de query string, validación local new/confirm, error genérico para invalid/expired/used, redirect a `/login` en éxito.
- Enlace "Forgot your password?" agregado en `/login`.

## Pendiente

- QA E2E completo con email real (Resend).
- Validación final del checklist AUTH-03.
- Preparación de PR/delivery.

## Siguiente paso

- QA E2E real de AUTH-03.