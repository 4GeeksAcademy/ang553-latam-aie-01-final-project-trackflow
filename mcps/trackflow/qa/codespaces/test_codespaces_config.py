from __future__ import annotations

import importlib.util
import io
import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "trackflow_codespaces_validate",
    HERE / "validate_config.py",
)
assert SPEC and SPEC.loader
validate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validate)

DEVICE_SPEC = importlib.util.spec_from_file_location(
    "trackflow_codespaces_device", HERE / "device_token.py"
)
assert DEVICE_SPEC and DEVICE_SPEC.loader
device = importlib.util.module_from_spec(DEVICE_SPEC)
DEVICE_SPEC.loader.exec_module(device)


def codespaces_env(**overrides: str) -> dict[str, str]:
    values = {
        "CODESPACES": "true",
        "CODESPACE_NAME": "trackflow-qa-abc123",
        "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "app.github.dev",
    }
    values.update(overrides)
    return values


def test_public_origin_is_derived_and_not_caller_selected() -> None:
    assert validate.public_origin(codespaces_env()) == (
        "https://trackflow-qa-abc123-18085.app.github.dev"
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CODESPACES", "false"),
        ("CODESPACE_NAME", "bad/name"),
        ("CODESPACE_NAME", "a" * 60),
        ("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "bad_domain.example"),
    ],
)
def test_public_origin_rejects_non_codespaces_or_unsafe_values(
    key: str, value: str
) -> None:
    with pytest.raises(ValueError):
        validate.public_origin(codespaces_env(**{key: value}))


def test_gateway_has_allowlist_and_explicit_administration_denies() -> None:
    config = (HERE / "gateway.nginx.conf.template").read_text(encoding="utf-8")
    assert "location = /mcp" in config
    assert "location = /.well-known/oauth-protected-resource/mcp" in config
    assert (
        "location = /realms/${QA_KEYCLOAK_REALM}/.well-known/openid-configuration"
        in config
    )
    assert "location = /realms/${QA_KEYCLOAK_REALM}/protocol/openid-connect/auth/device" in config
    assert "location = /realms/${QA_KEYCLOAK_REALM}/protocol/openid-connect/token" in config
    assert "location = /realms/${QA_KEYCLOAK_REALM}/protocol/openid-connect/certs" in config
    assert "location = /realms/${QA_KEYCLOAK_REALM}/device" in config
    assert "location = /realms/${QA_KEYCLOAK_REALM}/login-actions/authenticate" in config
    assert "/protocol/openid-connect/(auth|token|certs)" not in config
    assert "location = /realms/${QA_KEYCLOAK_REALM}/protocol/openid-connect/auth {" not in config
    assert "/clients-registrations/" not in config
    assert "/_gateway/health" not in config
    assert "location ~ ^/(admin|realms/master)(/|$)" in config
    assert "location / {" in config and "return 404;" in config
    assert "proxy_pass http://host.docker.internal:18083" not in config
    assert "55440" not in config


def test_compose_publishes_only_loopback_bindings() -> None:
    compose = (HERE / "compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:55440:5432"' in compose
    assert '"127.0.0.1:18082:8080"' in compose
    assert '"127.0.0.1:18085:8080"' in compose
    assert "9000:9000" not in compose
    assert "0.0.0.0:" not in compose


def test_launchers_never_publish_codespaces_ports_or_embed_secrets() -> None:
    scripts = "\n".join(
        (HERE / name).read_text(encoding="utf-8")
        for name in ("start.sh", "verify.sh", "stop.sh")
    )
    assert "gh codespace ports visibility" not in scripts
    assert ":public" not in scripts
    assert "Get-Random" not in scripts
    assert "secrets.token_urlsafe" in scripts
    assert "QA_KEYCLOAK_MCP_CLIENT_SECRET=" not in (HERE / "README.md").read_text(
        encoding="utf-8"
    )
    assert "TRACKFLOW_PLAYGROUND_REDIRECT_URI" not in scripts
    assert "TRACKFLOW_PLAYGROUND_ORIGIN" not in scripts


def test_readme_documents_manual_visibility_and_verification_commands() -> None:
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    assert "gh codespace ports visibility 18085:public -c $codespace" in readme
    assert "gh codespace ports visibility 18085:private -c $codespace" in readme
    assert (
        "gh codespace ports -c $codespace --json sourcePort,visibility,browseUrl"
        in readme
    )
    scripts = "\n".join(
        (HERE / name).read_text(encoding="utf-8")
        for name in ("start.sh", "verify.sh", "stop.sh")
    )
    assert "gh codespace ports visibility" not in scripts


def test_keycloak_hardening_is_device_only_without_callbacks() -> None:
    source = (HERE / "harden_keycloak.py").read_text(encoding="utf-8")
    assert '"oauth2.device.authorization.grant.enabled"' in source
    assert '"standardFlowEnabled": False' in source
    assert '"implicitFlowEnabled": False' in source
    assert '"directAccessGrantsEnabled": False' in source
    assert '"serviceAccountsEnabled": False' in source
    assert '"authorizationServicesEnabled": False' in source
    assert '"fullScopeAllowed": False' in source
    assert '"publicClient": False' in source
    assert '"clientAuthenticatorType": "client-secret"' in source
    assert 'mcp_attributes["standard.token.exchange.enabled"] = "true"' in source
    assert 'mcp_attributes[DEVICE_GRANT_ATTRIBUTE] = "false"' in source
    assert '"redirectUris": []' in source
    assert '"webOrigins": []' in source
    assert "registrationAllowed" in source
    assert '"grant_type": "client_credentials"' in source
    assert '"grant_type": "password"' in source
    assert "trackflow-mcp-audience" in source
    assert "trackflow_user_id" in source
    assert "oauth/callback" not in source
    assert '"*"' not in source


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_device_helper_refuses_interactive_stdout_before_network() -> None:
    calls: list[object] = []

    def client_factory(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("network must not be reached")

    output = _TTY()
    with pytest.raises(device.DeviceFlowError, match="interactive terminal") as raised:
        device.run(stdout=output, stderr=io.StringIO(), client_factory=client_factory)
    assert calls == []
    assert output.getvalue() == ""
    assert "Bearer " not in repr(raised.value)


class _Response:
    def __init__(self, status_code: int, payload: dict[str, object]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


class _PollingClient:
    def __init__(self, responses: list[_Response]):
        self.responses = iter(responses)
        self.requests: list[dict[str, str]] = []

    def post(self, endpoint: str, *, data: dict[str, str]) -> _Response:
        self.requests.append(data)
        return next(self.responses)


@pytest.mark.parametrize(
    "override",
    [
        {"device_code": 123},
        {"device_code": ""},
        {"user_code": "CODE\nINJECT"},
        {"user_code": ""},
        {"verification_uri": 123},
        {"expires_in": "600"},
        {"expires_in": True},
        {"expires_in": 0},
        {"expires_in": 1801},
        {"interval": "5"},
        {"interval": 0},
        {"interval": 31},
    ],
)
def test_device_start_rejects_malformed_or_terminal_unsafe_fields(
    override: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "device_code": "device-code",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://codespace.example/realms/qa/device",
        "expires_in": 600,
        "interval": 5,
    }
    payload.update(override)
    client = _PollingClient([_Response(200, payload)])
    with pytest.raises(device.DeviceFlowError, match="incomplete device response"):
        device.begin_device_flow(client, "http://gateway/auth/device", "public-client")


def test_device_poll_handles_pending_and_slow_down_without_leaking_payload() -> None:
    client = _PollingClient(
        [
            _Response(400, {"error": "authorization_pending", "secret": "do-not-log"}),
            _Response(400, {"error": "slow_down", "secret": "do-not-log"}),
            _Response(200, {"access_token": "opaque-bearer"}),
        ]
    )
    sleeps: list[float] = []
    states: list[str] = []
    ticks = iter([0.0, 0.1, 0.2, 0.3])
    token = device.poll_device_token(
        client,
        "http://gateway/token",
        "public-client",
        "device-code",
        30,
        1,
        sleep=sleeps.append,
        monotonic=lambda: next(ticks),
        status=states.append,
    )
    assert token == "opaque-bearer"
    assert sleeps == [1, 6]
    assert states == ["authorization_pending", "slow_down"]
    assert "do-not-log" not in repr(states)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ("access_denied", "denied"),
        ("expired_token", "expired"),
        ("invalid_grant", "failed"),
    ],
)
def test_device_poll_errors_are_sanitized(error: str, message: str) -> None:
    client = _PollingClient(
        [_Response(400, {"error": error, "error_description": "sensitive detail"})]
    )
    with pytest.raises(device.DeviceFlowError, match=message) as raised:
        device.poll_device_token(
            client,
            "http://gateway/token",
            "public-client",
            "device-code",
            30,
            1,
            monotonic=lambda: 0,
        )
    assert "sensitive detail" not in str(raised.value)


def _signed_token(**overrides: object) -> tuple[str, dict[str, object], int]:
    now = int(time.time())
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "qa-key"
    claims: dict[str, object] = {
        "iss": "https://trackflow-qa-abc123-18085.app.github.dev/realms/trackflow-codespaces-qa",
        "aud": "trackflow-mcp",
        "sub": "subject",
        "trackflow_user_id": "user-123",
        "scope": " ".join(sorted(device.SCOPES)),
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "qa-key"})
    return token, {"keys": [public_jwk]}, now


class _RunClient:
    def __init__(self, token: str, jwks: dict[str, object]):
        self.token = token
        self.jwks = jwks

    def __enter__(self) -> "_RunClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, endpoint: str, *, data: dict[str, str]) -> _Response:
        if endpoint.endswith("/auth/device"):
            return _Response(
                200,
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": (
                        "https://trackflow-qa-abc123-18085.app.github.dev/"
                        "realms/trackflow-codespaces-qa/device"
                    ),
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        return _Response(200, {"access_token": self.token})

    def get(self, endpoint: str) -> _Response:
        return _Response(200, self.jwks)


def test_device_run_emits_validated_token_only_to_non_tty_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, jwks, _now = _signed_token()
    monkeypatch.setattr(
        device,
        "load_runtime",
        lambda _path: {
            "QA_KEYCLOAK_REALM": "trackflow-codespaces-qa",
            "TRACKFLOW_CODESPACES_PUBLIC_ORIGIN": (
                "https://trackflow-qa-abc123-18085.app.github.dev"
            ),
            "QA_KEYCLOAK_PUBLIC_CLIENT_ID": "mcp-playground-qa",
            "QA_TRACKFLOW_USER_ID": "user-123",
        },
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    result = device.run(
        stdout=stdout,
        stderr=stderr,
        client_factory=lambda **_kwargs: _RunClient(token, jwks),
    )
    assert result == 0
    assert stdout.getvalue() == token
    assert token not in stderr.getvalue()
    assert "ABCD-EFGH" in stderr.getvalue()


def test_device_token_validates_signature_binding_audience_lifetime_and_scopes() -> None:
    token, jwks, now = _signed_token()
    claims = device.validate_token(
        token,
        jwks,
        "https://trackflow-qa-abc123-18085.app.github.dev/realms/trackflow-codespaces-qa",
        "user-123",
        now=now,
    )
    assert claims["aud"] == "trackflow-mcp"


@pytest.mark.parametrize(
    "override",
    [
        {"aud": "wrong-audience"},
        {"aud": ["trackflow-mcp", "extra-audience"]},
        {"iss": "https://issuer.invalid/realms/wrong"},
        {"trackflow_user_id": "other-user"},
        {"scope": "incidents:read"},
        {"exp": int(time.time()) - 1},
        {"exp": int(time.time()) + 600},
    ],
)
def test_device_token_rejects_claim_drift(override: dict[str, object]) -> None:
    token, jwks, now = _signed_token(**override)
    with pytest.raises(device.DeviceFlowError):
        device.validate_token(
            token,
            jwks,
            "https://trackflow-qa-abc123-18085.app.github.dev/realms/trackflow-codespaces-qa",
            "user-123",
            now=now,
        )
