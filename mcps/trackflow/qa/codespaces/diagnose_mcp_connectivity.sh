#!/usr/bin/env bash
# Diagnostic script to test MCP connectivity after launcher starts.
# This can be run separately to debug connectivity issues.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="/tmp/trackflow-mcp-codespaces-${UID}"

[[ -r "${STATE_DIR}/runtime.env" ]] || {
  printf 'ERROR: QA runtime state not found. Run start.sh first.\n' >&2
  exit 1
}

source "${STATE_DIR}/runtime.env"

echo "MCP Connectivity Diagnostic"
echo "============================"
echo

# Test 1: MCP health check (from host)
echo "Test 1: MCP health check (from host on loopback)"
if curl --silent --show-error --fail --max-time 5 \
  "http://127.0.0.1:18084/.well-known/oauth-protected-resource/mcp" >/dev/null; then
  echo "  ✓ MCP responds on 127.0.0.1:18084"
else
  echo "  ✗ MCP not responding on 127.0.0.1:18084"
  exit 1
fi
echo

# Test 2: Gateway health check
echo "Test 2: Gateway health check (Nginx)"
if curl --silent --show-error --fail --max-time 5 \
  "http://127.0.0.1:18085/" >/dev/null 2>&1; then
  echo "  ✓ Nginx responds on 127.0.0.1:18085"
else
  echo "  ✗ Nginx not responding on 127.0.0.1:18085"
  exit 1
fi
echo

# Test 3: Protected Resource Metadata through Nginx
echo "Test 3: Protected Resource Metadata through Nginx"
response="$(curl --silent --show-error --write-out "\n%{http_code}" \
  "http://127.0.0.1:18085/.well-known/oauth-protected-resource/mcp")" || true

http_code="$(echo "${response}" | tail -1)"
body="$(echo "${response}" | head -1)"

if [[ "${http_code}" == "200" ]]; then
  echo "  ✓ HTTP 200 from Nginx MCP proxy"
  if echo "${body}" | grep -q '"resource"'; then
    echo "  ✓ Valid JSON response with resource field"
  else
    echo "  ⚠ Response is JSON but missing resource field"
  fi
else
  echo "  ✗ HTTP ${http_code} (expected 200)"
  echo "  Response: ${body:0:100}"
  exit 1
fi
echo

# Test 4: OIDC discovery
echo "Test 4: OIDC discovery through Nginx"
if curl --silent --show-error --fail --max-time 5 \
  "http://127.0.0.1:18085/realms/${QA_KEYCLOAK_REALM}/.well-known/openid-configuration" >/dev/null; then
  echo "  ✓ OIDC discovery responds"
else
  echo "  ✗ OIDC discovery failed"
  exit 1
fi
echo

# Test 5: Security check - blocked endpoints
echo "Test 5: Security check - blocked endpoints"
for endpoint in "/_gateway/health" "/admin/" "/realms/master/"; do
  http_code="$(curl --silent --write-out "%{http_code}" --output /dev/null \
    "http://127.0.0.1:18085${endpoint}")"
  if [[ "${http_code}" == "404" ]]; then
    echo "  ✓ ${endpoint} blocked (404)"
  else
    echo "  ✗ ${endpoint} returned ${http_code} (expected 404)"
    exit 1
  fi
done
echo

echo "✅ All connectivity tests passed!"
echo
echo "MCP is accessible through the Nginx gateway at:"
echo "  http://127.0.0.1:18085/.well-known/oauth-protected-resource/mcp"
