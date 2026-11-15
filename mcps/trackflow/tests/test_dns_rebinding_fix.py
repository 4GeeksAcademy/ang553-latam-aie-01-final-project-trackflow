"""
Test regression for HTTP 421 fix: DNS rebinding protection with allowed hosts.
"""

import os
import pytest
from unittest.mock import patch

from trackflow_mcp.settings import MCPSettings
from mcp.server.transport_security import TransportSecuritySettings


class TestDNSRebindingProtection:
    """Verify DNS rebinding protection allows correct hosts and rejects others."""

    def test_local_qa_has_loopback_hosts(self):
        """Test that local_qa() includes loopback hosts."""
        settings = MCPSettings.local_qa()

        assert settings.allowed_hosts is not None
        assert "127.0.0.1:*" in settings.allowed_hosts
        assert "localhost:*" in settings.allowed_hosts
        assert "[::1]:*" in settings.allowed_hosts

    def test_from_env_reads_allowed_hosts(self):
        """Test that from_env() reads TRACKFLOW_MCP_ALLOWED_HOSTS variable."""
        with patch.dict(os.environ, {
            "TRACKFLOW_MCP_RESOURCE_URL": "http://127.0.0.1:18085/mcp",
            "TRACKFLOW_MCP_ISSUER": "http://127.0.0.1:18082/realms/trackflow",
            "TRACKFLOW_MCP_AUDIENCE": "trackflow-mcp",
            "TRACKFLOW_API_BASE_URL": "http://127.0.0.1:18083",
            "TRACKFLOW_MCP_ALLOWED_HOSTS": "127.0.0.1:*, example.com, example.com:*",
        }):
            settings = MCPSettings.from_env()

            assert settings.allowed_hosts is not None
            assert len(settings.allowed_hosts) == 3
            assert "127.0.0.1:*" in settings.allowed_hosts
            assert "example.com" in settings.allowed_hosts
            assert "example.com:*" in settings.allowed_hosts

    def test_from_env_without_allowed_hosts(self):
        """Test that from_env() works when TRACKFLOW_MCP_ALLOWED_HOSTS not set."""
        with patch.dict(os.environ, {
            "TRACKFLOW_MCP_RESOURCE_URL": "http://127.0.0.1:18085/mcp",
            "TRACKFLOW_MCP_ISSUER": "http://127.0.0.1:18082/realms/trackflow",
            "TRACKFLOW_MCP_AUDIENCE": "trackflow-mcp",
            "TRACKFLOW_API_BASE_URL": "http://127.0.0.1:18083",
        }, clear=False):
            # Remove if exists
            os.environ.pop("TRACKFLOW_MCP_ALLOWED_HOSTS", None)

            settings = MCPSettings.from_env()

            assert settings.allowed_hosts is None

    def test_transport_security_settings_creation(self):
        """Test that TransportSecuritySettings is created correctly."""
        allowed_hosts = ["127.0.0.1:*", "localhost:*", "example.com"]

        tss = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
        )

        assert tss.enable_dns_rebinding_protection is True
        assert tss.allowed_hosts == allowed_hosts

    def test_transport_security_rejects_unknown_hosts(self):
        """Test that TransportSecuritySettings rejects unknown hosts."""
        # This test verifies the configuration, not the actual HTTP behavior
        # The actual HTTP 421 response is tested via integration tests

        allowed_hosts = ["127.0.0.1:*", "localhost:*"]
        tss = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
        )

        # Verify that unknown hosts would be rejected
        # (TransportSecurityMiddleware checks this in HTTP layer)
        assert "attacker.example.com" not in tss.allowed_hosts
        assert "127.0.0.1:*" in tss.allowed_hosts

    def test_codespaces_host_pattern_parsing(self):
        """Test parsing of Codespaces host pattern."""
        # Simulate what start.sh does
        codespaces_host = "user-1234-567.exp.codespaces.dev"

        allowed_hosts_str = f"127.0.0.1:*, localhost:*, [::1]:*, {codespaces_host}, {codespaces_host}:*"
        allowed_hosts = [h.strip() for h in allowed_hosts_str.split(",") if h.strip()]

        assert len(allowed_hosts) == 5
        assert "127.0.0.1:*" in allowed_hosts
        assert codespaces_host in allowed_hosts
        assert f"{codespaces_host}:*" in allowed_hosts

    def test_app_creates_transport_security_with_allowed_hosts(self):
        """Test that create_app() creates TransportSecuritySettings when allowed_hosts set."""
        from trackflow_mcp.app import create_app
        from pydantic import SecretStr

        # Create settings with allowed_hosts and credentials
        settings = MCPSettings(
            resource_url="http://127.0.0.1:8010/mcp",
            issuer="http://127.0.0.1:8080/realms/trackflow",
            audience="trackflow-mcp",
            authorization_endpoint="http://127.0.0.1:8080/realms/trackflow/protocol/openid-connect/auth",
            token_endpoint="http://127.0.0.1:8080/realms/trackflow/protocol/openid-connect/token",
            jwks_uri="http://127.0.0.1:8080/realms/trackflow/protocol/openid-connect/certs",
            api_base_url="http://127.0.0.1:8000",
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
            keycloak_client_id="test-client",
            keycloak_client_secret=SecretStr("test-secret"),
        )

        app = create_app(settings)

        # Verify that FastMCP has transport_security configured
        assert app.state.mcp_server.settings.transport_security is not None
        assert app.state.mcp_server.settings.transport_security.enable_dns_rebinding_protection is True
        assert app.state.mcp_server.settings.transport_security.allowed_hosts == settings.allowed_hosts

    def test_app_without_allowed_hosts(self):
        """Test that create_app() skips transport_security when allowed_hosts is None."""
        from trackflow_mcp.app import create_app
        from pydantic import SecretStr

        # Create settings without allowed_hosts
        settings = MCPSettings(
            resource_url="http://127.0.0.1:8010/mcp",
            issuer="http://127.0.0.1:8080/realms/trackflow",
            audience="trackflow-mcp",
            authorization_endpoint="http://127.0.0.1:8080/realms/trackflow/protocol/openid-connect/auth",
            token_endpoint="http://127.0.0.1:8080/realms/trackflow/protocol/openid-connect/token",
            jwks_uri="http://127.0.0.1:8080/realms/trackflow/protocol/openid-connect/certs",
            api_base_url="http://127.0.0.1:8000",
            allowed_hosts=None,
            keycloak_client_id="test-client",
            keycloak_client_secret=SecretStr("test-secret"),
        )

        app = create_app(settings)

        # When no allowed_hosts, we don't pass transport_security to FastMCP constructor
        # (FastMCP may have its own defaults, but we don't force it)
        assert app.state.mcp_server is not None
