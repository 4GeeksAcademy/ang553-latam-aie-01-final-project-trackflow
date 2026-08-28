# TESTING

## Purpose

This document defines the testing strategy for TrackFlow with immediate focus on AUTH-088. It describes how tests will be executed and which authentication behaviors must be covered before implementation.

## Testing principles

- Tests focus on business logic, not on FastAPI, Pydantic, or HTTP serialization internals.
- Tests must be independent and deterministic.
- TinyDB must be isolated in tests.
- Tests must not use real production-like data.
- External services (for example, Resend) must be mocked.
- Time-sensitive behavior (token expiration, issued-at windows) must be controlled explicitly.

## AUTH-088 scope

Mandatory endpoint scope for this ticket:

- POST /users
- POST /auth/login
- GET /auth/me
- POST /auth/forgot-password
- POST /auth/reset-password
- POST /auth/change-password

POST /users is included as functional authentication scope because it is the public signup flow that creates user identity.

Out of mandatory scope for AUTH-088: users CRUD/profile endpoint testing beyond what is required by the six flows above.

## Planned test matrix

| ID | Endpoint | Type | Planned case |
| --- | --- | --- | --- |
| AUTH-USERS-HP-01 | POST /users | Happy path | Valid signup creates a standard user. |
| AUTH-USERS-EDGE-01 | POST /users | Edge case | Valid signup with optional/partial profile data. |
| AUTH-USERS-FAIL-01 | POST /users | Failure mode | Duplicate email is rejected. |
| AUTH-USERS-FAIL-02 | POST /users | High value | If profile creation fails after user creation, rollback is executed. |
| AUTH-LOGIN-HP-01 | POST /auth/login | Happy path | Correct active-user credentials produce an access token. |
| AUTH-LOGIN-EDGE-01 | POST /auth/login | Edge case | Email with case/space differences is resolved using existing normalization. |
| AUTH-LOGIN-FAIL-01 | POST /auth/login | Failure mode | Incorrect password is rejected. |
| AUTH-LOGIN-FAIL-02 | POST /auth/login | High value | Non-existent user is rejected with generic behavior. |
| AUTH-LOGIN-FAIL-03 | POST /auth/login | High value | Inactive user is rejected. |
| AUTH-ME-HP-01 | GET /auth/me | Happy path | Valid access token returns matching active user. |
| AUTH-ME-EDGE-01 | GET /auth/me | Edge case | Valid token whose user no longer exists is rejected. |
| AUTH-ME-FAIL-01 | GET /auth/me | Failure mode | Expired access token is rejected. |
| AUTH-ME-FAIL-02 | GET /auth/me | High value | Malformed token or invalid signature is rejected. |
| AUTH-ME-FAIL-03 | GET /auth/me | High value | User deactivated after token issuance is rejected. |
| AUTH-FORGOT-HP-01 | POST /auth/forgot-password | Happy path | Existing user triggers reset token creation and email flow processing. |
| AUTH-FORGOT-EDGE-01 | POST /auth/forgot-password | Edge case | Non-existent email returns the same generic response (anti-enumeration). |
| AUTH-FORGOT-FAIL-01 | POST /auth/forgot-password | Failure mode | If email delivery fails, issued reset token is invalidated and corresponding error is returned. |
| AUTH-FORGOT-FAIL-02 | POST /auth/forgot-password | High value | Reset token issuance failure is handled in a controlled way. |
| AUTH-RESET-HP-01 | POST /auth/reset-password | Happy path | Valid unused reset token changes password and becomes invalid. |
| AUTH-RESET-EDGE-01 | POST /auth/reset-password | Edge case | Cryptographically valid but already-used token is rejected and cannot be reused. |
| AUTH-RESET-FAIL-01 | POST /auth/reset-password | Failure mode | Expired reset token is rejected. |
| AUTH-RESET-FAIL-02 | POST /auth/reset-password | High value | Token with type different from password_reset is rejected. |
| AUTH-RESET-FAIL-03 | POST /auth/reset-password | High value | Valid token whose user no longer exists is rejected. |
| AUTH-RESET-HV-UTC-01 | POST /auth/reset-password | High value (future) | Investigate timezone-naive expires_at persistence vs UTC normalization, if backed by implementation details. |
| AUTH-CHPASS-HP-01 | POST /auth/change-password | Happy path | Correct current password allows changing to new password. |
| AUTH-CHPASS-EDGE-01 | POST /auth/change-password | Edge case | Empty current_password reaches domain logic and is rejected as incorrect current password. |
| AUTH-CHPASS-FAIL-01 | POST /auth/change-password | Failure mode | Incorrect current password is rejected. |
| AUTH-CHPASS-OPT-01 | POST /auth/change-password | Optional | Authenticated user whose record no longer exists. |

## Critical token regression

AUTH-088 is planned after a regression related to token expiration behavior. The following test objectives are essential and must be explicitly verified during implementation:

- Expired access token rejection.
- Expired password-reset token rejection.
- Password-reset token single-use enforcement and reuse rejection.

These checks are planned; they are not executed yet.

## AI-assisted test discovery

Research case (not a confirmed bug):

A password-reset token may carry a valid sub, and get_current_user may not explicitly enforce the token type claim.

Planned validation:

- Generate a valid password-reset token.
- Use it directly against get_current_user.
- Verify whether it is accepted as session identity.

Interpretation:

- If accepted: evidence of a possible token-purpose separation bug.
- If rejected: risk is refuted.

No bug is declared or fixed at this stage.

## Planned technical strategy

Primary testing targets:

- Router functions invoked directly.
- auth_services.
- auth_security.

With controlled dependencies. TestClient is not the primary strategy because the target is business logic, not HTTP transport behavior.

Dependencies requiring isolation/control:

- TinyDB.
- Resend.
- Time and expiration behavior.
- JWT handling where applicable.
- Password hashing where applicable.

Fixture design is intentionally deferred to implementation phase.

## Planned execution

Planned commands for this ticket:

- uv run pytest
- uv run pytest --cov

Current status: testing infrastructure for this plan is not yet installed/configured in this phase.

Coverage goal for auth scope: at least 70%.

## TypeScript and Jest

- The monorepo contains TypeScript utility logic.
- Jest is not configured yet.
- Jest setup and TS utility tests will be handled in a later phase.
- Target command will be jest --coverage (or the equivalent script finally configured).

## Optional backlog

After AUTH-088 completion, the following may be addressed:

- API-042: backend/backoffice tests.
- FE-019: frontend utility tests.

These are not part of the current mandatory blocking scope.

## Results placeholder

Status: Not executed yet.

Fields for future update:

- Total tests:
- Tests passed:
- Auth coverage:
- Jest coverage (if applicable):
- Bugs discovered:
