# TrackFlow MCP - Codespaces QA

This directory prepares the disposable stack required for human acceptance
with an MCP Playground that accepts an arbitrary `Authorization: Bearer`
header. Authentication uses OAuth 2.0 Device Authorization Grant. The
launcher never changes port visibility; the only forwarding candidate is the
loopback gateway on port `18085`.

## Trust boundaries

| Component | Binding | Public route |
|---|---|---|
| PostgreSQL QA | `127.0.0.1:55440` | none |
| Keycloak 26.8.2 | `127.0.0.1:18082` | selected realm through gateway |
| TrackFlow API | `127.0.0.1:18083` | none |
| TrackFlow MCP | `127.0.0.1:18084` | `/mcp` and PRM through gateway |
| restrictive gateway | `127.0.0.1:18085` | future Codespaces URL |

PostgreSQL uses `tmpfs`; Keycloak data lives in a disposable container
`tmpfs`; the TinyDB authentication file and process logs live in a
mode-`0700` temporary directory. Cleanup is scoped to the Compose project and
the PIDs recorded by this launcher.

The gateway returns `404` for Keycloak administration, the `master` realm,
normal browser authorization, account, logout, API routes and all unrelated
paths. It forwards only MCP/PRM, OIDC discovery, token/JWKS, the Device
Authorization endpoint, device verification, its exact login action and
Keycloak's versioned login assets.

The Keycloak public QA client is hardened to Device Authorization Grant only:
Standard Flow, Direct Access Grants and service accounts are disabled,
`fullScopeAllowed=false`, and redirect URIs/web origins are empty. Anonymous
Dynamic Client Registration is disabled. Tokens carry audience
`trackflow-mcp`, the linked `trackflow_user_id`, the exact four operational
scopes, and expire after at most 300 seconds.

## Private preparation in a Codespace

```bash
bash mcps/trackflow/qa/codespaces/start.sh
bash mcps/trackflow/qa/codespaces/verify.sh
```

The start script asks for one ephemeral human QA password using hidden input.
All other credentials are generated with Python's `secrets` module, kept only
in process environments, and never printed. A successful run ends with
`PRIVATE_READY`; this is not external Playground acceptance.

The calculated URLs use the following origin:

```text
https://${CODESPACE_NAME}-18085.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}
```

## Bearer acquisition while the port is Private

Keep port `18085` **Private**. From a trusted local PowerShell, run the bearer
helper and pipe its stdout directly to the clipboard. Replace the placeholders
with the selected Codespace and repository directory; neither value is a
credential:

```powershell
gh codespace ssh -c <codespace-name> -- "cd /workspaces/<repository-directory> && uv run --project mcps/trackflow --python 3.12 python mcps/trackflow/qa/codespaces/device_token.py" | Set-Clipboard
```

The helper writes only the verification URI, user code and sanitized status
to stderr. Open the URI, authenticate with the printed QA username and the
ephemeral password entered at startup, and approve the device. The browser
must be signed in to GitHub so it can access the still-private forwarded URL.
The bearer is the sole stdout value. Interactive stdout is rejected, so the
token is never displayed. The helper validates RS256/JWKS, issuer, exact
audience, expiration, identity binding and exact scopes before releasing it
to the pipe.

Confirm in that authenticated browser that the calculated HTTPS origin opens
the selected realm device page and that its `/mcp` endpoint returns an
authentication challenge rather than an application or gateway error. Never
paste the bearer into the browser address bar.

## Human publication decision and MCP Playground

Only after private startup, bearer acquisition and URL checks succeed may the
user approve external access. The user then changes only port `18085` to
**Public** in the GitHub Codespaces **Ports** panel. This is an explicit human
action. The scripts never invoke a visibility command or create a tunnel.
As an alternative to the panel, the user may run these exact PowerShell
commands manually after recording the approval:

```powershell
$codespace = '<codespace-name>'
gh codespace ports visibility 18085:public -c $codespace
gh codespace ports -c $codespace --json sourcePort,visibility,browseUrl
```

Confirm that the returned entry for source port `18085` is `public` and that
its `browseUrl` is the calculated HTTPS origin. These commands are documented
for the user; they were not executed by the launcher or during repository QA.

Connect the Playground to the calculated origin plus `/mcp` and add:

```text
Authorization: Bearer <clipboard-value>
```

Do not paste the bearer into chat, screenshots, shell history or repository
files. It expires within five minutes. This runbook does not enable a
Playground callback, DCR, password grant, client-credentials grant or
anonymous MCP access.

## Acceptance evidence

1. Inspect the Playground's displayed discovery catalog without executing a
   tool. Confirm exactly these five names and review their descriptions and
   schemas: `create_incident`, `get_incident`, `update_incident_status`,
   `list_inventory_products`, `get_inventory_product`. This is read-only. Do
   not report a raw protocol call such as `tools/list` unless the Playground
   actually exposes that transcript.
2. Using only disposable QA values, execute each of the five tools once. Use
   the incident returned by `create_incident` for the subsequent read and
   status update; use a product returned by `list_inventory_products` for the
   product read. Do not invent or reuse production identifiers.
3. Record sanitized evidence only: timestamp, public MCP origin, tool name,
   success/error category, masked disposable identifier and request ID when
   the UI exposes one. Exclude bearer, device/user code, email, tracking
   number, description, raw payloads and browser storage.
4. Run the non-mutating automated configuration test inside the Codespace:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 uv run --project mcps/trackflow --python 3.12 \
     pytest mcps/trackflow/qa/codespaces/test_codespaces_config.py \
     -q -p no:cacheprovider
   ```

The automated test validates configuration and security contracts; it is not
evidence that the external Playground executed a tool.

## Cleanup

```bash
bash mcps/trackflow/qa/codespaces/stop.sh
```

First return port `18085` to **Private**, either manually in the Codespaces
Ports panel or with these PowerShell commands:

```powershell
$codespace = '<codespace-name>'
gh codespace ports visibility 18085:private -c $codespace
gh codespace ports -c $codespace --json sourcePort,visibility,browseUrl
```

Confirm source port `18085` reports `private` before invoking `stop.sh`.
These commands were not executed during repository QA. `stop.sh` never uses
broad Docker deletion and does not touch containers outside the fixed Compose
project.

## Current validation limit

Private QA verifies startup, advertised public metadata, route allowlisting,
loopback bindings, secret handling and cleanup. A real Codespace, manual
visibility decision, browser Device Grant approval, external Playground
connection and end-to-end tool calls remain human acceptance gates; local
tests must not be reported as that evidence.
