#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/../../../.." && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/compose.yml"
COMPOSE_PROJECT="trackflow-mcp-codespaces-qa"
STATE_DIR="/tmp/trackflow-mcp-codespaces-${UID}"

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

for command in docker uv curl python3; do
  command -v "${command}" >/dev/null 2>&1 || fail "Missing command: ${command}"
done

[[ "${CODESPACES:-}" == "true" ]] || fail "This launcher only runs inside GitHub Codespaces."
[[ ! -e "${STATE_DIR}" ]] || fail "QA state already exists; run stop.sh first."

existing="$(docker ps -aq --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}")"
[[ -z "${existing}" ]] || fail "The dedicated Codespaces QA Compose project already exists."

python3 - <<'PY'
import socket

for port in (55440, 18082, 18083, 18084, 18085):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise SystemExit(f"Required loopback port {port} is unavailable: {error}")
PY

mkdir -m 700 "${STATE_DIR}"

cleanup_on_error() {
  status=$?
  if (( status != 0 )); then
    bash "${SCRIPT_DIR}/stop.sh" >/dev/null 2>&1 || true
  fi
  unset HUMAN_PASSWORD POSTGRES_PASSWORD QA_KEYCLOAK_ADMIN_PASSWORD
  unset QA_KEYCLOAK_MCP_CLIENT_SECRET JWT_SECRET_KEY
  exit "${status}"
}
trap cleanup_on_error EXIT

export QA_KEYCLOAK_REALM="trackflow-codespaces-qa"
export TRACKFLOW_CODESPACES_PUBLIC_ORIGIN="$(
  python3 "${SCRIPT_DIR}/validate_config.py"
)"
export TRACKFLOW_CODESPACES_PUBLIC_HOST="${TRACKFLOW_CODESPACES_PUBLIC_ORIGIN#https://}"

read -r -s -p "Enter one ephemeral QA password for human login (minimum 12 characters): " HUMAN_PASSWORD
printf '\n'
[[ ${#HUMAN_PASSWORD} -ge 12 ]] || fail "The ephemeral QA password must contain at least 12 characters."

new_secret() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
}

export TRACKFLOW_MCP_QA=1
export QA_KEYCLOAK_BASE_URL="http://127.0.0.1:18082"
export QA_KEYCLOAK_ADMIN_USER="tf-admin-$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
export QA_KEYCLOAK_ADMIN_PASSWORD="$(new_secret)"
export QA_KEYCLOAK_PUBLIC_CLIENT_ID="mcp-playground-qa"
export QA_KEYCLOAK_MCP_CLIENT_ID="trackflow-mcp"
export QA_KEYCLOAK_MCP_CLIENT_SECRET="$(new_secret)"
export QA_KEYCLOAK_API_CLIENT_ID="trackflow-api"
export QA_KEYCLOAK_USERNAME="trackflow-qa-user"
export QA_KEYCLOAK_PASSWORD="${HUMAN_PASSWORD}"
export QA_TRACKFLOW_USER_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
export QA_TRACKFLOW_LOCAL_PASSWORD="${HUMAN_PASSWORD}"

export POSTGRES_USER="trackflowqa"
export POSTGRES_PASSWORD="$(new_secret)"
export POSTGRES_DB="trackflowqa"
export DATABASE_URL="postgresql+psycopg2://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:55440/${POSTGRES_DB}"
export TRACKFLOW_AUTH_DATABASE_PATH="${STATE_DIR}/trackflow-mcp-auth.json"
export JWT_SECRET_KEY="$(new_secret)"

ISSUER="${TRACKFLOW_CODESPACES_PUBLIC_ORIGIN}/realms/${QA_KEYCLOAK_REALM}"
PROTOCOL="${ISSUER}/protocol/openid-connect"
INTERNAL_PROTOCOL="http://127.0.0.1:18082/realms/${QA_KEYCLOAK_REALM}/protocol/openid-connect"

export TRACKFLOW_OIDC_ISSUER="${ISSUER}"
export TRACKFLOW_OIDC_JWKS_URI="${INTERNAL_PROTOCOL}/certs"
export TRACKFLOW_MCP_RESOURCE_URL="${TRACKFLOW_CODESPACES_PUBLIC_ORIGIN}/mcp"
export TRACKFLOW_MCP_ISSUER="${ISSUER}"
export TRACKFLOW_MCP_AUDIENCE="trackflow-mcp"
export TRACKFLOW_MCP_AUTHORIZATION_ENDPOINT="${PROTOCOL}/auth"
export TRACKFLOW_MCP_TOKEN_ENDPOINT="${PROTOCOL}/token"
export TRACKFLOW_MCP_JWKS_URI="${PROTOCOL}/certs"
export TRACKFLOW_MCP_KEYCLOAK_CLIENT_ID="trackflow-mcp"
export TRACKFLOW_MCP_KEYCLOAK_CLIENT_SECRET="${QA_KEYCLOAK_MCP_CLIENT_SECRET}"
export TRACKFLOW_API_BASE_URL="http://127.0.0.1:18083"

cd "${REPOSITORY_ROOT}"
docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" up -d --wait postgres keycloak

uv run --project mcps/trackflow --python 3.12 \
  python mcps/trackflow/qa/configure_keycloak.py
uv run --project mcps/trackflow --python 3.12 \
  python mcps/trackflow/qa/codespaces/harden_keycloak.py
uv run --python 3.12 python mcps/trackflow/qa/codespaces/provision_qa_ddl.py
uv run --python 3.12 python mcps/trackflow/qa/seed_trackflow.py

setsid uv run --python 3.12 uvicorn services.api.main:app \
  --host 127.0.0.1 --port 18083 >"${STATE_DIR}/api.log" 2>&1 &
printf '%s\n' "$!" >"${STATE_DIR}/api.pid"

setsid uv run --project mcps/trackflow --python 3.12 \
  uvicorn trackflow_mcp.runtime:create_runtime_app --factory \
  --host 172.17.0.1 --port 18084 >"${STATE_DIR}/mcp.log" 2>&1 &
printf '%s\n' "$!" >"${STATE_DIR}/mcp.pid"

wait_http() {
  local url="$1"
  local attempts="${2:-60}"
  for ((index=0; index<attempts; index++)); do
    if curl --silent --show-error --fail --max-time 2 "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail "Service did not become ready: ${url}"
}

wait_http "http://127.0.0.1:18083/health"
wait_http "http://172.17.0.1:18084/.well-known/oauth-protected-resource/mcp"

docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" up -d --wait gateway

cat >"${STATE_DIR}/runtime.env" <<EOF
QA_KEYCLOAK_REALM='${QA_KEYCLOAK_REALM}'
TRACKFLOW_CODESPACES_PUBLIC_ORIGIN='${TRACKFLOW_CODESPACES_PUBLIC_ORIGIN}'
TRACKFLOW_CODESPACES_PUBLIC_HOST='${TRACKFLOW_CODESPACES_PUBLIC_HOST}'
QA_KEYCLOAK_PUBLIC_CLIENT_ID='${QA_KEYCLOAK_PUBLIC_CLIENT_ID}'
QA_TRACKFLOW_USER_ID='${QA_TRACKFLOW_USER_ID}'
EOF

bash "${SCRIPT_DIR}/verify.sh"

trap - EXIT
unset HUMAN_PASSWORD POSTGRES_PASSWORD QA_KEYCLOAK_ADMIN_PASSWORD
unset QA_KEYCLOAK_MCP_CLIENT_SECRET JWT_SECRET_KEY

printf 'PRIVATE_READY\n'
printf 'MCP URL: %s/mcp\n' "${TRACKFLOW_CODESPACES_PUBLIC_ORIGIN}"
printf 'QA username: %s\n' "${QA_KEYCLOAK_USERNAME}"
printf 'Bearer helper: %s/device_token.py (stdout must be piped)\n' "${SCRIPT_DIR}"
printf 'Port 18085 remains private; no visibility command was executed.\n'
