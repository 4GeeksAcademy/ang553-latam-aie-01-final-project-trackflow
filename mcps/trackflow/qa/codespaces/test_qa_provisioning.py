"""Regression tests for QA DDL provisioning and seed_trackflow ordering.

These tests verify:
1. DDL provisioning correctly creates all 4 operational tables
2. Seed script fails predictably if DDL is not applied first
3. Full integration: DDL → seed succeeds
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
QA_CODESPACES_DIR = REPOSITORY_ROOT / "mcps" / "trackflow" / "qa" / "codespaces"


@pytest.fixture
def qa_database():
    """Create an ephemeral QA database for testing.

    Uses the same loopback PostgreSQL that the launcher would use,
    but isolates each test in a separate database to prevent interference.
    """
    if os.getenv("TRACKFLOW_MCP_QA") != "1":
        pytest.skip("TRACKFLOW_MCP_QA=1 not set; skipping QA database tests")

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not set; skipping QA database tests")

    parsed = urlparse(database_url.replace("postgresql+psycopg2", "postgresql", 1))

    if parsed.hostname != "127.0.0.1" or parsed.port != 55440:
        pytest.skip("DATABASE_URL is not the QA loopback instance; skipping")

    # Use a temporary database name for test isolation
    test_db_name = f"test_qa_{os.urandom(8).hex()}"

    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port,
        database="postgres",
        user=parsed.username,
        password=parsed.password,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {test_db_name}")
    finally:
        conn.close()

    test_url = (
        f"postgresql+psycopg2://{parsed.username}:{parsed.password}"
        f"@{parsed.hostname}:{parsed.port}/{test_db_name}"
    )

    yield test_url

    # Cleanup: drop the test database
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port,
        database="postgres",
        user=parsed.username,
        password=parsed.password,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {test_db_name}")
    finally:
        conn.close()


def test_ddl_provisioning_creates_four_tables(qa_database: str) -> None:
    """Verify that DDL provisioning creates trackflow_client, user_client_membership, incident, incident_history."""
    parsed = urlparse(qa_database.replace("postgresql+psycopg2", "postgresql", 1))

    # Run provision script
    env = os.environ.copy()
    env["TRACKFLOW_MCP_QA"] = "1"
    env["DATABASE_URL"] = qa_database

    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT),
            "--python",
            "3.12",
            str(QA_CODESPACES_DIR / "provision_qa_ddl.py"),
        ],
        env=env,
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"provision_qa_ddl.py failed:\n{result.stderr}"

    # Verify tables exist
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port,
        database=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
            tables = {row[0] for row in cursor.fetchall()}

        expected = {
            "trackflow_client",
            "user_client_membership",
            "incident",
            "incident_history",
        }
        assert expected.issubset(
            tables
        ), f"Expected tables missing: {expected - tables}"

    finally:
        conn.close()


def test_seed_fails_without_ddl(qa_database: str) -> None:
    """Verify that seed_trackflow.py fails if DDL is not applied first.

    This regression test ensures the fix stays in place: the launcher must
    apply DDL before seeding.
    """
    # Do NOT run provision_qa_ddl.py; the database should be empty
    env = os.environ.copy()
    env["TRACKFLOW_MCP_QA"] = "1"
    env["DATABASE_URL"] = qa_database

    with tempfile.TemporaryDirectory() as tmpdir:
        auth_db_path = Path(tmpdir) / "auth.json"
        env["TRACKFLOW_AUTH_DATABASE_PATH"] = str(auth_db_path)
        env["QA_TRACKFLOW_USER_ID"] = "test-user-id"
        env["QA_TRACKFLOW_LOCAL_PASSWORD"] = "TestPassword12345"

        result = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(REPOSITORY_ROOT),
                "--python",
                "3.12",
                str(REPOSITORY_ROOT / "mcps" / "trackflow" / "qa" / "seed_trackflow.py"),
            ],
            env=env,
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
        )

        # Seed should fail with "relation does not exist" error
        assert result.returncode != 0, "seed_trackflow.py should fail without DDL"
        assert (
            "does not exist" in result.stderr
            or "relation" in result.stderr.lower()
        ), f"Expected table-not-found error, got: {result.stderr}"


def test_full_integration_ddl_then_seed(qa_database: str) -> None:
    """Verify complete flow: apply DDL, then seed succeeds."""
    parsed = urlparse(qa_database.replace("postgresql+psycopg2", "postgresql", 1))

    # Step 1: Apply DDL
    env = os.environ.copy()
    env["TRACKFLOW_MCP_QA"] = "1"
    env["DATABASE_URL"] = qa_database

    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT),
            "--python",
            "3.12",
            str(QA_CODESPACES_DIR / "provision_qa_ddl.py"),
        ],
        env=env,
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"provision_qa_ddl.py failed:\n{result.stderr}"

    # Step 2: Run seed with full environment
    with tempfile.TemporaryDirectory() as tmpdir:
        auth_db_path = Path(tmpdir) / "auth.json"
        env["TRACKFLOW_AUTH_DATABASE_PATH"] = str(auth_db_path)
        env["QA_TRACKFLOW_USER_ID"] = "7f6e3d21-8c4a-4b92-a1d7-5e9f2c6b1048"
        env["QA_TRACKFLOW_LOCAL_PASSWORD"] = "TestPassword12345"

        result = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(REPOSITORY_ROOT),
                "--python",
                "3.12",
                str(REPOSITORY_ROOT / "mcps" / "trackflow" / "qa" / "seed_trackflow.py"),
            ],
            env=env,
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"seed_trackflow.py failed:\n{result.stderr}"

    # Step 3: Verify data was inserted
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port,
        database=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )
    try:
        with conn.cursor() as cursor:
            # Check clients were created
            cursor.execute("SELECT COUNT(*) FROM public.trackflow_client")
            client_count = cursor.fetchone()[0]
            assert (
                client_count >= 2
            ), f"Expected at least 2 clients, got {client_count}"

    finally:
        conn.close()
