"""F1 against PostgreSQL, with deterministic analyzers and a leased intake job."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import os
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId, RFPIntakeDecision, RFPMetadata
from data.pipelines.rfp_intake.graph import RFPGraphError, RFPIntakeGraph
from data.pipelines.rfp_intake.pdf_conversion import PDFConversionError, PDFConversionResult
from services.rfp_intake.intake_events import (
    IntakeClaimLost, get_intake_decision, record_intake_decision, rfp_intake_decisions,
    rfp_sse_cursor, rfp_sse_events,
)
from services.rfp_intake import tasks
from services.rfp_intake.config import RFPSettings
from services.rfp_intake.models import rfp_department_sections, rfp_handoffs, rfp_jobs, rfp_metadata, rfp_tickets
from services.rfp_intake.sink import PostgresRFPResultSink
from services.rfp_intake.store import accept_rfp, claim_job, finish_job
from services.rfp_intake.tasks import _resume_conversion
from tests.pipelines.test_rfp_intake import (
    MARKDOWN, ConcurrentAnalyzer, DiscardAnalyzer, conversion_metrics,
    readability_metrics, valid_metadata_draft,
)


@pytest.fixture
def sse_db():
    target = os.environ.get("TRACKFLOW_SSE_QA_DATABASE_URL")
    if target is None or os.environ.get("TRACKFLOW_SSE_QA_DDL_DML") != "YES":
        pytest.skip("dedicated TrackFlow SSE PostgreSQL DDL/DML was not enabled")
    url = make_url(target)
    assert url.drivername == "postgresql+psycopg2" and url.host == "127.0.0.1"
    assert url.database and url.database.startswith("trackflow_sse_qa_")
    assert url.username == "trackflow_sse_qa" and url.port and url.port >= 1024
    assert not url.query
    schema = f"rfp_sse_test_{uuid4().hex}"
    admin = create_engine(url, hide_parameters=True)
    db = create_engine(url, hide_parameters=True, connect_args={
        "options": f"-c search_path={schema} -c statement_timeout=12000",
    })
    script_dir = Path(__file__).resolve().parents[1] / "infra" / "sql"
    created = False
    try:
        with admin.begin() as conn:
            assert conn.execute(text("SELECT current_database(), current_user")).one() == (
                url.database, url.username,
            )
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        created = True
        with db.connect().execution_options(isolation_level="AUTOCOMMIT", no_parameters=True) as conn:
            assert conn.exec_driver_sql("SELECT current_schema()").scalar_one() == schema
            for script in ("rfp_intake.sql", "rfp_sse.sql", "rfp_sse.sql"):
                conn.exec_driver_sql((script_dir / script).read_text(encoding="utf-8"))
        yield db
    finally:
        db.dispose()
        if created:
            with admin.begin() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def _claim(db):
    ticket_id = uuid4()
    with Session(db) as session:
        job_id = accept_rfp(session, ticket_id=ticket_id, raw_pdf_path="qa/source.pdf",
                            uploaded_by_subject="sse-test")
        job = claim_job(session, job_id, lease_seconds=60)
        session.commit()
    assert job
    return job


def _decision(job):
    return RFPIntakeDecision(
        ticket_id=job["ticket_id"],
        markdown_sha256=sha256(MARKDOWN.encode("utf-8")).hexdigest(),
        classification=ConcurrentAnalyzer().classify(MARKDOWN),
        metadata=RFPMetadata(**valid_metadata_draft().model_dump(), currency="EUR",
                             departments_needed=[DepartmentId.warehouse, DepartmentId.reverse]),
        conversion_metrics=conversion_metrics(),
    )


def _run(db, job, analyzer, *, markdown=MARKDOWN, decision=None):
    with Session(db) as session:
        sink = PostgresRFPResultSink(session, job)
        return RFPIntakeGraph().execute(
            ticket_id=job["ticket_id"], markdown=markdown,
            conversion_metrics=conversion_metrics(), readability_metrics=readability_metrics(),
            analyzer=analyzer, sink=sink, intake_sink=sink, decision=decision,
        )


def _events(db):
    with Session(db) as session:
        return session.execute(select(rfp_sse_events).order_by(rfp_sse_events.c.cursor)).mappings().all()


def test_valid_rfp_commits_once_before_departments_and_keeps_terminal_writes(sse_db):
    job = _claim(sse_db)

    class ObserveBoundary(ConcurrentAnalyzer):
        def analyze_department(self, **kwargs):
            rows = _events(sse_db)
            assert len(rows) == 1 and rows[0]["ticket_id"] == job["ticket_id"]
            with Session(sse_db) as session:
                assert session.execute(select(rfp_tickets.c.status)).scalar_one() == "analizando"
                assert session.execute(select(rfp_metadata.c.ticket_id)).first() is None
            return super().analyze_department(**kwargs)

    assert _run(sse_db, job, ObserveBoundary()).outcome == "analysis_complete"
    row, = _events(sse_db)
    assert row["cursor"] == 1 and row["event_type"] == "rfp_ticket_created"
    with Session(sse_db) as session:
        assert session.execute(select(rfp_tickets.c.status)).scalar_one() == "analisis_completo"
        assert session.execute(select(rfp_jobs.c.status)).scalar_one() == "succeeded"
        assert session.execute(select(rfp_metadata.c.client_name)).scalar_one() == "ModaViva"
        assert len(session.execute(select(rfp_department_sections)).all()) == 2
        assert session.execute(select(rfp_handoffs.c.ticket_id)).scalar_one() == job["ticket_id"]
        created_at = session.execute(select(rfp_tickets.c.created_at)).scalar_one()
    assert row["payload"] == {
        "ticket_id": str(job["ticket_id"]), "rfp_id": "MV-2026", "client_name": "ModaViva",
        "client_country": "ES", "services_requested": ["warehouse", "reverse"],
        "status": "analyzing", "created_at": created_at.isoformat().replace("+00:00", "Z"),
    }


def test_discard_and_routing_failures_create_no_event(sse_db):
    job = _claim(sse_db)
    assert _run(sse_db, job, DiscardAnalyzer()).outcome == "discarded"

    class NotRequested(ConcurrentAnalyzer):
        def extract_metadata(self, _markdown):
            metadata = valid_metadata_draft()
            for service in metadata.services_requested:
                service.requested = False
            return metadata

    with pytest.raises(RFPGraphError, match="no supported"):
        _run(sse_db, _claim(sse_db), NotRequested())
    with pytest.raises(RFPGraphError, match="no relevant extract"):
        _run(sse_db, _claim(sse_db), ConcurrentAnalyzer(), markdown="generic\n" * 5)
    assert not _events(sse_db)


def test_rollback_releases_the_cursor_and_removes_both_rows(sse_db):
    job = _claim(sse_db)
    decision = _decision(job)
    with Session(sse_db) as session:
        record_intake_decision(session, job, decision)
        assert not _events(sse_db)
        session.rollback()
        assert get_intake_decision(session, job["ticket_id"]) is None
        assert session.execute(select(rfp_sse_cursor.c.last_cursor)).scalar_one() == 0
        PostgresRFPResultSink(session, job).persist_intake_decision(decision)
    assert [event["cursor"] for event in _events(sse_db)] == [1]


def _reclaim(db, job):
    with Session(db) as session:
        session.execute(update(rfp_jobs).where(rfp_jobs.c.job_id == job["job_id"])
                        .values(lease_until=func.clock_timestamp()))
        claimed = claim_job(session, job["job_id"], lease_seconds=60)
        session.commit()
    assert claimed
    return claimed


def test_commit_ack_loss_redelivery_resume_and_purged_event_remain_idempotent(sse_db, tmp_path):
    job = _claim(sse_db)
    decision = _decision(job)

    class LostAck(Session):
        def commit(self):
            super().commit()
            raise OperationalError("COMMIT", None, RuntimeError("synthetic acknowledgement loss"))

    with LostAck(sse_db) as session:
        PostgresRFPResultSink(session, job).persist_intake_decision(decision)
    assert len(_events(sse_db)) == 1
    claimed = _reclaim(sse_db, job)
    with Session(sse_db) as session:
        with pytest.raises(IntakeClaimLost):
            record_intake_decision(session, job, decision)
        session.rollback()
        stored = get_intake_decision(session, job["ticket_id"])
        assert stored == decision
        session.execute(delete(rfp_sse_events))
        session.execute(update(rfp_sse_cursor).values(purged_through=1))
        session.commit()
        PostgresRFPResultSink(session, claimed).persist_intake_decision(decision)
    markdown_path = tmp_path / str(job["ticket_id"]) / "source.md"
    markdown_path.parent.mkdir()
    markdown_path.write_bytes(MARKDOWN.encode("utf-8"))
    assert _resume_conversion(tmp_path, stored).markdown == MARKDOWN
    markdown_path.write_bytes((MARKDOWN + "changed").encode("utf-8"))
    with pytest.raises(PDFConversionError, match="differs"):
        _resume_conversion(tmp_path, stored)

    class NoIntakeCalls(ConcurrentAnalyzer):
        def classify(self, _markdown):
            pytest.fail("repeated classification")

        def extract_metadata(self, _markdown):
            pytest.fail("repeated extraction")

    assert _run(sse_db, claimed, NoIntakeCalls(), decision=stored).outcome == "analysis_complete"
    assert not _events(sse_db)


def _wait_for_counter_block(db, future, blocker):
    deadline = time.monotonic() + 5
    with db.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        while time.monotonic() < deadline:
            if future.done():
                future.result()
                pytest.fail("second writer completed without waiting for the first")
            if conn.execute(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                 "WHERE :blocker = ANY(pg_blocking_pids(pid)))"),
                            {"blocker": blocker}).scalar_one():
                return
            time.sleep(0.01)
    pytest.fail("second writer never waited on the ordered cursor")


@pytest.mark.parametrize("expire_second_claim", [False, True])
def test_concurrent_commits_and_lease_expiration_after_wait(sse_db, expire_second_claim):
    first = _claim(sse_db)
    second = _claim(sse_db)
    if expire_second_claim:
        with Session(sse_db) as session:
            session.execute(update(rfp_jobs).where(rfp_jobs.c.job_id == second["job_id"])
                            .values(lease_until=func.clock_timestamp() + text("interval '2 seconds'")))
            session.commit()

    def publish_second():
        with Session(sse_db) as session:
            record_intake_decision(session, second, _decision(second))
            session.commit()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(sse_db) as session:
            blocker = session.execute(select(func.pg_backend_pid())).scalar_one()
            record_intake_decision(session, first, _decision(first))
            pending = executor.submit(publish_second)
            _wait_for_counter_block(sse_db, pending, blocker)
            assert not _events(sse_db)
            if expire_second_claim:
                while session.execute(select(rfp_jobs.c.lease_until > func.clock_timestamp())
                                      .where(rfp_jobs.c.job_id == second["job_id"])).scalar_one():
                    time.sleep(0.01)
            session.commit()
        if expire_second_claim:
            with pytest.raises(IntakeClaimLost):
                pending.result(timeout=5)
        else:
            pending.result(timeout=5)
    assert [row["ticket_id"] for row in _events(sse_db)] == (
        [first["ticket_id"]] if expire_second_claim else [first["ticket_id"], second["ticket_id"]]
    )
    assert [row["cursor"] for row in _events(sse_db)] == (
        [1] if expire_second_claim else [1, 2]
    )


def test_failure_after_publication_does_not_remove_the_event(sse_db):
    job = _claim(sse_db)

    class BrokenDepartments(ConcurrentAnalyzer):
        def analyze_department(self, **_kwargs):
            raise RuntimeError("deterministic department failure")

    with pytest.raises(RuntimeError, match="department failure"):
        _run(sse_db, job, BrokenDepartments())
    with Session(sse_db) as session:
        assert finish_job(session, job=job, status="failed")
        session.commit()
        assert session.execute(select(rfp_tickets.c.status)).scalar_one() == "analizando"
        assert session.execute(select(rfp_jobs.c.status)).scalar_one() == "failed"
        assert session.execute(select(rfp_intake_decisions.c.ticket_id)).scalar_one() == job["ticket_id"]
    assert len(_events(sse_db)) == 1


def test_worker_redelivery_reads_committed_artifact_without_reconverting(sse_db, tmp_path, monkeypatch):
    job = _claim(sse_db)
    directory = tmp_path / str(job["ticket_id"])
    directory.mkdir()
    settings = RFPSettings(artifact_root=tmp_path, max_upload_bytes=1024)
    metrics = conversion_metrics().model_copy(update={"markdown_reference": f"rfp_intake/{job['ticket_id']}/source.md"})
    artifact = PDFConversionResult(MARKDOWN, metrics)
    calls = []

    def convert(*args, **kwargs):
        calls.append("convert")
        return artifact

    class FailOnce(ConcurrentAnalyzer):
        def classify(self, markdown):
            calls.append("classify")
            return super().classify(markdown)

        def extract_metadata(self, markdown):
            calls.append("extract")
            return super().extract_metadata(markdown)

        def analyze_department(self, **kwargs):
            raise RuntimeError("deterministic failure after commit")

    class Resume(ConcurrentAnalyzer):
        def classify(self, _markdown):
            pytest.fail("redelivery must not classify")

        def extract_metadata(self, _markdown):
            pytest.fail("redelivery must not extract")

    monkeypatch.setattr(tasks, "pdf_to_markdown", convert)
    monkeypatch.setattr(tasks, "load_settings", lambda: None)
    monkeypatch.setattr(tasks, "load_intake_fourgeeks_budget_settings", lambda **kwargs: None)
    monkeypatch.setattr(tasks, "compute_readability", lambda markdown: readability_metrics())
    monkeypatch.setattr(tasks, "FourGeeksHTTPClient", lambda _: SimpleNamespace(
        settings=SimpleNamespace(generation_timeout_seconds=1), close=lambda: None,
    ))
    analyzers = iter((FailOnce(), Resume()))
    monkeypatch.setattr(tasks, "FourGeeksRFPAnalyzer", lambda *args, **kwargs: next(analyzers))

    with pytest.raises(RuntimeError, match="after commit"):
        tasks.execute_claimed_job(sse_db, job, settings)
    assert len(_events(sse_db)) == 1
    claimed = _reclaim(sse_db, job)
    tasks.execute_claimed_job(sse_db, claimed, settings)
    assert calls == ["convert", "classify", "extract"]
    assert (directory / "source.md").read_bytes() == MARKDOWN.encode("utf-8")
    assert len(_events(sse_db)) == 1
    with Session(sse_db) as session:
        assert session.execute(select(rfp_tickets.c.status)).scalar_one() == "analisis_completo"
