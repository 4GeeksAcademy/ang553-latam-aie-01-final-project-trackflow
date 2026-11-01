from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).parents[1]
DOCKERFILE = ROOT / "infra/scheduler/Dockerfile"
CRONTAB = ROOT / "infra/scheduler/crontab"
COMPOSE = ROOT / "docker-compose.yml"
INFRA_README = ROOT / "infra/README.md"


def test_crontab_has_one_exact_utc_scheduled_job():
    lines = [
        line.strip()
        for line in CRONTAB.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines == ["15 0 * * * python -m scripts.nightly_export"]
    assert "@reboot" not in CRONTAB.read_text(encoding="utf-8").lower()


def test_scheduler_image_contains_the_complete_independent_python_runtime():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    normalized = " ".join(dockerfile.split())
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim AS runtime" in dockerfile
    assert "WORKDIR /app" in dockerfile
    assert "COPY pyproject.toml /app/pyproject.toml" in dockerfile
    assert "COPY uv.lock /app/uv.lock" in dockerfile
    assert "COPY pipelines/ /app/pipelines/" in dockerfile
    assert "source=services,target=/source-services,ro" in dockerfile
    assert "cp -a /source-services /app/services" in dockerfile
    assert "rm -rf /app/services/api/data" in dockerfile
    assert "COPY services/" not in dockerfile
    assert "COPY scripts/ /app/scripts/" in dockerfile
    assert "uv sync --frozen --no-dev" in normalized
    assert "python -m pip install" not in dockerfile
    assert 'PATH="/app/.venv/bin:${PATH}"' in dockerfile
    assert "PREFECT_SERVER_UI_ENABLED=false" in dockerfile
    assert "PREFECT_SERVER_UI_V2_ENABLED=false" in dockerfile
    assert "PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS=120" in dockerfile
    assert '"prefect>=' in pyproject
    assert "TZ=UTC" in dockerfile
    assert "uvicorn" not in dockerfile.lower()
    assert "services.api.main" not in dockerfile


def test_scheduler_runs_non_root_with_writable_export_directory():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "useradd --uid 10001" in dockerfile
    assert "chown -R trackflow:trackflow" in dockerfile
    assert "/app/data/raw" in dockerfile.split("chown -R trackflow:trackflow", 1)[1]
    assert "mkdir -p /app/data/raw /app/infra/scheduler /app/services/api/data" in dockerfile
    assert "/app/services/api/data" in dockerfile.split("chown -R trackflow:trackflow", 1)[1]
    assert "USER 10001:10001" in dockerfile
    assert "COPY --chown=trackflow:trackflow infra/scheduler/crontab" in dockerfile


def test_scheduler_cmd_only_starts_supercronic_and_has_no_healthcheck_job():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert 'CMD ["/usr/local/bin/supercronic", "/app/infra/scheduler/crontab"]' in dockerfile
    assert "HEALTHCHECK" not in dockerfile.upper()
    assert "ENTRYPOINT" not in dockerfile.upper()
    assert "scripts.nightly_export" not in dockerfile


def test_image_does_not_copy_or_embed_runtime_secrets():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    crontab = CRONTAB.read_text(encoding="utf-8")

    assert "COPY .env" not in dockerfile
    assert "rm -rf /app/services/api/data" in dockerfile
    assert "COPY services/" not in dockerfile
    assert "DATABASE_URL" not in dockerfile
    assert "JWT_SECRET" not in dockerfile
    assert "DATABASE_URL" not in crontab
    assert "password" not in crontab.lower()
    assert "token" not in crontab.lower()


def test_compose_scheduler_is_dedicated_persistent_and_api_independent():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    scheduler = compose["services"]["scheduler"]

    assert scheduler["build"] == {
        "context": ".",
        "dockerfile": "infra/scheduler/Dockerfile",
    }
    assert scheduler["profiles"] == ["scheduler"]
    assert scheduler["env_file"] == [".env"]
    assert scheduler["environment"] == ["TZ=UTC"]
    assert scheduler["volumes"] == ["telemetry-raw:/app/data/raw"]
    assert scheduler["networks"] == ["trackflow-dev"]
    assert "depends_on" not in scheduler
    assert "ports" not in scheduler
    assert "healthcheck" not in scheduler
    assert "command" not in scheduler
    assert "entrypoint" not in scheduler
    assert "telemetry-raw" in compose["volumes"]


def test_compose_does_not_reuse_api_image_or_api_bind_mounts():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    scheduler = compose["services"]["scheduler"]
    api = compose["services"]["api"]

    assert scheduler["build"] != api["build"]
    assert scheduler["volumes"] != api["volumes"]
    assert all(not volume.startswith("./") for volume in scheduler["volumes"])


def test_compose_profile_excludes_scheduler_by_default_and_enables_it_explicitly(
    tmp_path,
):
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is required for Compose profile verification")
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    for service in compose["services"].values():
        service.pop("env_file", None)
    isolated = tmp_path / "compose.yml"
    isolated.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    environment = {**os.environ, "API_BACKEND_URL": "http://api:8000"}

    normal = subprocess.run(
        ["docker", "compose", "-f", str(isolated), "config", "--services"],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    explicit = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(isolated),
            "--profile",
            "scheduler",
            "config",
            "--services",
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )

    assert "scheduler" not in normal.stdout.splitlines()
    assert "scheduler" in explicit.stdout.splitlines()


def test_manual_reconciliation_runbook_is_cas_and_commit_uncertainty_safe():
    runbook = INFRA_README.read_text(encoding="utf-8")

    assert "exact `job_run_id`" in runbook
    assert "Prefect descendants" in runbook
    assert "validate_existing_csv" in runbook
    assert "commit_uncertain" in runbook
    assert "WHERE id = '<job_run_id>'::uuid" in runbook
    assert "AND status = 'processing'" in runbook
    assert "exactly one returned row" in runbook
    assert "ROLLBACK;" in runbook and "COMMIT;" in runbook
    assert "same target date" in runbook
