#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/compose.yml"
COMPOSE_PROJECT="trackflow-mcp-codespaces-qa"
STATE_DIR="/tmp/trackflow-mcp-codespaces-${UID}"

stop_owned_group() {
  local component="$1"
  local marker="$2"
  local pid_file="${STATE_DIR}/${component}.pid"
  [[ -f "${pid_file}" ]] || return 0
  local pid
  pid="$(<"${pid_file}")"
  [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || {
    printf 'WARNING: invalid %s PID file; refusing to signal it.\n' "${component}" >&2
    return 1
  }
  if ! kill -0 "${pid}" 2>/dev/null; then
    return 0
  fi
  local command_line
  command_line="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
  [[ "${command_line}" == *"${marker}"* ]] || {
    printf 'WARNING: PID %s is not the owned %s process; refusing to signal it.\n' \
      "${pid}" "${component}" >&2
    return 1
  }
  kill -TERM -- "-${pid}" 2>/dev/null || true
  for _ in {1..30}; do
    kill -0 "${pid}" 2>/dev/null || return 0
    sleep 1
  done
  kill -KILL -- "-${pid}" 2>/dev/null || true
}

cleanup_error=0
stop_owned_group mcp "trackflow_mcp.runtime:create_runtime_app" || cleanup_error=1
stop_owned_group api "services.api.main:app" || cleanup_error=1

# Compose parses interpolation even for down. These non-secret placeholders are
# used only to identify and remove the fixed project resources.
export POSTGRES_USER=unused
export POSTGRES_PASSWORD=unused
export POSTGRES_DB=unused
export QA_KEYCLOAK_ADMIN_USER=unused
export QA_KEYCLOAK_ADMIN_PASSWORD=unused
export QA_KEYCLOAK_REALM=trackflow-codespaces-qa
export TRACKFLOW_CODESPACES_PUBLIC_ORIGIN=https://unused.invalid
export TRACKFLOW_CODESPACES_PUBLIC_HOST=unused.invalid

docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" \
  down --volumes --remove-orphans

expected="/tmp/trackflow-mcp-codespaces-${UID}"
if (( cleanup_error == 0 )) && [[ "${STATE_DIR}" == "${expected}" && -d "${STATE_DIR}" ]]; then
  rm -rf -- "${STATE_DIR}"
fi

remaining="$(docker ps -aq --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}")"
[[ -z "${remaining}" ]] || {
  printf 'ERROR: dedicated QA containers remain after cleanup.\n' >&2
  exit 1
}
(( cleanup_error == 0 )) || {
  printf 'ERROR: process ownership could not be verified; state was preserved.\n' >&2
  exit 1
}
printf 'CODESPACES_QA_CLEANED\n'
