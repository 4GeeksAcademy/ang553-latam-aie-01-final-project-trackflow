"""Fail-closed validation for the disposable Codespaces QA launcher."""

from __future__ import annotations

import os
import re


SAFE_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SAFE_REALM = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def public_origin(environment: dict[str, str] | os._Environ[str] = os.environ) -> str:
    if environment.get("CODESPACES") != "true":
        raise ValueError("This launcher only runs inside GitHub Codespaces.")
    name = environment.get("CODESPACE_NAME", "")
    domain = environment.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "")
    if not SAFE_LABEL.fullmatch(name):
        raise ValueError("CODESPACE_NAME is missing or unsafe.")
    if not domain or any(not SAFE_LABEL.fullmatch(label) for label in domain.split(".")):
        raise ValueError("Codespaces forwarding domain is missing or unsafe.")
    public_label = f"{name}-18085"
    if not SAFE_LABEL.fullmatch(public_label):
        raise ValueError("Codespaces public host label would be invalid.")
    return f"https://{public_label}.{domain}"


def validate_realm(realm: str) -> str:
    if not SAFE_REALM.fullmatch(realm):
        raise ValueError("QA realm must contain only letters, digits, '_' or '-'.")
    return realm


def main() -> None:
    origin = public_origin()
    validate_realm(os.environ.get("QA_KEYCLOAK_REALM", ""))
    expected = os.environ.get("TRACKFLOW_CODESPACES_PUBLIC_ORIGIN")
    if expected and expected != origin:
        raise ValueError("Configured public origin does not match Codespaces.")
    print(origin)


if __name__ == "__main__":
    main()
