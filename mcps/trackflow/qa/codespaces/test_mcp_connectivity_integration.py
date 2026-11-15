#!/usr/bin/env python3
"""
Integration test simulating Nginx → MCP connectivity.

This test validates that the fix for HTTP 502 actually works by:
1. Simulating what Nginx does (connect to host.docker.internal:18084)
2. Verifying that the MCP endpoint responds correctly
3. Ensuring Protected Resource Metadata is valid JSON

This requires the QA launcher to be running.
"""

import json
import subprocess
import sys
import time
from pathlib import Path


def wait_for_service(url, max_attempts=10):
    """Wait for a service to become available."""
    for attempt in range(max_attempts):
        try:
            result = subprocess.run(
                ["curl", "--silent", "--show-error", "--fail", "--max-time", "2", url],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return True, result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        if attempt < max_attempts - 1:
            time.sleep(1)

    return False, None


def test_mcp_direct_access():
    """Test direct access to MCP from host."""
    print("Test 1: Direct access to MCP on loopback")

    success, response = wait_for_service("http://127.0.0.1:18084/.well-known/oauth-protected-resource/mcp")

    if not success:
        print("  ✗ FAILED: MCP not responding on 127.0.0.1:18084")
        print("    Make sure start.sh has completed successfully")
        return False

    try:
        data = json.loads(response)
        if data.get("resource") == "http://127.0.0.1:18085/mcp":
            print("  ✓ PASS: MCP responds with correct resource URL")
            return True
        else:
            print(f"  ✗ FAILED: Resource mismatch: {data.get('resource')}")
            return False
    except json.JSONDecodeError:
        print(f"  ✗ FAILED: Invalid JSON response: {response[:100]}")
        return False


def test_nginx_gateway_access():
    """Test access through Nginx gateway."""
    print("\nTest 2: Access through Nginx gateway")

    # First wait for gateway to be ready
    print("  Waiting for Nginx gateway...")
    success, _ = wait_for_service("http://127.0.0.1:18085/", max_attempts=30)

    if not success:
        print("  ✗ FAILED: Nginx gateway not responding on 127.0.0.1:18085")
        return False

    print("  Gateway is ready, testing Protected Resource Metadata...")

    # Now test the MCP endpoint through Nginx
    try:
        result = subprocess.run(
            ["curl", "--silent", "--show-error", "--write-out", "\n%{http_code}",
             "http://127.0.0.1:18085/.well-known/oauth-protected-resource/mcp"],
            capture_output=True,
            text=True,
            timeout=10
        )

        lines = result.stdout.split('\n')
        http_code = lines[-1].strip()
        body = '\n'.join(lines[:-1])

        if http_code != "200":
            print(f"  ✗ FAILED: HTTP {http_code} (expected 200)")
            print(f"    This indicates Nginx cannot reach MCP")
            print(f"    Response: {body[:200]}")
            return False

        # Validate JSON
        try:
            data = json.loads(body)
            print(f"  ✓ HTTP 200 with valid JSON")
            print(f"    Resource: {data.get('resource')}")
            print(f"    Scopes: {data.get('scopes_supported', [])}")
            return True
        except json.JSONDecodeError:
            print(f"  ✗ FAILED: HTTP 200 but invalid JSON")
            print(f"    Response: {body[:200]}")
            return False

    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False


def test_oidc_discovery():
    """Test OIDC discovery through Nginx."""
    print("\nTest 3: OIDC discovery through Nginx")

    # Read realm name from runtime.env
    state_dir = Path(f"/tmp/trackflow-mcp-codespaces-{os.getuid()}")
    runtime_env = state_dir / "runtime.env"

    if not runtime_env.exists():
        print("  ⚠ SKIPPED: runtime.env not found")
        return True

    # Parse realm from runtime.env
    realm = None
    for line in runtime_env.read_text().split('\n'):
        if line.startswith("QA_KEYCLOAK_REALM="):
            realm = line.split("=", 1)[1].strip("'\"")
            break

    if not realm:
        print("  ⚠ SKIPPED: Could not find realm in runtime.env")
        return True

    try:
        result = subprocess.run(
            ["curl", "--silent", "--show-error", "--write-out", "\n%{http_code}",
             f"http://127.0.0.1:18085/realms/{realm}/.well-known/openid-configuration"],
            capture_output=True,
            text=True,
            timeout=10
        )

        lines = result.stdout.split('\n')
        http_code = lines[-1].strip()

        if http_code == "200":
            print(f"  ✓ OIDC discovery responds with HTTP 200")
            return True
        else:
            print(f"  ✗ FAILED: HTTP {http_code}")
            return False

    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False


def test_blocked_endpoints():
    """Test that blocked endpoints still return 404."""
    print("\nTest 4: Security - blocked endpoints")

    blocked = ["/_gateway/health", "/admin/", "/realms/master/"]
    all_blocked = True

    for endpoint in blocked:
        try:
            result = subprocess.run(
                ["curl", "--silent", "--write-out", "%{http_code}", "--output", "/dev/null",
                 f"http://127.0.0.1:18085{endpoint}"],
                capture_output=True,
                text=True,
                timeout=5
            )

            http_code = result.stdout.strip()
            if http_code == "404":
                print(f"  ✓ {endpoint} → 404")
            else:
                print(f"  ✗ {endpoint} → {http_code} (expected 404)")
                all_blocked = False
        except Exception as e:
            print(f"  ✗ {endpoint} → Error: {e}")
            all_blocked = False

    return all_blocked


if __name__ == "__main__":
    import os

    print("=" * 70)
    print("MCP Connectivity Integration Tests")
    print("(Requires QA launcher to be running)")
    print("=" * 70)
    print()

    tests = [
        ("Direct MCP access", test_mcp_direct_access),
        ("Nginx gateway access", test_nginx_gateway_access),
        ("OIDC discovery", test_oidc_discovery),
        ("Blocked endpoints", test_blocked_endpoints),
    ]

    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"✗ {name}: Unexpected error: {e}")
            results.append((name, False))

    print("\n" + "=" * 70)
    print("Summary:")
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}: {name}")

    failed = sum(1 for _, passed in results if not passed)
    if failed == 0:
        print("\n✅ All connectivity tests passed!")
        print("The HTTP 502 issue has been fixed.")
        sys.exit(0)
    else:
        print(f"\n❌ {failed} test(s) failed")
        print("Check the output above for details.")
        sys.exit(1)
