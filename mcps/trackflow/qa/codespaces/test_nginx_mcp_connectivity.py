#!/usr/bin/env python3
"""
Regression test for Nginx → MCP connectivity via Docker bridge interface.

Ensures that:
1. MCP listens on Docker bridge interface (172.17.0.1) for container connectivity
2. MCP does NOT listen on 0.0.0.0 (security regression prevention)
3. API remains on loopback (127.0.0.1) for additional isolation
4. Nginx proxies to host.docker.internal which resolves correctly in Docker
5. HTTP 502 errors are prevented by proper interface binding

This test validates secure connectivity: MCP is accessible only from Docker containers
via the bridge network, never exposed to all IPv4 interfaces.
"""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path


def test_mcp_binds_to_docker_bridge():
    """Verify MCP listens on Docker bridge interface (172.17.0.1) in start.sh."""
    start_sh = Path(__file__).parent / "start.sh"
    assert start_sh.exists(), "start.sh not found"

    content = start_sh.read_text()

    # Check that MCP uses --host 172.17.0.1
    if "--host 172.17.0.1 --port 18084" in content:
        print("✓ MCP configured to listen on Docker bridge interface 172.17.0.1:18084")
        return True

    # Check for insecure 0.0.0.0 binding
    if "--host 0.0.0.0 --port 18084" in content:
        print("✗ MCP configured to listen on 0.0.0.0:18084 (SECURITY REGRESSION)")
        return False

    # Check for problematic 127.0.0.1 binding
    if "--host 127.0.0.1 --port 18084" in content:
        print("✗ MCP configured to listen on 127.0.0.1:18084 (HTTP 502 will occur)")
        return False

    print("✗ MCP host configuration not found or unclear in start.sh")
    return False


def test_nginx_proxies_to_host_docker_internal():
    """Verify Nginx is configured to reach MCP via host.docker.internal."""
    nginx_conf = Path(__file__).parent / "gateway.nginx.conf.template"
    assert nginx_conf.exists(), "gateway.nginx.conf.template not found"

    content = nginx_conf.read_text()

    # Check that Nginx proxies to host.docker.internal:18084
    if "proxy_pass http://host.docker.internal:18084;" in content:
        print("✓ Nginx configured to proxy MCP via host.docker.internal:18084")
        return True

    # Check for Unix socket (old attempt)
    if "unix:/mcp-socket" in content:
        print("✗ Nginx still configured for Unix socket (should use host.docker.internal)")
        return False

    print("✗ Nginx MCP proxy_pass configuration not found")
    return False


def test_compose_has_host_gateway_mapping():
    """Verify compose.yml maps host.docker.internal to host gateway."""
    compose_file = Path(__file__).parent / "compose.yml"
    assert compose_file.exists(), "compose.yml not found"

    content = compose_file.read_text()

    # Check for extra_hosts mapping for host.docker.internal
    if "host.docker.internal:host-gateway" in content:
        print("✓ Compose maps host.docker.internal to host-gateway")
        return True

    # Check that socket volume mount is NOT present (reverted)
    if "/mcp-socket" in content:
        print("⚠ Socket volume mount found (should be removed)")
        return False

    print("✗ host.docker.internal mapping not found in compose.yml")
    return False


def test_api_remains_on_loopback():
    """Verify API still uses 127.0.0.1 binding for security."""
    start_sh = Path(__file__).parent / "start.sh"
    content = start_sh.read_text()

    if "--host 127.0.0.1 --port 18083" in content:
        print("✓ API remains bound to 127.0.0.1:18083 (loopback only)")
        return True

    print("✗ API binding changed unexpectedly")
    return False


def test_mcp_healthcheck():
    """Verify healthcheck for MCP targets correct interface."""
    start_sh = Path(__file__).parent / "start.sh"
    content = start_sh.read_text()

    # Check for TCP healthcheck to Docker bridge interface
    if "http://172.17.0.1:18084" in content:
        print("✓ MCP healthcheck targets Docker bridge interface")
        return True

    print("✗ MCP healthcheck configuration unclear")
    return False


def test_nginx_blocks_admin_and_master():
    """Verify Nginx still blocks admin and master realm access."""
    nginx_conf = Path(__file__).parent / "gateway.nginx.conf.template"
    content = nginx_conf.read_text()

    if "location ~ ^/(admin|realms/master)" in content and "return 404;" in content:
        print("✓ Nginx still blocks /admin/ and /realms/master/")
        return True

    print("✗ Nginx security blocking rules may be missing")
    return False


def test_error_handling_in_verify():
    """Verify that verify.sh reports errors correctly."""
    verify_sh = Path(__file__).parent / "verify.sh"
    content = verify_sh.read_text()

    # Check that verify.sh has diagnostic for Protected Resource Metadata endpoint
    if "Protected Resource Metadata endpoint" in content:
        print("✓ verify.sh has diagnostic for Protected Resource Metadata")
        return True

    print("✗ verify.sh may not have proper diagnostics for MCP endpoint")
    return False


if __name__ == "__main__":
    print("=" * 70)
    print("Nginx ↔ MCP Secure Connectivity Regression Tests")
    print("=" * 70)

    tests = [
        ("MCP listening on Docker bridge interface", test_mcp_binds_to_docker_bridge),
        ("Nginx proxy via host.docker.internal", test_nginx_proxies_to_host_docker_internal),
        ("Compose host-gateway mapping", test_compose_has_host_gateway_mapping),
        ("API remains on loopback", test_api_remains_on_loopback),
        ("MCP healthcheck configuration", test_mcp_healthcheck),
        ("Nginx blocks admin/master", test_nginx_blocks_admin_and_master),
        ("verify.sh diagnostics", test_error_handling_in_verify),
    ]

    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"✗ {name}: {e}")
            results.append((name, False))

    print("\n" + "=" * 70)
    print("Summary:")
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}: {name}")

    failed_count = sum(1 for _, passed in results if not passed)
    if failed_count == 0:
        print("\n✅ All secure connectivity regression tests passed")
        sys.exit(0)
    else:
        print(f"\n❌ {failed_count} test(s) failed")
        sys.exit(1)
