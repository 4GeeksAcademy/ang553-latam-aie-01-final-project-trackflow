# TrackFlow MCP Server

Independent Python environment for the OAuth-protected TrackFlow MCP server.
It intentionally keeps MCP Auth dependencies separate from the existing
TrackFlow API runtime because `mcpauth` requires `requests>=2.32.3`, while the
API's current email dependency resolves to `requests==2.31.0`.

The server will use MCP Python SDK v1 (`mcp>=1.28,<2`) because the academically
required `langchain-mcp-adapters==0.3.2` client integration is not compatible
with MCP SDK v2.

Resolve and verify this environment from the repository root:

```powershell
uv lock --project mcps/trackflow --check
uv run --project mcps/trackflow python -c "import mcp, mcpauth"
```

The implementation provides a stateless Streamable HTTP `/mcp` application,
OAuth Protected Resource Metadata, `mcpauth` JWT validation, exact
operation-scope guards, a confidential Keycloak Standard Token Exchange V2
client, and typed downstream calls to the existing TrackFlow API. It does not
use the authentication helpers built into FastMCP.

Runtime configuration is supplied through environment variables; credentials
must never be committed:

- `TRACKFLOW_MCP_RESOURCE_URL`
- `TRACKFLOW_MCP_ISSUER`
- `TRACKFLOW_MCP_AUDIENCE`
- `TRACKFLOW_MCP_AUTHORIZATION_ENDPOINT` (optional Keycloak-derived default)
- `TRACKFLOW_MCP_TOKEN_ENDPOINT` (optional Keycloak-derived default)
- `TRACKFLOW_MCP_JWKS_URI` (optional Keycloak-derived default)
- `TRACKFLOW_MCP_KEYCLOAK_CLIENT_ID`
- `TRACKFLOW_MCP_KEYCLOAK_CLIENT_SECRET`

The advertised and enforceable operation scopes are exactly
`incidents:create`, `incidents:read`, `incidents:status`, and
`inventory:read`. Roles and client tenancy are not accepted from token claims.

| Tool | Required scope | Existing TrackFlow endpoint |
| --- | --- | --- |
| `create_incident` | `incidents:create` | `POST /api/incidents` |
| `get_incident` | `incidents:read` | `GET /api/incidents/{incident_id}` |
| `update_incident_status` | `incidents:status` | `PATCH /api/incidents/{incident_id}/status` |
| `list_inventory_products` | `inventory:read` | `GET /inventory/products` |
| `get_inventory_product` | `inventory:read` | `GET /inventory/products/{product_id}` |

The server exposes exactly five business tools:

- `create_incident`
- `get_incident`
- `update_incident_status`
- `list_inventory_products`
- `get_inventory_product`

They call the existing TrackFlow HTTP API with an exchanged
`aud=trackflow-api` bearer and propagate a UUIDv4 `X-Request-ID`. Incident and
inventory responses are reduced to safe projections; inventory integration is
strictly GET-only. Set `TRACKFLOW_API_BASE_URL` and optionally
`TRACKFLOW_MCP_DOWNSTREAM_TIMEOUT_SECONDS` alongside the OAuth variables.

The downstream API maps `trackflow_user_id` to an active TrackFlow user and
derives role and client memberships only from TrackFlow persistence; token
claims for roles or tenants are ignored.

The boundary is deliberately layered:

1. `mcpauth` verifies the external MCP bearer signature, issuer, audience,
   expiration and scopes, and binds request-local identity.
2. The confidential MCP client exchanges that bearer for a distinct token with
   audience `trackflow-api`; the incoming token is never passed downstream.
3. The TrackFlow API links `trackflow_user_id` to an active local user and
   rebuilds role and tenant access from TrackFlow persistence.
4. Tools call only the existing HTTP contracts. They do not import database or
   domain services and do not duplicate lifecycle rules.

Incident outputs contain only `incident_id`, `category`, `status`, timestamps
and `version`. Inventory outputs contain only product ID, name, SKU, category,
warehouse and current stock. The inventory adapter rejects non-GET methods and
paths outside the two approved product routes before making a network request.
Every downstream request carries a UUIDv4 `X-Request-ID`; structured logs omit
tokens, request payloads and PII.

## Disposable real integration QA

The opt-in utilities under `qa/` contain no credentials. They require
`TRACKFLOW_MCP_QA=1` and runtime-only environment variables, and refuse database
or Keycloak targets outside the dedicated loopback ports. The integration test
is skipped unless `TRACKFLOW_MCP_REAL_QA=1`; it expects disposable Keycloak,
PostgreSQL, API, and MCP processes and never provisions persistent volumes.

The public QA client supports Authorization Code with mandatory PKCE S256 and
the exact Inspector callbacks `http://localhost:6274/oauth/callback` and
`http://127.0.0.1:6274/oauth/callback`. Direct Access Grant remains enabled only
for automated disposable QA; its successful use is not evidence of OAuth 2.1
interactive-flow conformance.

The complete secret-free reproduction and cleanup procedure is in
[`qa/README.md`](qa/README.md). The final fresh disposable gate passed
**1 test in 13.90 s** (18.38 s command wall time): PRM, real Authorization Code
+ PKCE S256, dual-session isolation, Keycloak login/token exchange, API/MCP
authorization, all five tools, LangGraph through `langchain-mcp-adapters`,
tenant hiding and optimistic concurrency. An observed Keycloak 26 loopback
Secure-cookie mismatch in Python's cookie jar and the adapter's list-shaped
error `ToolMessage` were corrected fail-closed and covered by regression tests.
All QA services, containers, temporary data and secret-bearing environment
values were removed; the dedicated ports are free.

## LangGraph support integration

`POST /support/query` now has an exclusive Keycloak/OIDC boundary. It requires
an RS256 token with audience `trackflow-mcp`, the linked `trackflow_user_id`,
and both `incidents:read` and `inventory:read`. The local HS256 token is
intentionally not accepted on this one endpoint. The linked TrackFlow user must
be active and have the local admin or manager role; external role and tenant
claims are ignored.

The endpoint and external support graph are async and use `graph.ainvoke`.
Ticket and inventory nodes call `get_incident` and `get_inventory_product`
exclusively through `langchain-mcp-adapters==0.3.2`; there is no direct-service
fallback. Clarification, RAG, shared deadlines, partial results, sanitized
errors, traces and ephemeral checkpoints remain in the graph.

Backoffice retains its local JWT for navigation and every other API. Support
uses a separate Authorization Code + PKCE S256 session with a public Keycloak
client. Only state and verifier temporarily use `sessionStorage`; the OIDC
access token remains in memory and no client secret is shipped to the browser.
