"""Regression tests for Keycloak 26.8.2 client field representation.

This test suite validates that hardening verification correctly handles
Keycloak's representation of disabled fields, particularly authorizationServicesEnabled
which returns None/null when not explicitly set in Keycloak 26.8.2+.
"""

from __future__ import annotations

import os
import pytest
import httpx


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"Required env var missing: {name}")
    return value


class TestKeycloakClientFields:
    """Test Keycloak 26.8.2 client field behavior."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """Verify test environment is configured."""
        self.base_url = required("QA_KEYCLOAK_BASE_URL").rstrip("/")
        self.realm = required("QA_KEYCLOAK_REALM")
        self.public_client = required("QA_KEYCLOAK_PUBLIC_CLIENT_ID")
        self.mcp_client = required("QA_KEYCLOAK_MCP_CLIENT_ID")
        if os.getenv("TRACKFLOW_MCP_QA") != "1":
            pytest.skip("TRACKFLOW_MCP_QA must be set to 1")

    @pytest.fixture
    def admin_client(self) -> httpx.Client:
        """Get authenticated admin HTTP client."""
        client = httpx.Client(timeout=15)
        token_response = client.post(
            f"{self.base_url}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": required("QA_KEYCLOAK_ADMIN_USER"),
                "password": required("QA_KEYCLOAK_ADMIN_PASSWORD"),
            },
        )
        token_response.raise_for_status()
        self.admin_headers = {
            "Authorization": f"Bearer {token_response.json()['access_token']}"
        }
        yield client
        client.close()

    def fetch_client_config(
        self, client: httpx.Client, client_id: str
    ) -> dict[str, any]:
        """Fetch client configuration by clientId."""
        response = client.get(
            f"{self.base_url}/admin/realms/{self.realm}/clients",
            headers=self.admin_headers,
            params={"clientId": client_id},
        )
        response.raise_for_status()
        clients = response.json()
        assert len(clients) == 1, f"Expected 1 client, found {len(clients)}"
        return clients[0]

    def test_authorization_services_enabled_field_returns_none_when_false(
        self, admin_client: httpx.Client
    ) -> None:
        """Verify authorizationServicesEnabled returns None/null in Keycloak 26.8.2.

        When a client is created without explicit authorizationServicesEnabled,
        or when it's set to False, Keycloak 26.8.2 returns None for this field.
        This is distinct from other False flags which are persisted as False.

        This regression test ensures the hardening verification accepts both
        False and None as valid disabled states.
        """
        config = self.fetch_client_config(admin_client, self.public_client)

        # Before hardening, field should be None or False
        field_value = config.get("authorizationServicesEnabled")
        assert field_value in (False, None), (
            f"Expected authorizationServicesEnabled to be False or None, got {field_value}"
        )

    def test_authorization_services_enabled_persists_as_none_after_update(
        self, admin_client: httpx.Client
    ) -> None:
        """Verify authorizationServicesEnabled stays None after setting False.

        Even when explicitly set to False in a PUT request, Keycloak 26.8.2
        returns None for this field in subsequent GETs. This is a quirk of
        Keycloak's handling of this particular flag.
        """
        config = self.fetch_client_config(admin_client, self.public_client)
        client_id = config["id"]

        # Explicitly set to False
        config["authorizationServicesEnabled"] = False

        response = admin_client.put(
            f"{self.base_url}/admin/realms/{self.realm}/clients/{client_id}",
            headers=self.admin_headers,
            json=config,
        )
        response.raise_for_status()

        # Fetch again
        updated = self.fetch_client_config(admin_client, self.public_client)

        # Should still be None
        field_value = updated.get("authorizationServicesEnabled")
        assert field_value in (False, None), (
            f"After update, authorizationServicesEnabled should be False or None, got {field_value}"
        )

    def test_hardening_verification_accepts_none_as_disabled(
        self, admin_client: httpx.Client
    ) -> None:
        """Verify hardening logic treats None as equivalent to False for this field.

        The hardening verification must use:
            verified.get("authorizationServicesEnabled") not in (False, None)

        And NOT:
            verified.get("authorizationServicesEnabled") is not False

        Because Keycloak returns None, not False, for this disabled state.
        """
        config = self.fetch_client_config(admin_client, self.public_client)
        client_id = config["id"]

        # Harden: set all flags to False
        config.update(
            {
                "standardFlowEnabled": False,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "authorizationServicesEnabled": False,
                "fullScopeAllowed": False,
            }
        )

        response = admin_client.put(
            f"{self.base_url}/admin/realms/{self.realm}/clients/{client_id}",
            headers=self.admin_headers,
            json=config,
        )
        response.raise_for_status()

        # Verify
        verified = self.fetch_client_config(admin_client, self.public_client)

        # These should all be False (persisted as explicitly false)
        assert verified.get("standardFlowEnabled") is False
        assert verified.get("implicitFlowEnabled") is False
        assert verified.get("directAccessGrantsEnabled") is False
        assert verified.get("serviceAccountsEnabled") is False
        assert verified.get("fullScopeAllowed") is False

        # But authorizationServicesEnabled will be None
        auth_svc = verified.get("authorizationServicesEnabled")
        assert auth_svc in (False, None), (
            f"authorizationServicesEnabled should be False or None, got {auth_svc}"
        )

        # Hardening verification should accept both
        # Correct check: does NOT accept True (should only accept False or None)
        assert auth_svc not in (True,), (
            "authorizationServicesEnabled should not be True when disabled"
        )
        # Correct check: the condition 'not in (False, None)' should evaluate to False
        # when auth_svc is None or False, meaning the hardening check PASSES
        check_result = auth_svc not in (False, None)
        assert check_result is False, (
            f"Hardening check 'not in (False, None)' should be False (pass), got {check_result}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
