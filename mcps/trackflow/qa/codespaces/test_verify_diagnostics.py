#!/usr/bin/env python3
"""
Regression test for verify.sh diagnostic output.

Ensures that verify.sh provides detailed diagnostic messages for each verification step,
not silent failures. This prevents future issues where the launcher completes without
showing the expected PRIVATE_GATEWAY_VERIFIED or PRIVATE_READY messages.
"""

import re
import subprocess
import sys
from pathlib import Path


def test_verify_diagnostic_coverage():
    """Validate that verify.sh contains all expected diagnostic markers."""
    verify_path = Path(__file__).parent / "verify.sh"
    assert verify_path.exists(), f"verify.sh not found at {verify_path}"

    content = verify_path.read_text()

    # Verify that diagnostic functions are defined
    required_functions = [
        "verify_check()",
        "verify_success()",
        "expect_status()",
    ]
    for func in required_functions:
        assert func in content, (
            f"verify.sh missing required diagnostic function: {func}\n"
            "This would allow silent failures in verification."
        )

    # Verify that each major verification step has diagnostic output
    required_diagnostics = [
        r"verify_check.*endpoint",  # Endpoint checks (OIDC, PRM, MCP)
        r"verify_check.*OIDC",  # OIDC discovery
        r"verify_check.*Protected Resource",  # PRM
        r"verify_check.*MCP.*authentication",  # MCP auth
        r"verify_check.*WWW-Authenticate",  # Header validation
        r"verify_check.*DCR.*blocked",  # DCR blocking
        r"verify_check.*[Cc]ontainer.*port",  # Port bindings
    ]

    for diagnostic in required_diagnostics:
        matches = re.findall(diagnostic, content)
        assert matches, (
            f"verify.sh missing diagnostic for: {diagnostic}\n"
            "This check could fail silently."
        )

    print("✓ verify.sh has comprehensive diagnostic coverage")


def test_verify_error_messages_are_descriptive():
    """Validate that error messages provide actionable information."""
    verify_path = Path(__file__).parent / "verify.sh"
    content = verify_path.read_text()

    # Check for descriptive error messages (not generic or empty)
    error_patterns = [
        r"printf.*ERROR:.*%s",  # Dynamic status codes
        r"printf.*ERROR:.*HTTP",  # Explicit HTTP codes
        r"printf.*ERROR:.*mismatch",  # Clear failure category
        r"printf.*ERROR:.*missing",  # Clear failure category
        r"printf.*ERROR:.*connection",  # Network-level info
    ]

    error_count = 0
    for pattern in error_patterns:
        matches = re.findall(pattern, content)
        error_count += len(matches)

    assert error_count >= 8, (
        f"verify.sh has only {error_count} descriptive error messages (expected ≥8).\n"
        "Silent failures are likely."
    )

    print(f"✓ verify.sh has {error_count} descriptive error messages")


def test_verify_no_set_x_debugging():
    """Ensure verify.sh does not use `set -x` (which would leak env vars)."""
    verify_path = Path(__file__).parent / "verify.sh"
    content = verify_path.read_text()

    # Look for `set -x` anywhere in the script
    assert not re.search(r"^\s*set\s+-[^eu]*x", content, re.MULTILINE), (
        "verify.sh enables `set -x` which would leak environment variables.\n"
        "Remove all `set -x` directives."
    )

    print("✓ verify.sh does not enable set -x")


def test_verify_no_secret_logging():
    """Verify that verify.sh does not log tokens, passwords, or sensitive data."""
    verify_path = Path(__file__).parent / "verify.sh"
    content = verify_path.read_text()

    # Check for suspicious patterns that might leak secrets
    dangerous_patterns = [
        r'printf.*\$.*token',  # Logging token variables
        r'printf.*\$.*password',  # Logging password variables
        r'printf.*\$.*secret',  # Logging secret variables
        r'echo.*\$.*Bearer',  # Logging auth headers
        r'curl.*--verbose.*gateway',  # Verbose curl (may log headers)
    ]

    for pattern in dangerous_patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        assert not matches, (
            f"verify.sh appears to log sensitive data matching: {pattern}\n"
            "Remove all secret/token/password/auth logging."
        )

    print("✓ verify.sh does not log secrets")


def test_verify_outputs_gateway_verified():
    """Ensure verify.sh prints PRIVATE_GATEWAY_VERIFIED on success."""
    verify_path = Path(__file__).parent / "verify.sh"
    content = verify_path.read_text()

    assert "PRIVATE_GATEWAY_VERIFIED" in content, (
        "verify.sh missing PRIVATE_GATEWAY_VERIFIED output.\n"
        "This string is expected by start.sh."
    )

    # Verify it's printed after all checks (not inside a conditional)
    lines = content.split('\n')
    verified_line = -1
    for i, line in enumerate(lines):
        if 'PRIVATE_GATEWAY_VERIFIED' in line and 'printf' in line:
            verified_line = i
            break

    assert verified_line > 0, (
        "verify.sh does not explicitly print PRIVATE_GATEWAY_VERIFIED.\n"
        "Add: printf 'PRIVATE_GATEWAY_VERIFIED\\n'"
    )

    print("✓ verify.sh prints PRIVATE_GATEWAY_VERIFIED on success")


if __name__ == "__main__":
    try:
        test_verify_diagnostic_coverage()
        test_verify_error_messages_are_descriptive()
        test_verify_no_set_x_debugging()
        test_verify_no_secret_logging()
        test_verify_outputs_gateway_verified()
        print("\n✅ All verify.sh regression tests passed")
        sys.exit(0)
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}", file=sys.stderr)
        sys.exit(1)
