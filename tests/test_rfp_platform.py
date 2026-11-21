from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy.exc import OperationalError
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from sqlmodel import SQLModel

from data.pipelines.rfp_intake.contracts import DepartmentId, RESPONSIBLE_BY_DEPARTMENT
from services.api.routes.rfp import router
from services.api.routes import rfp as rfp_route
from services.rfp_intake.artifacts import InvalidRFPArtifact, persist_markdown, persist_pdf
from services.rfp_intake.contracts import RFPJobPublic
from services.rfp_intake.config import get_rfp_settings
from services.rfp_intake.models import rfp_jobs, rfp_metadata_registry
from services.rfp_intake.tasks import process_rfp
from services.rfp_intake import tasks as rfp_tasks
from services.task_queue.celery_app import celery_app


def test_rfp_metadata_is_isolated_and_postgresql_specific() -> None:
    assert rfp_metadata_registry is not SQLModel.metadata
    ddl = str(CreateTable(rfp_jobs).compile(dialect=postgresql.dialect()))
    assert "UUID" in ddl
    assert "dead_letter" in ddl


def test_artifacts_are_durable_and_server_named(tmp_path: Path) -> None:
    ticket_id = uuid4()
    upload = UploadFile(filename="untrusted-name.pdf", file=BytesIO(b"%PDF-1.7\nbody"))
    relative, digest, size = asyncio.run(
        persist_pdf(upload, root=tmp_path, ticket_id=ticket_id, max_bytes=1024)
    )
    assert relative == f"rfp_intake/{ticket_id}/source.pdf"
    assert len(digest) == 64 and size == 13
    markdown = persist_markdown(root=tmp_path, ticket_id=ticket_id, markdown="# RFP\n")
    assert markdown == f"rfp_intake/{ticket_id}/source.md"
    assert (tmp_path / str(ticket_id) / "source.md").read_text(encoding="utf-8") == "# RFP\n"


def test_invalid_pdf_signature_is_rejected(tmp_path: Path) -> None:
    upload = UploadFile(filename="claim.pdf", file=BytesIO(b"not a pdf"))
    try:
        asyncio.run(persist_pdf(upload, root=tmp_path, ticket_id=uuid4(), max_bytes=1024))
    except InvalidRFPArtifact as exc:
        assert str(exc) == "invalid PDF signature"
    else:
        raise AssertionError("invalid upload was accepted")


def test_http_contract_hides_artifact_paths_and_exposes_attempt_limit() -> None:
    fields = set(RFPJobPublic.model_fields)
    assert fields == {"status", "attempt_count", "max_attempts", "last_error_code", "last_error_summary"}
    paths = {(route.path, tuple(sorted(route.methods or []))) for route in router.routes}
    assert ("/rfps", ("POST",)) in paths
    assert ("/rfps", ("GET",)) in paths
    assert ("/rfps/{ticket_id}", ("GET",)) in paths
    assert ("/rfps/{ticket_id}/response", ("POST",)) in paths
    assert ("/rfps/{ticket_id}/response", ("GET",)) in paths
    assert ("/rfps/{ticket_id}/response/{run_id}", ("GET",)) in paths


def test_list_rfps_serializes_accepted_and_discarded_tickets(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    accepted_ticket_id = uuid4()
    discarded_ticket_id = uuid4()
    rows = [
        {
            "ticket_id": accepted_ticket_id,
            "rfp_id": None,
            "status": "analizando",
            "client_name": None,
            "currency": None,
            "departments_needed": None,
            "job_status": "queued",
            "created_at": now,
            "updated_at": now,
        },
        {
            "ticket_id": discarded_ticket_id,
            "rfp_id": None,
            "status": "descartado",
            "client_name": None,
            "currency": None,
            "departments_needed": [],
            "job_status": "succeeded",
            "created_at": now,
            "updated_at": now,
        },
    ]
    monkeypatch.setattr(rfp_route, "list_rfps", lambda *_args, **_kwargs: (rows, 2))

    response = rfp_route.list_rfp_tickets(object(), object(), limit=25, offset=0)

    assert response.total == 2
    assert [item.ticket_id for item in response.items] == [accepted_ticket_id, discarded_ticket_id]
    assert [item.departments_needed for item in response.items] == [[], []]


def test_backoffice_proxies_same_origin_rfp_requests() -> None:
    config = (
        Path(__file__).parents[1] / "uis" / "backoffice" / "next.config.ts"
    ).read_text(encoding="utf-8")
    assert 'source: "/rfps/:path*"' in config
    assert 'destination: `${backendUrl}/rfps/:path*`' in config


def test_canonical_responsible_allowlist_and_task_registration() -> None:
    assert RESPONSIBLE_BY_DEPARTMENT[DepartmentId.warehouse] == "Ana Whitfield"
    assert process_rfp.name == "trackflow.process_rfp"
    assert celery_app.tasks["trackflow.process_rfp"].name == "trackflow.process_rfp"


def test_pdf_parser_bounds_are_configurable_and_validated(monkeypatch) -> None:
    monkeypatch.setenv("RFP_MAX_PDF_PAGES", "37")
    monkeypatch.setenv("RFP_PDF_CONVERSION_TIMEOUT_SECONDS", "12.5")
    settings = get_rfp_settings()
    assert settings.max_pdf_pages == 37
    assert settings.pdf_conversion_timeout_seconds == 12.5

    monkeypatch.setenv("RFP_PDF_CONVERSION_TIMEOUT_SECONDS", "inf")
    try:
        get_rfp_settings()
    except RuntimeError as exc:
        assert "finite positive" in str(exc)
    else:
        raise AssertionError("an unbounded parser timeout was accepted")


def test_explicit_ddl_contains_fencing_outbox_and_closed_statuses() -> None:
    ddl = (Path(__file__).parents[1] / "infra" / "sql" / "rfp_intake.sql").read_text(encoding="utf-8")
    for required in (
        "execution_epoch", "claim_token", "lease_until", "publish_failed",
        "uq_rfp_active_job_per_ticket", "analisis_completo", "max_attempts=3",
    ):
        assert required in ddl


def test_failure_recording_db_outage_requests_same_task_redelivery(monkeypatch, tmp_path: Path) -> None:
    class RedeliveryRequested(Exception):
        pass

    class FakeTask:
        def retry(self, *, exc, countdown):
            assert isinstance(exc, OperationalError)
            assert countdown == 600
            raise RedeliveryRequested

    outage = OperationalError("UPDATE rfp_jobs", {}, RuntimeError("offline"))
    monkeypatch.setattr(rfp_tasks, "_record_failure", lambda *_args, **_kwargs: (_ for _ in ()).throw(outage))
    monkeypatch.setattr(rfp_tasks, "_terminal_state", lambda *_args: "running")
    settings = rfp_tasks.RFPSettings(
        artifact_root=tmp_path, max_upload_bytes=1024, lease_seconds=600,
        heartbeat_seconds=60,
    )
    job = {"job_id": uuid4()}
    try:
        rfp_tasks._record_failure_or_redeliver(
            FakeTask(), object(), job, settings, retryable=True,
            code="structured_output_invalid", summary="invalid output",
        )
    except RedeliveryRequested:
        pass
    else:
        raise AssertionError("database outage did not request redelivery")


def test_upload_reconciles_uncertain_commit_before_cleanup(monkeypatch, tmp_path: Path) -> None:
    committed_ticket_ids = []
    cleanup_calls = []
    fresh_sessions = []

    async def fake_persist_pdf(_file, *, root, ticket_id, max_bytes):
        return f"rfp_intake/{ticket_id}/source.pdf", "0" * 64, 10

    class RequestSession:
        def commit(self):
            raise OperationalError("COMMIT", {}, RuntimeError("ack lost"))

        def rollback(self):
            return None

        def get_bind(self):
            return object()

    class ReconciliationSession:
        def __init__(self, *, bind):
            fresh_sessions.append(bind)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(rfp_route, "persist_pdf", fake_persist_pdf)
    monkeypatch.setattr(rfp_route, "accept_rfp", lambda _session, **kwargs: committed_ticket_ids.append(kwargs["ticket_id"]))
    monkeypatch.setattr(rfp_route, "Session", ReconciliationSession)
    monkeypatch.setattr(rfp_route, "ticket_exists", lambda _session, ticket_id: ticket_id in committed_ticket_ids)
    monkeypatch.setattr(rfp_route, "cleanup_artifact", lambda *args: cleanup_calls.append(args))
    monkeypatch.setattr(rfp_route, "get_rfp_settings", lambda: SimpleNamespace(
        artifact_root=tmp_path, max_upload_bytes=1024,
    ))

    result = asyncio.run(rfp_route.upload_rfp(
        UploadFile(filename="request.pdf", file=BytesIO(b"%PDF-test")),
        SimpleNamespace(id="admin-1"), RequestSession(),
    ))
    assert result.status == "analizando"
    assert result.ticket_id == committed_ticket_ids[0]
    assert fresh_sessions and cleanup_calls == []


def test_upload_preserves_artifact_when_commit_reconciliation_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    cleanup_calls = []

    async def fake_persist_pdf(_file, *, root, ticket_id, max_bytes):
        return f"rfp_intake/{ticket_id}/source.pdf", "0" * 64, 10

    class RequestSession:
        def commit(self):
            raise OperationalError("COMMIT", {}, RuntimeError("ack lost"))

        def rollback(self):
            return None

        def get_bind(self):
            return object()

    class UnavailableReconciliationSession:
        def __init__(self, *, bind):
            raise OperationalError("SELECT rfp_tickets", {}, RuntimeError("offline"))

    monkeypatch.setattr(rfp_route, "persist_pdf", fake_persist_pdf)
    monkeypatch.setattr(rfp_route, "accept_rfp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rfp_route, "Session", UnavailableReconciliationSession)
    monkeypatch.setattr(rfp_route, "cleanup_artifact", lambda *args: cleanup_calls.append(args))
    monkeypatch.setattr(rfp_route, "get_rfp_settings", lambda: SimpleNamespace(
        artifact_root=tmp_path, max_upload_bytes=1024,
    ))

    try:
        asyncio.run(rfp_route.upload_rfp(
            UploadFile(filename="request.pdf", file=BytesIO(b"%PDF-test")),
            SimpleNamespace(id="admin-1"), RequestSession(),
        ))
    except HTTPException as exc:
        assert exc.status_code == 503
    else:
        raise AssertionError("uncertain commit did not return service unavailable")
    assert cleanup_calls == []


def test_api_and_worker_images_share_non_root_rfp_volume_identity() -> None:
    root = Path(__file__).parents[1]
    api_dockerfile = (root / "services" / "Dockerfile").read_text(encoding="utf-8")
    worker_dockerfile = (root / "services" / "task_queue" / "Dockerfile").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    for dockerfile in (api_dockerfile, worker_dockerfile):
        assert "mkdir -p /app/services/api/data /app/data/raw/rfp_intake" in dockerfile
        assert "chown -R 10002:10002 /app/services/api/data /app/data/raw/rfp_intake" in dockerfile
        assert "USER 10002:10002" in dockerfile
    assert compose.count("rfp-raw:/app/data/raw/rfp_intake") == 2


def test_api_selects_installed_psycopg2_driver(monkeypatch) -> None:
    from services.api import database

    observed = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://qa:secret@localhost/trackflow_rfp_test")
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "create_engine", lambda url: observed.append(url) or object())
    database._get_engine()
    database._engine = None
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://qa:secret@localhost/trackflow_rfp_test")
    database._get_engine()
    assert observed == [
        "postgresql+psycopg2://qa:secret@localhost/trackflow_rfp_test",
        "postgresql+psycopg2://qa:secret@localhost/trackflow_rfp_test",
    ]
    database._engine = None
