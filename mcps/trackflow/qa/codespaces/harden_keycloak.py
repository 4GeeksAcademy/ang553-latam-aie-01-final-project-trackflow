"""Harden the ephemeral Keycloak realm for a public Codespaces origin."""

from __future__ import annotations

import os

import httpx

from validate_config import public_origin, validate_playground, validate_realm


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Required QA environment variable is missing: {name}")
    return value


def main() -> None:
    if os.getenv("TRACKFLOW_MCP_QA") != "1":
        raise SystemExit("Refusing to harden Keycloak without TRACKFLOW_MCP_QA=1")
    base_url = required("QA_KEYCLOAK_BASE_URL").rstrip("/")
    if base_url != "http://127.0.0.1:18082":
        raise SystemExit("Keycloak administration must use isolated loopback.")

    expected_origin = public_origin()
    if required("TRACKFLOW_CODESPACES_PUBLIC_ORIGIN") != expected_origin:
        raise SystemExit("Public origin does not match the current Codespace.")
    realm = validate_realm(required("QA_KEYCLOAK_REALM"))
    redirect_uri, web_origin = validate_playground(
        required("TRACKFLOW_PLAYGROUND_REDIRECT_URI"),
        required("TRACKFLOW_PLAYGROUND_ORIGIN"),
    )
    public_client = required("QA_KEYCLOAK_PUBLIC_CLIENT_ID")

    with httpx.Client(timeout=15) as client:
        token_response = client.post(
            f"{base_url}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": required("QA_KEYCLOAK_ADMIN_USER"),
                "password": required("QA_KEYCLOAK_ADMIN_PASSWORD"),
            },
        )
        token_response.raise_for_status()
        headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}

        realm_response = client.get(f"{base_url}/admin/realms/{realm}", headers=headers)
        realm_response.raise_for_status()
        realm_config = realm_response.json()
        realm_config["sslRequired"] = "external"
        realm_config["registrationAllowed"] = False
        updated_realm = client.put(
            f"{base_url}/admin/realms/{realm}",
            headers=headers,
            json=realm_config,
        )
        updated_realm.raise_for_status()

        matches = client.get(
            f"{base_url}/admin/realms/{realm}/clients",
            headers=headers,
            params={"clientId": public_client},
        )
        matches.raise_for_status()
        clients = matches.json()
        if len(clients) != 1:
            raise RuntimeError("Expected exactly one public QA client.")
        client_config = clients[0]
        client_config.update(
            {
                "publicClient": True,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "fullScopeAllowed": False,
                "redirectUris": [redirect_uri],
                "webOrigins": [web_origin],
            }
        )
        attributes = dict(client_config.get("attributes") or {})
        attributes["pkce.code.challenge.method"] = "S256"
        client_config["attributes"] = attributes
        updated_client = client.put(
            f"{base_url}/admin/realms/{realm}/clients/{client_config['id']}",
            headers=headers,
            json=client_config,
        )
        updated_client.raise_for_status()

        verification = client.get(
            f"{base_url}/admin/realms/{realm}/clients/{client_config['id']}",
            headers=headers,
        )
        verification.raise_for_status()
        verified = verification.json()
        if (
            verified.get("publicClient") is not True
            or verified.get("standardFlowEnabled") is not True
            or
            verified.get("directAccessGrantsEnabled") is not False
            or verified.get("serviceAccountsEnabled") is not False
            or verified.get("fullScopeAllowed") is not False
            or verified.get("redirectUris") != [redirect_uri]
            or verified.get("webOrigins") != [web_origin]
            or (verified.get("attributes") or {}).get("pkce.code.challenge.method") != "S256"
        ):
            raise RuntimeError("Public QA client hardening verification failed.")
        verified_realm = client.get(
            f"{base_url}/admin/realms/{realm}", headers=headers
        )
        verified_realm.raise_for_status()
        if verified_realm.json().get("sslRequired") != "external":
            raise RuntimeError("QA realm TLS policy hardening verification failed.")

        anonymous_dcr = client.post(
            f"{base_url}/realms/{realm}/clients-registrations/default",
            json={},
        )
        if anonymous_dcr.status_code not in {401, 403}:
            raise RuntimeError("Anonymous Dynamic Client Registration is not blocked.")

    print("Ephemeral Codespaces Keycloak realm hardened.")


if __name__ == "__main__":
    main()
