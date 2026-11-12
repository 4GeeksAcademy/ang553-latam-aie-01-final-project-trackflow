"""Fail-closed validation for the disposable Codespaces QA launcher."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse


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


def validate_playground(redirect_uri: str, web_origin: str) -> tuple[str, str]:
    if "*" in redirect_uri or "*" in web_origin:
        raise ValueError("Playground URLs must not contain wildcards.")
    forbidden = {"'", '"', "\\", "\r", "\n", "\t", " "}
    if any(character in redirect_uri or character in web_origin for character in forbidden):
        raise ValueError("Playground URLs contain unsafe shell or whitespace characters.")
    redirect = urlparse(redirect_uri)
    origin = urlparse(web_origin)
    for label, parsed in (("redirect URI", redirect), ("web origin", origin)):
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError(f"Playground {label} must be a credential-free HTTPS URL.")
    if origin.path not in ("", "/") or origin.params or origin.query:
        raise ValueError("Playground web origin must not contain path, query or parameters.")
    if redirect.params or redirect.query:
        raise ValueError("Playground redirect URI must not contain query or parameters.")
    normalized_origin = f"{origin.scheme}://{origin.netloc}"
    redirect_origin = f"{redirect.scheme}://{redirect.netloc}"
    if normalized_origin != redirect_origin:
        raise ValueError("Playground redirect URI and web origin must use the same origin.")
    if redirect.path in ("", "/"):
        raise ValueError("Playground redirect URI must contain an explicit callback path.")
    return redirect_uri, normalized_origin


def main() -> None:
    origin = public_origin()
    validate_realm(os.environ.get("QA_KEYCLOAK_REALM", ""))
    validate_playground(
        os.environ.get("TRACKFLOW_PLAYGROUND_REDIRECT_URI", ""),
        os.environ.get("TRACKFLOW_PLAYGROUND_ORIGIN", ""),
    )
    expected = os.environ.get("TRACKFLOW_CODESPACES_PUBLIC_ORIGIN")
    if expected and expected != origin:
        raise ValueError("Configured public origin does not match Codespaces.")
    print(origin)


if __name__ == "__main__":
    main()
