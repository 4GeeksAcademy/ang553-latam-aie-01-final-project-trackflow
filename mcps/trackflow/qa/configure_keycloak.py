"""Configure an ephemeral Keycloak realm for opt-in MCP integration QA.

All credentials are required through environment variables. The script never
prints or persists them.
"""

from __future__ import annotations

import os

import httpx


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Required QA environment variable is missing: {name}")
    return value


def main() -> None:
    if os.getenv("TRACKFLOW_MCP_QA") != "1":
        raise SystemExit("Refusing to configure Keycloak without TRACKFLOW_MCP_QA=1")

    base_url = required("QA_KEYCLOAK_BASE_URL").rstrip("/")
    if base_url != "http://127.0.0.1:18082":
        raise SystemExit("QA Keycloak URL must be the isolated loopback endpoint")

    admin_user = required("QA_KEYCLOAK_ADMIN_USER")
    admin_password = required("QA_KEYCLOAK_ADMIN_PASSWORD")
    realm = required("QA_KEYCLOAK_REALM")
    public_client = required("QA_KEYCLOAK_PUBLIC_CLIENT_ID")
    mcp_client = required("QA_KEYCLOAK_MCP_CLIENT_ID")
    mcp_secret = required("QA_KEYCLOAK_MCP_CLIENT_SECRET")
    api_client = required("QA_KEYCLOAK_API_CLIENT_ID")
    username = required("QA_KEYCLOAK_USERNAME")
    password = required("QA_KEYCLOAK_PASSWORD")
    trackflow_user_id = required("QA_TRACKFLOW_USER_ID")

    with httpx.Client(timeout=15) as client:
        admin_response = client.post(
            f"{base_url}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": admin_user,
                "password": admin_password,
            },
        )
        admin_response.raise_for_status()
        admin_token = admin_response.json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        existing = client.get(
            f"{base_url}/admin/realms/{realm}", headers=admin_headers
        )
        if existing.status_code == 200:
            deleted = client.delete(
                f"{base_url}/admin/realms/{realm}", headers=admin_headers
            )
            deleted.raise_for_status()
        elif existing.status_code != 404:
            existing.raise_for_status()

        scopes = [
            "incidents:create",
            "incidents:read",
            "incidents:status",
            "inventory:read",
        ]
        subject_scope = {
            "name": "trackflow-sub",
            "protocol": "openid-connect",
            "attributes": {
                "include.in.token.scope": "false",
                "display.on.consent.screen": "false",
            },
            "protocolMappers": [
                {
                    "name": "subject",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-sub-mapper",
                    "config": {
                        "access.token.claim": "true",
                        "id.token.claim": "true",
                        "introspection.token.claim": "true",
                    },
                }
            ],
        }
        operation_scopes = [
            {
                "name": scope,
                "protocol": "openid-connect",
                "attributes": {
                    "include.in.token.scope": "true",
                    "display.on.consent.screen": "false",
                },
            }
            for scope in scopes
        ]
        realm_config = {
            "realm": realm,
            "enabled": True,
            "sslRequired": "none",
            "registrationAllowed": False,
            "accessTokenLifespan": 300,
            "clientScopes": [subject_scope, *operation_scopes],
            "clients": [
                {
                    "clientId": public_client,
                    "enabled": True,
                    "protocol": "openid-connect",
                    "publicClient": True,
                    "standardFlowEnabled": True,
                    "directAccessGrantsEnabled": True,
                    "serviceAccountsEnabled": False,
                    "fullScopeAllowed": False,
                    "defaultClientScopes": ["trackflow-sub"],
                    "optionalClientScopes": scopes,
                    "redirectUris": [
                        "http://localhost:6274/oauth/callback",
                        "http://127.0.0.1:6274/oauth/callback",
                        "http://localhost:3001/backoffice/support",
                        "http://127.0.0.1:3001/backoffice/support",
                    ],
                    "webOrigins": [
                        "http://localhost:3001",
                        "http://127.0.0.1:3001",
                    ],
                    "attributes": {"pkce.code.challenge.method": "S256"},
                    "protocolMappers": [
                        {
                            "name": "trackflow-mcp-audience",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-audience-mapper",
                            "config": {
                                "included.client.audience": mcp_client,
                                "access.token.claim": "true",
                                "id.token.claim": "false",
                            },
                        },
                        {
                            "name": "trackflow-user-id",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-usermodel-attribute-mapper",
                            "config": {
                                "user.attribute": "trackflow_user_id",
                                "claim.name": "trackflow_user_id",
                                "jsonType.label": "String",
                                "multivalued": "false",
                                "access.token.claim": "true",
                                "id.token.claim": "false",
                                "userinfo.token.claim": "false",
                            },
                        },
                    ],
                },
                {
                    "clientId": mcp_client,
                    "enabled": True,
                    "protocol": "openid-connect",
                    "publicClient": False,
                    "clientAuthenticatorType": "client-secret",
                    "secret": mcp_secret,
                    "standardFlowEnabled": False,
                    "directAccessGrantsEnabled": False,
                    "serviceAccountsEnabled": True,
                    "fullScopeAllowed": False,
                    "defaultClientScopes": ["trackflow-sub"],
                    "optionalClientScopes": scopes,
                    "attributes": {"standard.token.exchange.enabled": "true"},
                    "protocolMappers": [
                        {
                            "name": "trackflow-api-audience",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-audience-mapper",
                            "config": {
                                "included.client.audience": api_client,
                                "access.token.claim": "true",
                                "id.token.claim": "false",
                            },
                        },
                        {
                            "name": "trackflow-user-id",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-usermodel-attribute-mapper",
                            "config": {
                                "user.attribute": "trackflow_user_id",
                                "claim.name": "trackflow_user_id",
                                "jsonType.label": "String",
                                "multivalued": "false",
                                "access.token.claim": "true",
                                "id.token.claim": "false",
                                "userinfo.token.claim": "false",
                            },
                        }
                    ],
                },
                {
                    "clientId": api_client,
                    "enabled": True,
                    "protocol": "openid-connect",
                    "bearerOnly": True,
                    "fullScopeAllowed": False,
                    "defaultClientScopes": [],
                    "optionalClientScopes": scopes,
                },
            ],
            "users": [
                {
                    "username": username,
                    "enabled": True,
                    "firstName": "MCP",
                    "lastName": "QA",
                    "email": "mcp-qa-user@example.invalid",
                    "emailVerified": True,
                    "requiredActions": [],
                    "attributes": {"trackflow_user_id": [trackflow_user_id]},
                    "credentials": [
                        {"type": "password", "value": password, "temporary": False}
                    ],
                }
            ],
        }
        response = client.post(
            f"{base_url}/admin/realms",
            headers=admin_headers,
            json=realm_config,
        )
        response.raise_for_status()

    print("Ephemeral Keycloak realm configured for TrackFlow MCP QA.")


if __name__ == "__main__":
    main()
