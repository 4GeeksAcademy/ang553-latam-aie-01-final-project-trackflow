# TrackFlow MCP - Codespaces QA

This directory prepares the disposable stack required for the academic
GitHub Codespaces validation. It never changes port visibility. The only
candidate for later forwarding is the loopback gateway on port `18085`.

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

The gateway returns `404` for Keycloak administration, the `master` realm and
all unrelated paths. It forwards only the selected QA realm and Keycloak's
versioned login assets.

## Required Playground decision

The launcher does not guess the academic Playground callback. Set both values
from the designated client's own documentation or UI:

```bash
export TRACKFLOW_PLAYGROUND_REDIRECT_URI='https://playground.example/oauth/callback'
export TRACKFLOW_PLAYGROUND_ORIGIN='https://playground.example'
```

Both must use HTTPS, contain no wildcard, credentials or fragment, and the
origin must match the callback origin exactly. Dynamic Client Registration is
disabled. The public client is Authorization Code + PKCE S256 only.

## Private preparation in a Codespace

```bash
cd "/workspaces/$(basename "$GITHUB_REPOSITORY")"
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

Do not make the port public until Usuario + ChatGPT approve the exact
Playground and callback. When authorized, the user - not this launcher - runs:

```bash
gh codespace ports visibility 18085:public --codespace "$CODESPACE_NAME"
```

## Cleanup

```bash
bash mcps/trackflow/qa/codespaces/stop.sh
gh codespace ports visibility 18085:private --codespace "$CODESPACE_NAME"
```

Run the visibility command first during an external session. `stop.sh` never
uses broad Docker deletion and does not touch containers outside the fixed
Compose project.

## Current validation limit

While the forwarded port remains private, the public Codespaces hostname is
protected by GitHub and cannot be used by an unauthenticated external OAuth
client. Private QA therefore verifies startup, advertised public metadata,
route allowlisting, loopback bindings, secret handling and cleanup. The full
Authorization Code flow through the public hostname remains an external gate.
