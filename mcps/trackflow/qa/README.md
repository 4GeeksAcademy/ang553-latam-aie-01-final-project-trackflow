# Disposable MCP integration QA

> GitHub Codespaces academic validation uses the separate, fail-closed
> [Codespaces QA runbook](codespaces/README.md). That launcher keeps every
> port private and requires the designated Playground callback as an explicit
> input; it does not publish services.

This runbook reproduces the TrackFlow MCP integration gate with isolated,
disposable services. It is for local QA only. It is not a production
deployment guide, and none of the example values are credentials.

The scripts fail closed unless `TRACKFLOW_MCP_QA=1`. Keycloak must be the
dedicated loopback endpoint `http://127.0.0.1:18082`, PostgreSQL must be
`127.0.0.1:55440`, and the TinyDB authentication file must be below the
operating-system temporary directory.

## Preflight

Run from the repository root in PowerShell. Docker Desktop must be running,
Python 3.12 must already be available to `uv`, and these ports must be free:

```powershell
$qaPorts = 18082, 55440, 18083, 18084
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalPort -in $qaPorts }

docker ps -a --filter "name=trackflow-mcp-qa-keycloak-2682" `
  --filter "name=trackflow-mcp-qa-postgres-16"
```

Stop if either command reports a collision. Do not reuse, stop, or remove an
unrelated container. The reserved resources are:

| Component | Exact QA name | Loopback endpoint |
| --- | --- | --- |
| Keycloak 26.8.2 | `trackflow-mcp-qa-keycloak-2682` | `127.0.0.1:18082` |
| PostgreSQL 16 | `trackflow-mcp-qa-postgres-16` | `127.0.0.1:55440` |
| TrackFlow API | host process | `127.0.0.1:18083` |
| TrackFlow MCP | host process | `127.0.0.1:18084` |

Use an OS-temporary working directory. Generate all passwords and client
secrets at runtime, retain them only in the current process environment, and
never paste their values into commands, logs, screenshots, or files. The
placeholders below describe required values; replace them only in the shell:

```text
TRACKFLOW_MCP_QA=1
TRACKFLOW_MCP_REAL_QA=1
QA_KEYCLOAK_BASE_URL=http://127.0.0.1:18082
QA_KEYCLOAK_ADMIN_USER=<generated-admin-name>
QA_KEYCLOAK_ADMIN_PASSWORD=<generated-admin-password>
QA_KEYCLOAK_REALM=<unique-qa-realm>
QA_KEYCLOAK_PUBLIC_CLIENT_ID=mcp-playground-qa
QA_KEYCLOAK_MCP_CLIENT_ID=trackflow-mcp
QA_KEYCLOAK_MCP_CLIENT_SECRET=<generated-confidential-client-secret>
QA_KEYCLOAK_API_CLIENT_ID=trackflow-api
QA_KEYCLOAK_USERNAME=<generated-qa-username>
QA_KEYCLOAK_PASSWORD=<generated-qa-password>
QA_TRACKFLOW_USER_ID=<generated-uuid>
QA_TRACKFLOW_LOCAL_PASSWORD=<generated-local-shell-password>
DATABASE_URL=postgresql+psycopg2://<qa-db-user>:<qa-db-password>@127.0.0.1:55440/<qa-db-name>
TRACKFLOW_AUTH_DATABASE_PATH=<os-temp-directory>/trackflow-mcp-auth.json
JWT_SECRET_KEY=<generated-local-api-secret>
```

## Start the disposable dependencies

Use the exact names and loopback bindings. PostgreSQL data must remain in
`tmpfs`; do not attach a persistent volume.

```powershell
docker run --name trackflow-mcp-qa-keycloak-2682 --rm -d `
  -p 127.0.0.1:18082:8080 `
  -e KC_BOOTSTRAP_ADMIN_USERNAME=$env:QA_KEYCLOAK_ADMIN_USER `
  -e KC_BOOTSTRAP_ADMIN_PASSWORD=$env:QA_KEYCLOAK_ADMIN_PASSWORD `
  quay.io/keycloak/keycloak:26.8.2 start-dev

docker run --name trackflow-mcp-qa-postgres-16 --rm -d `
  --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=256m `
  -p 127.0.0.1:55440:5432 `
  -e POSTGRES_USER=<qa-db-user> `
  -e POSTGRES_PASSWORD=<qa-db-password> `
  -e POSTGRES_DB=<qa-db-name> `
  postgres:16
```

Wait for both services to become ready. Apply
`infra/sql/incident_query_security.sql` only to this disposable database. One
reviewable PowerShell form is:

```powershell
Get-Content -Raw infra/sql/incident_query_security.sql |
  docker exec -i trackflow-mcp-qa-postgres-16 `
    psql -v ON_ERROR_STOP=1 -U <qa-db-user> -d <qa-db-name>
```

Configure the isolated realm and seed only the temporary TrackFlow stores:

```powershell
uv run --project mcps/trackflow --python 3.12 `
  python mcps/trackflow/qa/configure_keycloak.py

uv run --python 3.12 python mcps/trackflow/qa/seed_trackflow.py
```

The realm config registers Authorization Code with mandatory PKCE S256 and
only these MCP Inspector callbacks:

- `http://localhost:6274/oauth/callback`
- `http://127.0.0.1:6274/oauth/callback`

Direct Access Grant is enabled solely so the automated disposable test can
obtain a token without a browser. Its success is not evidence of an OAuth 2.1
interactive authorization flow. Human Inspector acceptance must use the
pre-registered PKCE flow.

The same public client registers only the Backoffice support callbacks
`http://localhost:3001/backoffice/support` and
`http://127.0.0.1:3001/backoffice/support`, plus their matching loopback web
origins. The E2E gate completes a real Authorization Code + PKCE S256 flow and
validates `state`; Direct Access Grant does not accredit the Support login.

## Start API and MCP

Set the following derived, non-secret URLs in the same environment as their
corresponding process:

```text
TRACKFLOW_OIDC_ISSUER=http://127.0.0.1:18082/realms/<unique-qa-realm>
TRACKFLOW_OIDC_JWKS_URI=http://127.0.0.1:18082/realms/<unique-qa-realm>/protocol/openid-connect/certs
TRACKFLOW_MCP_RESOURCE_URL=http://127.0.0.1:18084/mcp
TRACKFLOW_MCP_ISSUER=http://127.0.0.1:18082/realms/<unique-qa-realm>
TRACKFLOW_MCP_AUDIENCE=trackflow-mcp
TRACKFLOW_MCP_KEYCLOAK_CLIENT_ID=trackflow-mcp
TRACKFLOW_MCP_KEYCLOAK_CLIENT_SECRET=<same-runtime-generated-secret>
TRACKFLOW_API_BASE_URL=http://127.0.0.1:18083
TRACKFLOW_SUPPORT_MCP_URL=http://127.0.0.1:18084/mcp
```

Start each command in its own PowerShell process so the environment is
inherited without adding credentials to command-line arguments:

```powershell
uv run --python 3.12 uvicorn services.api.main:app `
  --host 127.0.0.1 --port 18083

uv run --project mcps/trackflow --python 3.12 `
  uvicorn trackflow_mcp.runtime:create_runtime_app --factory `
  --host 127.0.0.1 --port 18084
```

Do not use `--reload`: the gate needs one predictable process and must not copy
the secret-bearing environment into file watchers.

## Execute the real gate

Set these test aliases to the already configured endpoints:

```text
QA_TOKEN_ENDPOINT=http://127.0.0.1:18082/realms/<unique-qa-realm>/protocol/openid-connect/token
QA_JWKS_URI=http://127.0.0.1:18082/realms/<unique-qa-realm>/protocol/openid-connect/certs
QA_ISSUER=http://127.0.0.1:18082/realms/<unique-qa-realm>
QA_API_URL=http://127.0.0.1:18083
QA_MCP_URL=http://127.0.0.1:18084/mcp
QA_PRM_URL=http://127.0.0.1:18084/.well-known/oauth-protected-resource/mcp
```

Then run:

```powershell
uv run --project mcps/trackflow --python 3.12 `
  pytest mcps/trackflow/qa/test_e2e.py -q -p no:cacheprovider
```

The test validates PRM, real RS256 claims, issuer/audience/expiration, Standard
Token Exchange V2, distinct downstream bearer, identity linking, exact tool
discovery, incident persistence and optimistic concurrency, inventory reads,
tenant hiding, scope denial, inventory mutation rejection, and the async
`/support/query` path through LangGraph, `langchain-mcp-adapters`, and the real
MCP tools. A skip is not a pass; `TRACKFLOW_MCP_REAL_QA=1` must be present.

## Manual Backoffice acceptance on port 3001

The Backoffice shell uses the existing local JWT while Support uses a separate
OIDC session. Start it from the repository root with the API and the disposable
Keycloak/MCP stack already ready. The three `NEXT_PUBLIC_*` values below are
public endpoints/client metadata; never add a client secret to the browser:

```powershell
$env:API_BACKEND_URL='http://127.0.0.1:18083'
$env:NEXT_PUBLIC_SUPPORT_OIDC_AUTHORIZATION_ENDPOINT='http://127.0.0.1:18082/realms/<unique-qa-realm>/protocol/openid-connect/auth'
$env:NEXT_PUBLIC_SUPPORT_OIDC_TOKEN_ENDPOINT='http://127.0.0.1:18082/realms/<unique-qa-realm>/protocol/openid-connect/token'
$env:NEXT_PUBLIC_SUPPORT_OIDC_CLIENT_ID='mcp-playground-qa'

Push-Location uis/backoffice
npm run dev -- --hostname 127.0.0.1 --port 3001
Pop-Location
```

Open `http://127.0.0.1:3001/backoffice/support`. Authenticate the existing
Backoffice shell with the fixed disposable user
`mcp-qa-user@example.invalid` and the runtime-only value held in
`QA_TRACKFLOW_LOCAL_PASSWORD`. The Support connection then redirects to the
public Keycloak client and must return to the exact callback
`http://127.0.0.1:3001/backoffice/support` using Authorization Code + PKCE
S256. Use the runtime QA Keycloak username/password at that prompt. The local
JWT must remain authoritative for the shell; the in-memory OIDC token must be
authoritative only for `POST /support/query`.

Exercise these manual scenarios without inventing identifiers:

1. **Knowledge:** ask a policy question backed by the existing private RAG
   corpus. This scenario requires the repository's real private RAG/model
   configuration; if it is unavailable, report it as blocked and do not create
   substitute credentials, sources or answers.
2. **Ticket:** ask for the status of the incident ID created by the disposable
   E2E (for example, use its observed `TRF-......` value, never a fabricated
   production ID).
3. **Inventory:** ask for seeded disposable product `91001`.
4. **Mixed:** ask for that observed incident and product `91001` in one query;
   both operational nodes must traverse the real MCP server.
5. **Clarification:** submit the proven ticket-intent request
   `Check my ticket status` without an ID and verify that the graph asks for
   exactly one valid incident identifier without performing a lookup.

For reference, the public metadata values are:

```text
NEXT_PUBLIC_SUPPORT_OIDC_AUTHORIZATION_ENDPOINT=http://127.0.0.1:18082/realms/<unique-qa-realm>/protocol/openid-connect/auth
NEXT_PUBLIC_SUPPORT_OIDC_TOKEN_ENDPOINT=http://127.0.0.1:18082/realms/<unique-qa-realm>/protocol/openid-connect/token
NEXT_PUBLIC_SUPPORT_OIDC_CLIENT_ID=mcp-playground-qa
```

The normal Backoffice JWT remains required for its shell and all non-Support
APIs. The seed hashes `QA_TRACKFLOW_LOCAL_PASSWORD` only into the disposable
TinyDB so `/auth/login` and `/auth/me` can be accepted before the PKCE redirect.
The OIDC token is separate and authoritative only for Support; the E2E verifies
that neither bearer is accepted at the other session's endpoint.

No QA services are intentionally left running after automated acceptance.
There is no secure channel in this handoff for transferring the ephemeral
passwords and confidential-client secret without persisting or exposing them.
Reproduce the stack from this runbook in one controlled shell for human
acceptance, then perform the exact cleanup below.

## Exact cleanup

Stop the two host processes by the PIDs that were recorded when they were
started. Verify each PID still owns port 18083 or 18084 before stopping it.
Remove only the two exact QA container names:

```powershell
docker stop trackflow-mcp-qa-keycloak-2682
docker stop trackflow-mcp-qa-postgres-16

$qaPorts = 18082, 55440, 18083, 18084
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalPort -in $qaPorts }

docker ps -a --filter "name=trackflow-mcp-qa-keycloak-2682" `
  --filter "name=trackflow-mcp-qa-postgres-16"
```

Delete the temporary TinyDB/work directory only after resolving its absolute
path and confirming that it remains below the operating-system temp directory.
Never use a broad recursive target, wildcard, repository path, or persistent
TrackFlow data directory. Clear the secret-bearing shell environment or close
that shell after cleanup.
