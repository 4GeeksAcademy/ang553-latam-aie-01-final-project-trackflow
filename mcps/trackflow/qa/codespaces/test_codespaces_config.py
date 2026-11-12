from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "trackflow_codespaces_validate",
    HERE / "validate_config.py",
)
assert SPEC and SPEC.loader
validate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validate)


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


def test_playground_requires_exact_https_origin_and_callback_path() -> None:
    assert validate.validate_playground(
        "https://playground.example/oauth/callback",
        "https://playground.example",
    ) == (
        "https://playground.example/oauth/callback",
        "https://playground.example",
    )


@pytest.mark.parametrize(
    ("redirect", "origin"),
    [
        ("http://playground.example/oauth/callback", "https://playground.example"),
        ("https://playground.example/*", "https://playground.example"),
        ("https://playground.example", "https://playground.example"),
        ("https://other.example/oauth/callback", "https://playground.example"),
        ("https://user:secret@playground.example/callback", "https://playground.example"),
        ("https://playground.example/callback?token=x", "https://playground.example"),
        ("https://playground.example/callback'bad", "https://playground.example"),
        ("https://playground.example/callback", "https://playground.example/path"),
    ],
)
def test_playground_rejects_unsafe_or_inexact_urls(
    redirect: str, origin: str
) -> None:
    with pytest.raises(ValueError):
        validate.validate_playground(redirect, origin)


def test_gateway_has_allowlist_and_explicit_administration_denies() -> None:
    config = (HERE / "gateway.nginx.conf.template").read_text(encoding="utf-8")
    assert "location = /mcp" in config
    assert "location = /.well-known/oauth-protected-resource/mcp" in config
    assert (
        "location = /realms/${QA_KEYCLOAK_REALM}/.well-known/openid-configuration"
        in config
    )
    assert "/protocol/openid-connect/(auth|token|certs)" in config
    assert "/login-actions/" in config
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
