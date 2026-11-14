"""Harden the ephemeral Keycloak realm for a public Codespaces origin."""

from __future__ import annotations

import os

import httpx

from validate_config import public_origin, validate_realm


DEVICE_GRANT_ATTRIBUTE = "oauth2.device.authorization.grant.enabled"
OPERATION_SCOPES = {
    "incidents:create",
    "incidents:read",
    "incidents:status",
    "inventory:read",
}


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
    public_client = required("QA_KEYCLOAK_PUBLIC_CLIENT_ID")
    mcp_client = required("QA_KEYCLOAK_MCP_CLIENT_ID")

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
        realm_config["accessTokenLifespan"] = 300
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
                "standardFlowEnabled": False,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "authorizationServicesEnabled": False,
                "fullScopeAllowed": False,
                "redirectUris": [],
                "webOrigins": [],
            }
        )
        attributes = dict(client_config.get("attributes") or {})
        attributes.pop("pkce.code.challenge.method", None)
        attributes[DEVICE_GRANT_ATTRIBUTE] = "true"
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
        verified_mappers = {
            mapper.get("name"): mapper for mapper in verified.get("protocolMappers", [])
        }
        audience_mapper = verified_mappers.get("trackflow-mcp-audience", {})
        identity_mapper = verified_mappers.get("trackflow-user-id", {})
        if (
            verified.get("publicClient") is not True
            or verified.get("standardFlowEnabled") is not False
            or verified.get("implicitFlowEnabled") is not False
            or verified.get("directAccessGrantsEnabled") is not False
            or verified.get("serviceAccountsEnabled") is not False
            or verified.get("authorizationServicesEnabled") is not False
            or verified.get("fullScopeAllowed") is not False
            or verified.get("redirectUris") not in ([], None)
            or verified.get("webOrigins") not in ([], None)
            or (verified.get("attributes") or {}).get(DEVICE_GRANT_ATTRIBUTE) != "true"
            or set(verified.get("optionalClientScopes", [])) != OPERATION_SCOPES
            or set(verified.get("defaultClientScopes", [])) != {"trackflow-sub"}
            or (audience_mapper.get("config") or {}).get("included.client.audience")
            != mcp_client
            or (identity_mapper.get("config") or {}).get("claim.name")
            != "trackflow_user_id"
        ):
            raise RuntimeError("Public QA client hardening verification failed.")

        mcp_matches = client.get(
            f"{base_url}/admin/realms/{realm}/clients",
            headers=headers,
            params={"clientId": mcp_client},
        )
        mcp_matches.raise_for_status()
        mcp_clients = mcp_matches.json()
        if len(mcp_clients) != 1:
            raise RuntimeError("Expected exactly one confidential MCP client.")
        mcp_config = mcp_clients[0]
        mcp_config.update(
            {
                "publicClient": False,
                "clientAuthenticatorType": "client-secret",
                "standardFlowEnabled": False,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "authorizationServicesEnabled": False,
                "fullScopeAllowed": False,
            }
        )
        mcp_attributes = dict(mcp_config.get("attributes") or {})
        mcp_attributes["standard.token.exchange.enabled"] = "true"
        mcp_attributes[DEVICE_GRANT_ATTRIBUTE] = "false"
        mcp_config["attributes"] = mcp_attributes
        mcp_update = client.put(
            f"{base_url}/admin/realms/{realm}/clients/{mcp_config['id']}",
            headers=headers,
            json=mcp_config,
        )
        mcp_update.raise_for_status()
        mcp_verified = client.get(
            f"{base_url}/admin/realms/{realm}/clients/{mcp_config['id']}",
            headers=headers,
        )
        mcp_verified.raise_for_status()
        mcp_verified_config = mcp_verified.json()
        if (
            mcp_verified_config.get("publicClient") is not False
            or mcp_verified_config.get("clientAuthenticatorType") != "client-secret"
            or mcp_verified_config.get("fullScopeAllowed") is not False
            or any(
                mcp_verified_config.get(flag) is not False
                for flag in (
                    "standardFlowEnabled",
                    "implicitFlowEnabled",
                    "directAccessGrantsEnabled",
                    "serviceAccountsEnabled",
                    "authorizationServicesEnabled",
                )
            )
            or (mcp_verified_config.get("attributes") or {}).get(
                "standard.token.exchange.enabled"
            )
            != "true"
            or (mcp_verified_config.get("attributes") or {}).get(
                DEVICE_GRANT_ATTRIBUTE
            )
            != "false"
        ):
            raise RuntimeError("MCP client grant hardening verification failed.")

        client_credentials = client.post(
            f"{base_url}/realms/{realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": mcp_client,
                "client_secret": required("QA_KEYCLOAK_MCP_CLIENT_SECRET"),
            },
        )
        if client_credentials.status_code not in {400, 401, 403}:
            raise RuntimeError("Client Credentials Grant is not blocked.")

        password_grant = client.post(
            f"{base_url}/realms/{realm}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": public_client,
                "username": required("QA_KEYCLOAK_USERNAME"),
                "password": required("QA_KEYCLOAK_PASSWORD"),
            },
        )
        if password_grant.status_code not in {400, 401, 403}:
            raise RuntimeError("Direct Access Grant is not blocked.")
        verified_realm = client.get(
            f"{base_url}/admin/realms/{realm}", headers=headers
        )
        verified_realm.raise_for_status()
        verified_realm_config = verified_realm.json()
        if (
            verified_realm_config.get("sslRequired") != "external"
            or verified_realm_config.get("registrationAllowed") is not False
            or verified_realm_config.get("accessTokenLifespan") != 300
        ):
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
