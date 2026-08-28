# TESTING

## Purpose

This document describes the final testing strategy, implemented test coverage, execution results, discovered regressions, and manual QA evidence for TrackFlow ticket AUTH-088.

The primary objective of AUTH-088 is to provide meaningful automated coverage for the authentication domain, with emphasis on business logic rather than framework internals.

The ticket also includes Jest coverage for applicable TypeScript utility logic present in the monorepo.

---

## Testing principles

The implemented test suite follows these principles:

- Tests focus on business logic rather than FastAPI, Pydantic, or HTTP serialization internals.
- Tests are independent and deterministic.
- TinyDB is isolated from the real application database during tests.
- Tests do not use production-like user data.
- External email delivery through Resend is mocked in automated tests.
- Token expiration behavior is controlled explicitly without `sleep`.
- Password hashing and JWT handling use the real application logic whenever practical.
- Mocks are limited primarily to external boundaries.
- Coverage is used as a quality signal rather than as a goal to reach 100%.

---

# AUTH-088 scope

The mandatory authentication scope covered by this ticket is:

1. `POST /users`
2. `POST /auth/login`
3. `GET /auth/me`
4. `POST /auth/forgot-password`
5. `POST /auth/reset-password`
6. `POST /auth/change-password`

`POST /users` is included in the authentication scope because it represents the public signup flow that creates a user identity.

Each mandatory endpoint has coverage for at least:

- happy path;
- edge case;
- failure mode.

General user CRUD and profile CRUD outside what is required by these six authentication flows remain outside the mandatory AUTH-088 scope.

---

# Implemented authentication test matrix

| ID | Endpoint | Type | Implemented behavior |
| --- | --- | --- | --- |
| AUTH-USERS-HP-01 | POST /users | Happy path | Valid signup creates a standard active user and persists a hashed password. |
| AUTH-USERS-EDGE-01 | POST /users | Edge case | Signup succeeds with optional/partial profile data. |
| AUTH-USERS-FAIL-01 | POST /users | Failure mode | Duplicate email is rejected without replacing the original user. |
| AUTH-USERS-FAIL-02 | POST /users | High value | If profile creation fails after user creation, the user creation is rolled back. |
| AUTH-LOGIN-HP-01 | POST /auth/login | Happy path | Correct credentials for an active user return a valid access token. |
| AUTH-LOGIN-EDGE-01 | POST /auth/login | Edge case | Email case/spacing differences are handled using the application's existing normalization. |
| AUTH-LOGIN-FAIL-01 | POST /auth/login | Failure mode | Incorrect password is rejected. |
| AUTH-LOGIN-FAIL-02 | POST /auth/login | High value | Non-existent user receives the same generic authentication failure behavior. |
| AUTH-LOGIN-FAIL-03 | POST /auth/login | High value | Inactive user is rejected even with the correct password. |
| AUTH-ME-HP-01 | GET /auth/me | Happy path | Valid access token resolves the correct active user. |
| AUTH-ME-EDGE-01 | GET /auth/me | Edge case | A valid token is rejected when its user no longer exists. |
| AUTH-ME-FAIL-01 | GET /auth/me | Failure mode | Expired access token is rejected deterministically. |
| AUTH-ME-FAIL-02 | GET /auth/me | High value | Token with invalid signature is rejected. |
| AUTH-ME-FAIL-03 | GET /auth/me | High value | User deactivated after token issuance is rejected. |
| AUTH-FORGOT-HP-01 | POST /auth/forgot-password | Happy path | Existing user produces a persisted reset token and executes the email delivery boundary. |
| AUTH-FORGOT-EDGE-01 | POST /auth/forgot-password | Edge case | Non-existent email returns the same generic response without exposing account existence. |
| AUTH-FORGOT-FAIL-01 | POST /auth/forgot-password | Failure mode | Email delivery failure invalidates the issued reset token and returns a controlled server error. |
| AUTH-FORGOT-FAIL-02 | POST /auth/forgot-password | High value | Reset-token issuance failure is handled without sending email or leaving partial state. |
| AUTH-RESET-HP-01 | POST /auth/reset-password | Happy path | Valid unused reset token changes the password and becomes unusable afterward. |
| AUTH-RESET-EDGE-01 | POST /auth/reset-password | Edge case | Already-used reset token is rejected without modifying the current password. |
| AUTH-RESET-FAIL-01 | POST /auth/reset-password | Failure mode | Expired reset token is rejected both at JWT expiration and persisted `expires_at` validation. |
| AUTH-RESET-FAIL-02 | POST /auth/reset-password | High value | Token with a purpose/type other than `password_reset` is rejected. |
| AUTH-RESET-FAIL-03 | POST /auth/reset-password | High value | Valid reset token is rejected when its associated user no longer exists. |
| AUTH-CHPASS-HP-01 | POST /auth/change-password | Happy path | Correct current password allows replacement with a new password. |
| AUTH-CHPASS-EDGE-01 | POST /auth/change-password | Edge case | Empty current password reaches domain logic and is rejected. |
| AUTH-CHPASS-FAIL-01 | POST /auth/change-password | Failure mode | Incorrect current password is rejected without changing the stored hash. |
| AUTH-CHPASS-OPT-01 | POST /auth/change-password | Additional | Missing user record produces the application's defined `User not found` failure. |

Additional router-level checks confirm that selected domain `ValueError` failures are translated into the expected HTTP error without making HTTP transport serialization the primary focus of the suite.

---

# Critical token regression coverage

AUTH-088 was implemented after concerns around token expiration behavior.

The final suite explicitly protects the following regressions:

- expired access token rejection;
- expired password-reset JWT rejection;
- expired persisted reset-token state rejection;
- password-reset token single-use enforcement;
- rejection of already-used reset tokens;
- rejection of tokens with the wrong purpose/type;
- rejection of access tokens whose user was removed;
- rejection of access tokens whose user was deactivated.

Expiration tests are deterministic and do not depend on `sleep`.

---

# AI-assisted security test discovery

During review of the authentication implementation, an AI-assisted test hypothesis identified a possible token-purpose separation issue.

## Hypothesis

Password-reset tokens contain a valid:

- `sub`;
- `exp`;
- signed JWT structure;

and `get_current_user` appeared to validate the token signature, expiration, user identity, and active state without validating the token purpose.

A dedicated regression test was added in:

```text
tests/test_auth_token_purpose.py
```

## Bug confirmed

The test demonstrated that a valid password-reset token could be supplied to:

```text
get_current_user
```

and was accepted as authenticated session identity.

This confirmed a real token-purpose separation vulnerability.

## Root cause

`get_current_user` validated:

- JWT signature;
- expiration;
- `sub`;
- user existence;
- active user state;

but did not validate whether the JWT was intended to be used as an access token.

## Correction

The authentication contract was updated so that newly created access tokens contain:

```text
type=access
```

Password-reset tokens continue to use:

```text
type=password_reset
```

`get_current_user` now rejects tokens with an inappropriate purpose.

## Legacy access-token compatibility

Access tokens issued before this correction did not contain a `type` claim.

Because access tokens have a short lifetime, compatibility was preserved for still-valid legacy access tokens by accepting:

```text
type=access
```

or absence of the `type` claim.

The session authentication path rejects:

```text
type=password_reset
```

and other unsupported token types.

This allows existing short-lived access sessions to expire naturally without allowing password-reset tokens to authenticate as sessions.

## Regression protection

The final automated suite verifies both:

- password-reset tokens cannot authenticate a session;
- legacy access tokens without `type` remain accepted during their remaining lifetime.

---

# Pytest infrastructure

Automated backend tests use:

- `pytest`;
- `pytest-cov`;
- application JWT logic;
- application password hashing;
- temporary TinyDB persistence.

Test infrastructure is located under:

```text
tests/
```

The TinyDB fixture patches the authentication tables used by the relevant runtime modules so tests operate against temporary storage rather than:

```text
services/api/data/auth.json
```

The real authentication database file is protected from test mutation.

---

# Backend execution

## Full backend suite

Primary command:

```bash
uv run pytest
```

Final result:

```text
32 passed
0 failed
0 skipped
```

The verbose form was also executed:

```bash
uv run pytest -v
```

with the same final result.

---

# Authentication coverage

The final authentication coverage measurement was executed with:

```bash
uv run pytest \
  tests/test_auth_register.py \
  tests/test_auth_login.py \
  tests/test_auth_me.py \
  tests/test_auth_forgot_password.py \
  tests/test_auth_reset_password.py \
  tests/test_auth_change_password.py \
  tests/test_auth_token_purpose.py \
  --cov=services.api.auth_security \
  --cov=services.api.auth_services \
  --cov=services.api.routes.auth \
  --cov=services.api.routes.users \
  --cov-report=term-missing
```

Final coverage:

| Module | Statements | Missing | Coverage |
| --- | ---: | ---: | ---: |
| `services.api.auth_security` | 62 | 2 | 97% |
| `services.api.auth_services` | 170 | 47 | 72% |
| `services.api.routes.auth` | 62 | 10 | 84% |
| `services.api.routes.users` | 65 | 31 | 52% |
| **TOTAL** | **359** | **90** | **75%** |

AUTH-088 authentication coverage target:

```text
>= 70%
```

Final result:

```text
75% — target met
```

The lower percentage in `routes.users` is expected because that module also contains user-management CRUD routes outside the six mandatory authentication flows covered by AUTH-088.

Coverage was not increased by adding unrelated tests only to improve the percentage.

---

# TypeScript and Jest

The monorepo contains shared TypeScript business utilities suitable for unit testing.

Jest was configured with:

- `jest`;
- `ts-jest`;
- `@types/jest`;
- Node test environment;
- TypeScript test configuration;
- coverage reporting.

The TypeScript production configuration was not changed to accommodate Jest.

---

# TypeScript utility scope

The final Jest suite covers three shared validation functions:

```text
validateProduct
validateShipment
validateCarrier
```

located in:

```text
src/utils/validations.ts
```

Each function has:

- happy path;
- failure mode;
- meaningful boundary coverage.

---

## validateProduct

Covered behavior includes:

- valid product;
- empty/whitespace SKU rejection;
- upper weight boundary;
- weight above the supported boundary.

---

## validateShipment

Covered behavior includes:

- valid shipment;
- invalid zero quantity;
- zero-distance boundary;
- negative distance rejection.

---

## validateCarrier

Covered behavior includes:

- valid carrier;
- out-of-range `onTimeRate`;
- inclusive `0` and `100` boundaries;
- rejection below `0`;
- rejection above `100`.

---

# Jest execution

Normal test command:

```bash
npm test -- --runInBand
```

Final result:

```text
1 test suite passed
10 tests passed
0 failed
0 skipped
```

Coverage command:

```bash
npx jest --coverage --runInBand
```

The equivalent package script also works:

```bash
npm run test:coverage -- --runInBand
```

---

# TypeScript utility coverage

Final coverage for:

```text
src/utils/validations.ts
```

was:

| Metric | Coverage |
| --- | ---: |
| Statements | 71.11% |
| Branches | 71.73% |
| Functions | 100% |
| Lines | 71.11% |

All three selected validation functions are executed by the suite.

No attempt was made to force 100% coverage by adding low-value tests.

---

# External service isolation

Automated forgot-password tests do not invoke the real Resend provider.

The email boundary is mocked while:

- reset-token generation;
- reset-token persistence;
- reset-token invalidation;
- user state;

continue to use the real application logic where appropriate.

This prevents automated tests from depending on network connectivity or third-party credentials.

---

# Manual QA

A manual authentication QA pass was completed using GitHub Codespaces with the real FastAPI backend and Next.js backoffice frontend.

## Environment

Backend:

```text
FastAPI
port 8000
```

Frontend:

```text
Next.js backoffice
port 3000
```

For cross-origin browser testing:

- frontend port remained `Private`;
- backend port was temporarily changed to `Public`;
- `NEXT_PUBLIC_API_URL` pointed to the forwarded backend URL.

No Codespaces-specific URL is stored in this document.

---

## Connectivity preflight

Before authentication testing, the following were verified:

- FastAPI started successfully on `0.0.0.0:8000`;
- backend `/docs` was reachable through the forwarded URL;
- Next.js backoffice started successfully on port `3000`;
- frontend used the forwarded backend URL rather than `127.0.0.1`;
- browser requests reached FastAPI;
- CORS preflight completed successfully;
- requests were visible in both browser Network tools and Uvicorn logs.

A deliberately invalid login request reached:

```text
POST /auth/login
```

and received application-level:

```text
401 Unauthorized
```

confirming that connectivity was working before functional authentication QA began.

---

# Manual authentication checks

| Check | Result |
| --- | --- |
| Register new QA user | PASS |
| Automatic authenticated entry after registration | PASS |
| Session persists after browser reload | PASS |
| Logout | PASS |
| Login again with created user | PASS |
| Protected Profile page loads authenticated user's information | PASS |
| Removal of local authentication state redirects user to login | PASS |
| Frontend → backend connectivity | PASS |
| CORS / browser preflight | PASS |

The manual QA user was test-only data and is not part of the repository.

---

# Forgot-password manual QA

The forgot-password UI was tested manually using the QA account.

Observed backend behavior:

```text
OPTIONS /auth/forgot-password → 200 OK
POST /auth/forgot-password → 500 Internal Server Error
```

The frontend displayed a controlled message indicating that the password reset service was temporarily unavailable.

This demonstrates that:

- forgot-password UI is reachable;
- frontend sends the expected request;
- browser preflight succeeds;
- request reaches FastAPI;
- backend returns a controlled failure;
- frontend handles the failure.

The external email provider/configuration was not available for full end-to-end delivery during this QA session.

Therefore:

```text
Real email delivery: NOT VERIFIED
Password reset via received email: NOT VERIFIED
```

This limitation does not replace the automated forgot/reset tests, which cover token generation, expiration, persistence, invalidation, single-use behavior, and failure handling with the external email boundary mocked.

---

# Change-password manual QA

The current backoffice UI does not expose a visible change-password flow.

Therefore:

```text
Change-password UI QA: NOT EXECUTED
```

The service and endpoint logic remain covered by automated pytest tests for:

- successful password change;
- incorrect current password;
- empty current password;
- missing user;
- HTTP error mapping.

---

# Known limitations and technical debt

## External email provider

Full forgot-password/reset-password E2E testing depends on an operational email provider and valid environment configuration.

That provider was unavailable during the manual QA session.

The automated suite does not depend on the real provider.

---

## Global TypeScript typecheck

The following command was also evaluated:

```bash
npm run typecheck
```

The repository currently reports `TS2835` errors related to extensionless imports under NodeNext in existing TypeScript source files including:

```text
collections.ts
search.ts
transformations.ts
validations.ts
```

This global typecheck condition was not introduced by AUTH-088.

The issue was not changed as part of this ticket because correcting production TypeScript imports would expand the scope beyond authentication/testing work.

Jest/ts-jest successfully transpiles and executes the selected utility tests.

---

## ts-jest / NodeNext warning handling

During Jest configuration, `ts-jest` emitted diagnostic `TS151002` for the NodeNext test environment.

Testing with:

```text
isolatedModules=true
```

caused an ESM runtime incompatibility in this repository setup.

The Jest-only configuration therefore ignores diagnostic:

```text
151002
```

while leaving the production TypeScript configuration unchanged.

This is considered minor testing infrastructure technical debt and is not currently blocking Jest execution.

---

# Test files

Authentication tests include:

```text
tests/test_auth_register.py
tests/test_auth_login.py
tests/test_auth_me.py
tests/test_auth_forgot_password.py
tests/test_auth_reset_password.py
tests/test_auth_change_password.py
tests/test_auth_token_purpose.py
tests/test_test_infrastructure.py
```

TypeScript tests include:

```text
tests/validations.test.ts
```

Shared pytest fixtures are located in:

```text
tests/conftest.py
```

---

# Optional backlog

The following work remains separate from the mandatory AUTH-088 scope:

## API-042

Additional backend/backoffice endpoint groups outside authentication may receive their own happy/edge/failure coverage.

## FE-019

Additional frontend-specific utilities and browser-facing helpers may receive broader unit coverage.

The Jest work completed in AUTH-088 validates applicable shared TypeScript utility logic but does not attempt to complete all possible frontend test coverage.

---

# Final results

## Backend

```text
pytest:
32 passed
0 failed
```

Authentication coverage:

```text
75%
```

Target:

```text
>= 70%
```

Status:

```text
PASS
```

---

## TypeScript / Jest

```text
Jest:
10 passed
0 failed
```

`validations.ts` coverage:

```text
Statements: 71.11%
Branches:   71.73%
Functions:  100%
Lines:      71.11%
```

Status:

```text
PASS
```

---

## Security regression

AI-assisted test discovery found a real token-purpose vulnerability:

```text
password-reset token accepted as session token
```

The issue was corrected and protected by regression tests.

Status:

```text
PASS
```

---

## Manual QA

Core browser authentication flow:

```text
PASS
```

External email reset E2E:

```text
NOT VERIFIED — external provider unavailable
```

Change-password UI:

```text
NOT AVAILABLE IN CURRENT BACKOFFICE
```

---

# AUTH-088 final testing status

AUTH-088 testing requirements implemented in this branch include:

- all mandatory authentication endpoints covered;
- happy path per endpoint;
- edge case per endpoint;
- failure mode per endpoint;
- deterministic token expiration coverage;
- password-reset single-use regression protection;
- TinyDB test isolation;
- external email isolation;
- pytest execution;
- authentication coverage above 70%;
- Jest configuration for applicable TypeScript utility logic;
- happy/failure/boundary Jest tests;
- TypeScript utility coverage reporting;
- AI-assisted security regression discovery and correction;
- real browser manual QA;
- documented environment limitations and known technical debt.

**AUTH-088 testing implementation is ready for final repository QA.**

