#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/compose.yml"
COMPOSE_PROJECT="trackflow-mcp-codespaces-qa"
STATE_DIR="/tmp/trackflow-mcp-codespaces-${UID}"
RUNTIME_ENV="${STATE_DIR}/runtime.env"

[[ -r "${RUNTIME_ENV}" ]] || {
  printf 'ERROR: private QA runtime state is missing.\n' >&2
  exit 1
}
# The file contains public, non-secret routing values only.
# shellcheck disable=SC1090
source "${RUNTIME_ENV}"

gateway="http://127.0.0.1:18085"

verify_check() {
  local check_name="$1"
  printf 'Verifying: %s...\n' "${check_name}" >&2
}

verify_success() {
  local check_name="$1"
  printf '  ✓ %s\n' "${check_name}" >&2
}

expect_status() {
  local expected="$1"
  local path="$2"
  local check_label="${3:-${path}}"
  verify_check "HTTP ${expected} on ${check_label}"
  local actual
  actual="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --max-time 5 "${gateway}${path}")" || {
    printf 'ERROR: curl failed for %s (connection or timeout issue)\n' "${path}" >&2
    exit 1
  }
  [[ "${actual}" == "${expected}" ]] || {
    printf 'ERROR: %s returned HTTP %s, expected %s.\n' "${path}" "${actual}" "${expected}" >&2
    exit 1
  }
  verify_success "HTTP ${expected} on ${check_label}"
}

expect_status 404 "/_gateway/health" "_gateway/health (should not exist)"
expect_status 404 "/admin/" "/admin/ (should not exist)"
expect_status 404 "/realms/master/" "/realms/master/ (should not exist)"
expect_status 404 "/realms/not-the-selected-realm/.well-known/openid-configuration" "wrong realm OIDC discovery"
expect_status 404 "/api/incidents" "/api/incidents (should not exist)"
expect_status 404 "/unknown" "/unknown (should not exist)"
expect_status 404 "/realms/${QA_KEYCLOAK_REALM}/protocol/openid-connect/auth" "OAuth auth endpoint (not proxied)"
expect_status 404 "/realms/${QA_KEYCLOAK_REALM}/protocol/openid-connect/logout" "OAuth logout endpoint (not proxied)"
expect_status 404 "/realms/${QA_KEYCLOAK_REALM}/account" "Keycloak account page (not proxied)"

verify_check "OIDC discovery endpoint"
oidc_json="$(curl --silent --show-error --fail --max-time 10 \
  "${gateway}/realms/${QA_KEYCLOAK_REALM}/.well-known/openid-configuration")" || {
  printf 'ERROR: OIDC discovery endpoint returned invalid response.\n' >&2
  exit 1
}
verify_success "OIDC discovery endpoint"

verify_check "Protected Resource Metadata endpoint"
prm_json="$(curl --silent --show-error --fail --max-time 10 \
  "${gateway}/.well-known/oauth-protected-resource/mcp")" || {
  printf 'ERROR: Protected Resource Metadata endpoint returned invalid response.\n' >&2
  exit 1
}
verify_success "Protected Resource Metadata endpoint"

verify_check "OIDC and PRM content validation"
python3 - "${TRACKFLOW_CODESPACES_PUBLIC_ORIGIN}" "${QA_KEYCLOAK_REALM}" \
  "${oidc_json}" "${prm_json}" <<'PY'
import json
import sys

origin = sys.argv[1]
realm = sys.argv[2]
oidc = json.loads(sys.argv[3])
prm = json.loads(sys.argv[4])
expected_issuer = f"{origin}/realms/{realm}"
if oidc.get("issuer") != expected_issuer:
    raise SystemExit("OIDC issuer mismatch: expected {}, got {}".format(expected_issuer, oidc.get("issuer")))
expected_device = f"{expected_issuer}/protocol/openid-connect/auth/device"
if oidc.get("device_authorization_endpoint") != expected_device:
    raise SystemExit("Device Authorization endpoint mismatch: expected {}, got {}".format(expected_device, oidc.get("device_authorization_endpoint")))
if prm.get("resource") != f"{origin}/mcp":
    raise SystemExit("Protected Resource Metadata resource mismatch: expected {}/mcp, got {}".format(origin, prm.get("resource")))
if expected_issuer not in str(prm.get("authorization_servers")):
    raise SystemExit("Protected Resource Metadata missing QA issuer in authorization_servers")
expected_scopes = {
    "incidents:create",
    "incidents:read",
    "incidents:status",
    "inventory:read",
}
actual_scopes = set(prm.get("scopes_supported", ()))
if actual_scopes != expected_scopes:
    raise SystemExit("Protected Resource Metadata scopes mismatch: expected {}, got {}".format(sorted(expected_scopes), sorted(actual_scopes)))
PY
verify_success "OIDC and PRM content validation"

verify_check "MCP endpoint authentication (should reject unauthenticated)"
headers="$(mktemp)"
trap 'rm -f -- "${headers}"' EXIT
status="$(curl --silent --output /dev/null --dump-header "${headers}" \
  --write-out '%{http_code}' --max-time 5 "${gateway}/mcp")"
[[ "${status}" == "401" ]] || {
  printf 'ERROR: unauthenticated MCP request returned HTTP %s, expected 401.\n' "${status}" >&2
  exit 1
}
verify_success "MCP endpoint authentication"

verify_check "MCP endpoint WWW-Authenticate header"
grep -qi '^www-authenticate:.*resource_metadata=' "${headers}" || {
  printf 'ERROR: MCP 401 response missing resource_metadata challenge in WWW-Authenticate header.\n' >&2
  exit 1
}
verify_success "MCP endpoint WWW-Authenticate header"

verify_check "DCR endpoint is blocked (should return 404)"
dcr_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --max-time 5 -X POST -H 'content-type: application/json' -d '{}' \
  "${gateway}/realms/${QA_KEYCLOAK_REALM}/clients-registrations/default")"
[[ "${dcr_status}" == "404" ]] || {
  printf 'ERROR: DCR route is reachable through gateway (HTTP %s), should be blocked (404).\n' "${dcr_status}" >&2
  exit 1
}
verify_success "DCR endpoint is blocked"

verify_check "Container and port configuration"
python3 - "${COMPOSE_PROJECT}" <<'PY'
import json
import subprocess
import sys

project = sys.argv[1]
ids = subprocess.check_output(
    ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"],
    text=True,
).split()
if len(ids) != 3:
    raise SystemExit("Expected exactly 3 QA containers (postgres, keycloak, gateway), got {}: {}".format(len(ids), ", ".join(ids[:3] if ids else [])))
containers = json.loads(
    subprocess.check_output(["docker", "inspect", *ids], text=True)
)
allowed = {("5432/tcp", "55440"), ("8080/tcp", "18082"), ("8080/tcp", "18085")}
seen = set()
for container in containers:
    for container_port, bindings in (container["NetworkSettings"]["Ports"] or {}).items():
        for binding in bindings or ():
            if binding.get("HostIp") != "127.0.0.1":
                raise SystemExit("Container port {} is not bound to loopback 127.0.0.1, but to {}".format(container_port, binding.get("HostIp")))
            seen.add((container_port, binding.get("HostPort")))
if seen != allowed:
    missing = allowed - seen
    extra = seen - allowed
    msg = "Port binding mismatch."
    if missing:
        msg += " Missing: {}".format(sorted(missing))
    if extra:
        msg += " Extra: {}".format(sorted(extra))
    raise SystemExit(msg)
PY
verify_success "Container and port configuration"

printf 'PRIVATE_GATEWAY_VERIFIED\n'
