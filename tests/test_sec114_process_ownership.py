"""Windows PowerShell process-ownership regression for SEC-114 QA."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPOSITORY_ROOT / "mcps" / "trackflow" / "qa" / "run_sec114.ps1"


def run_self_test(tmp_path: Path, switch: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUNNER),
            "-EvidenceDirectory",
            str(tmp_path / "evidence"),
            switch,
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="Windows PowerShell 5.1 is required for the runner ownership test",
)
def test_runner_accepts_owned_child_and_rejects_alien_listener(tmp_path: Path) -> None:
    """Exercise both deterministic branches without starting infrastructure."""

    completed = run_self_test(tmp_path, "-OwnershipSelfTest")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "SEC114_PROCESS_OWNERSHIP_SELF_TEST_PASS"


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="Windows PowerShell 5.1 is required for the runner observability test",
)
def test_status_counts_guardrails_by_failure_category(tmp_path: Path) -> None:
    """Keep guard categories while separating MCP sources and missing logs."""

    completed = run_self_test(tmp_path, "-ObservabilitySelfTest")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "SEC114_OBSERVABILITY_SELF_TEST_PASS"


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="Windows PowerShell 5.1 is required for the runner shutdown test",
)
def test_stop_is_recoverable_and_rejects_reused_or_alien_resources(
    tmp_path: Path,
) -> None:
    """Cover complete, partial, vanished, reused and alien shutdown states."""

    completed = run_self_test(tmp_path, "-ShutdownSelfTest")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "SEC114_SHUTDOWN_RECOVERY_SELF_TEST_PASS"
