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

expect_status() {
  local expected="$1"
  local path="$2"
  local actual
  actual="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --max-time 5 "${gateway}${path}")"
  [[ "${actual}" == "${expected}" ]] || {
    printf 'ERROR: %s returned %s, expected %s.\n' "${path}" "${actual}" "${expected}" >&2
    exit 1
  }
}

expect_status 404 "/_gateway/health"
expect_status 404 "/admin/"
expect_status 404 "/realms/master/"
expect_status 404 "/realms/not-the-selected-realm/.well-known/openid-configuration"
expect_status 404 "/api/incidents"
expect_status 404 "/unknown"
expect_status 404 "/realms/${QA_KEYCLOAK_REALM}/protocol/openid-connect/auth"
expect_status 404 "/realms/${QA_KEYCLOAK_REALM}/protocol/openid-connect/logout"
expect_status 404 "/realms/${QA_KEYCLOAK_REALM}/account"

oidc_json="$(curl --silent --show-error --fail --max-time 10 \
  "${gateway}/realms/${QA_KEYCLOAK_REALM}/.well-known/openid-configuration")"
prm_json="$(curl --silent --show-error --fail --max-time 10 \
  "${gateway}/.well-known/oauth-protected-resource/mcp")"

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
    raise SystemExit("OIDC issuer does not use the public Codespaces origin.")
expected_device = f"{expected_issuer}/protocol/openid-connect/auth/device"
if oidc.get("device_authorization_endpoint") != expected_device:
    raise SystemExit("OIDC discovery does not advertise the Device Authorization endpoint.")
if prm.get("resource") != f"{origin}/mcp":
    raise SystemExit("Protected Resource Metadata advertises the wrong resource.")
if expected_issuer not in str(prm.get("authorization_servers")):
    raise SystemExit("Protected Resource Metadata does not advertise the QA issuer.")
expected_scopes = {
    "incidents:create",
    "incidents:read",
    "incidents:status",
    "inventory:read",
}
if set(prm.get("scopes_supported", ())) != expected_scopes:
    raise SystemExit("Protected Resource Metadata scopes changed.")
PY

headers="$(mktemp)"
trap 'rm -f -- "${headers}"' EXIT
status="$(curl --silent --output /dev/null --dump-header "${headers}" \
  --write-out '%{http_code}' --max-time 5 "${gateway}/mcp")"
[[ "${status}" == "401" ]] || {
  printf 'ERROR: unauthenticated MCP request returned %s, expected 401.\n' "${status}" >&2
  exit 1
}
grep -qi '^www-authenticate:.*resource_metadata=' "${headers}" || {
  printf 'ERROR: MCP 401 omitted resource_metadata challenge.\n' >&2
  exit 1
}

dcr_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --max-time 5 -X POST -H 'content-type: application/json' -d '{}' \
  "${gateway}/realms/${QA_KEYCLOAK_REALM}/clients-registrations/default")"
[[ "${dcr_status}" == "404" ]] || {
  printf 'ERROR: DCR route is reachable through the gateway (%s).\n' "${dcr_status}" >&2
  exit 1
}

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
    raise SystemExit("Expected exactly three dedicated QA containers.")
containers = json.loads(
    subprocess.check_output(["docker", "inspect", *ids], text=True)
)
allowed = {("5432/tcp", "55440"), ("8080/tcp", "18082"), ("8080/tcp", "18085")}
seen = set()
for container in containers:
    for container_port, bindings in (container["NetworkSettings"]["Ports"] or {}).items():
        for binding in bindings or ():
            if binding.get("HostIp") != "127.0.0.1":
                raise SystemExit("A QA container port is not bound to loopback.")
            seen.add((container_port, binding.get("HostPort")))
if seen != allowed:
    raise SystemExit(f"Unexpected QA port bindings: {sorted(seen)!r}")
PY

printf 'PRIVATE_GATEWAY_VERIFIED\n'
