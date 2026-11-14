"""Issue one short-lived QA bearer with OAuth 2.0 Device Authorization Grant.

Human instructions and sanitized state go to stderr.  The bearer is emitted
only to a non-interactive stdout so it can be piped directly to a clipboard.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse

import httpx
import jwt


SCOPES = frozenset(
    {
        "incidents:create",
        "incidents:read",
        "incidents:status",
        "inventory:read",
    }
)
AUDIENCE = "trackflow-mcp"
GATEWAY = "http://127.0.0.1:18085"


class DeviceFlowError(RuntimeError):
    """A sanitized device-flow failure safe to show to a QA operator."""


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise DeviceFlowError(f"Missing non-secret runtime setting: {name}")
    return value


def load_runtime(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise DeviceFlowError("Invalid runtime metadata file.")
        name, encoded = line.split("=", 1)
        parsed = shlex.split(encoded, posix=True)
        if len(parsed) != 1 or not name.isidentifier():
            raise DeviceFlowError("Invalid runtime metadata file.")
        values[name] = parsed[0]
    return values


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise DeviceFlowError("Identity provider returned an invalid response.") from exc
    if not isinstance(value, dict):
        raise DeviceFlowError("Identity provider returned an invalid response.")
    return value


def begin_device_flow(
    client: httpx.Client,
    endpoint: str,
    client_id: str,
) -> dict[str, Any]:
    response = client.post(
        endpoint,
        data={"client_id": client_id, "scope": " ".join(sorted(SCOPES))},
    )
    if response.status_code != 200:
        raise DeviceFlowError("Device authorization could not be started.")
    payload = _json(response)
    device_code = payload.get("device_code")
    user_code = payload.get("user_code")
    verification_uri = payload.get("verification_uri")
    expires_in = payload.get("expires_in")
    interval = payload.get("interval", 5)
    if (
        not isinstance(device_code, str)
        or not 1 <= len(device_code) <= 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in device_code)
        or not isinstance(user_code, str)
        or not 1 <= len(user_code) <= 128
        or any(ord(character) < 32 or ord(character) == 127 for character in user_code)
        or not isinstance(verification_uri, str)
        or not verification_uri
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or not 1 <= expires_in <= 1800
        or isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 1 <= interval <= 30
    ):
        raise DeviceFlowError("Identity provider returned an incomplete device response.")
    return payload


def poll_device_token(
    client: httpx.Client,
    endpoint: str,
    client_id: str,
    device_code: str,
    expires_in: int,
    interval: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    status: Callable[[str], None] | None = None,
) -> str:
    deadline = monotonic() + expires_in
    delay = max(interval, 1)
    while monotonic() < deadline:
        response = client.post(
            endpoint,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            },
        )
        payload = _json(response)
        if response.status_code == 200:
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise DeviceFlowError("Identity provider omitted the access token.")
            return token
        error = payload.get("error")
        if error == "authorization_pending":
            if status:
                status("authorization_pending")
        elif error == "slow_down":
            delay += 5
            if status:
                status("slow_down")
        elif error == "access_denied":
            raise DeviceFlowError("Device authorization was denied.")
        elif error == "expired_token":
            raise DeviceFlowError("Device authorization expired.")
        else:
            raise DeviceFlowError("Device authorization failed.")
        sleep(delay)
    raise DeviceFlowError("Device authorization expired.")


def validate_token(
    token: str,
    jwks: Mapping[str, Any],
    issuer: str,
    expected_user_id: str,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
        key_id = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(key_id, str):
            raise DeviceFlowError("Bearer header is not an approved RS256 key.")
        key_set = jwt.PyJWKSet.from_dict(dict(jwks))
        key = next((item.key for item in key_set.keys if item.key_id == key_id), None)
        if key is None:
            raise DeviceFlowError("Bearer signing key is not trusted.")
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub", "trackflow_user_id", "scope"]},
        )
    except DeviceFlowError:
        raise
    except jwt.PyJWTError as exc:
        raise DeviceFlowError("Bearer validation failed.") from exc

    current = int(time.time()) if now is None else now
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    if (
        not isinstance(issued_at, int)
        or not isinstance(expires_at, int)
        or expires_at <= current
        or expires_at - issued_at > 300
        or expires_at - current > 300
    ):
        raise DeviceFlowError("Bearer lifetime is not the approved short-lived profile.")
    if claims.get("trackflow_user_id") != expected_user_id:
        raise DeviceFlowError("Bearer is not linked to the disposable TrackFlow user.")
    if claims.get("aud") != AUDIENCE:
        raise DeviceFlowError("Bearer audience is not the exact MCP resource.")
    token_scopes = claims.get("scope")
    if not isinstance(token_scopes, str) or frozenset(token_scopes.split()) != SCOPES:
        raise DeviceFlowError("Bearer scopes do not match the approved QA operations.")
    return claims


def run(
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> int:
    if stdout.isatty():
        raise DeviceFlowError(
            "Refusing to print a bearer to an interactive terminal; pipe stdout directly to a clipboard."
        )
    uid = os.getuid() if hasattr(os, "getuid") else 0
    state_dir = Path(f"/tmp/trackflow-mcp-codespaces-{uid}")
    runtime = load_runtime(state_dir / "runtime.env")
    realm = _required(runtime, "QA_KEYCLOAK_REALM")
    origin = _required(runtime, "TRACKFLOW_CODESPACES_PUBLIC_ORIGIN").rstrip("/")
    client_id = _required(runtime, "QA_KEYCLOAK_PUBLIC_CLIENT_ID")
    expected_user_id = _required(runtime, "QA_TRACKFLOW_USER_ID")
    issuer = f"{origin}/realms/{realm}"
    local_protocol = f"{GATEWAY}/realms/{realm}/protocol/openid-connect"

    with client_factory(timeout=15) as client:
        device = begin_device_flow(client, f"{local_protocol}/auth/device", client_id)
        verification_uri = str(device["verification_uri"])
        parsed = urlparse(verification_uri)
        if (
            f"{parsed.scheme}://{parsed.netloc}" != origin
            or parsed.path != f"/realms/{realm}/device"
            or parsed.query
            or parsed.fragment
        ):
            raise DeviceFlowError("Identity provider returned an unexpected verification URI.")
        print(f"Verification URI: {verification_uri}", file=stderr)
        print(f"User code: {device['user_code']}", file=stderr)
        print("Status: waiting for browser authorization", file=stderr)
        token = poll_device_token(
            client,
            f"{local_protocol}/token",
            client_id,
            str(device["device_code"]),
            device["expires_in"],
            device["interval"] if "interval" in device else 5,
            status=lambda value: print(f"Status: {value}", file=stderr),
        )
        jwks_response = client.get(f"{local_protocol}/certs")
        if jwks_response.status_code != 200:
            raise DeviceFlowError("Signing keys are unavailable.")
        validate_token(token, _json(jwks_response), issuer, expected_user_id)
    stdout.write(token)
    stdout.flush()
    print("Status: bearer validated and copied through stdout pipe", file=stderr)
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (DeviceFlowError, OSError, httpx.HTTPError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
