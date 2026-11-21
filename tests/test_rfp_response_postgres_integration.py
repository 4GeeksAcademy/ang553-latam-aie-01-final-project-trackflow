"""Opt-in Parte 2 persistence test against a verified disposable PostgreSQL."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
from threading import Barrier
from uuid import UUID, uuid4
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.engine import make_url
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId, RESPONSIBLE_BY_DEPARTMENT
from data.pipelines.rfp_response.compliance import (
    build_global_safe_preview, build_safe_preview, evaluate_global_compliance,
    evaluate_section_compliance,
)
from data.pipelines.rfp_response.contracts import (
    DepartmentDraft, DepartmentHandoffResult, EvidenceReference,
    EvaluatorExecutionFailure, GlobalCommercialTermsDraft, GlobalCurrency, GlobalSLA,
    LLMInvocationRecord, RFPResponseHandoff, ReadabilityEvaluation, ReadabilityMetric,
    RelevanceEvaluation, SafetyInspection, SupportedClaim,
)
from data.pipelines.rfp_response.evaluators import (
    evaluate_department, evaluate_global, inspect_department_safety,
    inspect_global_safety,
)
from data.pipelines.rfp_response.generators import (
    generate_department_draft, generate_global_terms,
)
from data.pipelines.rfp_response.evaluation import (
    reduce_proposal_evaluations, reduce_section_evaluations,
)
from data.pipelines.rfp_response.graph import RFPResponseGraph
from services.rfp_intake.models import (
    rfp_department_sections, rfp_handoffs, rfp_jobs, rfp_metadata, rfp_outbox,
    rfp_tickets,
)
from services.rfp_intake.config import RFPSettings
from services.rfp_intake.store import claim_job
from services.rfp_response.models import (
    rfp_response_drafts, rfp_response_evaluations,
    rfp_response_execution_failures, rfp_response_handoffs,
    rfp_response_llm_invocations, rfp_response_previews, rfp_response_runs,
    rfp_response_sections,
)
from services.rfp_response.runtime import PostgresResponseRunRepository
from services.rfp_response.tasks import execute_claimed_response_job
from services.rfp_response.store import (
    ResponsePersistenceConflict, StaleResponseExecution, accept_response_generation,
    canonical_hash,
    claim_response_run, finalize_response_handoff, load_response_start,
    mark_response_technical_failure,
    persist_department_draft, persist_global_draft, persist_proposal_evaluation,
    persist_section_evaluation, record_evaluator_failure, start_response_run,
    get_response_generation, persist_llm_invocation,
)


ROOT = Path(__file__).parents[1]
URL_ENV = "TRACKFLOW_RFP_RESPONSE_TEST_DATABASE_URL"
CONTAINER_ENV = "TRACKFLOW_RFP_RESPONSE_TEST_CONTAINER"
NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
HASH = "b" * 64
HISTORICAL_TICKET_ID = UUID("11111111-1111-4111-8111-111111111111")
HISTORICAL_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
HISTORICAL_OUTBOX_ID = UUID("33333333-3333-4333-8333-333333333333")

pytestmark = pytest.mark.skipif(
    not os.getenv(URL_ENV) or not os.getenv(CONTAINER_ENV),
    reason="verified isolated Parte 2 PostgreSQL is required",
)


def _validated_engine():
    raw = os.environ[URL_ENV]
    url = make_url(raw)
    if url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost"}:
        pytest.fail("Parte 2 PostgreSQL must be PostgreSQL on loopback")
    if not (url.database or "").startswith("trackflow_rfp_response_test"):
        pytest.fail("Parte 2 PostgreSQL database name is not explicitly disposable")
    production = os.getenv("DATABASE_URL")
    if production and make_url(production).set(password=None) == url.set(password=None):
        pytest.fail("refusing to reuse DATABASE_URL")
    name = os.environ[CONTAINER_ENV]
    inspected = subprocess.run(["docker", "inspect", name], capture_output=True,
                               text=True, check=True, timeout=20)
    item = json.loads(inspected.stdout)[0]
    ports = item["NetworkSettings"]["Ports"]["5432/tcp"]
    tmpfs = item["HostConfig"].get("Tmpfs") or {}
    if not item["State"]["Running"] or item["State"].get("Health", {}).get("Status") != "healthy":
        pytest.fail("Parte 2 PostgreSQL container is not healthy")
    if not ports or any(port["HostIp"] not in {"127.0.0.1", "::1"} for port in ports):
        pytest.fail("Parte 2 PostgreSQL is not bound exclusively to loopback")
    if "/var/lib/postgresql/data" not in tmpfs:
        pytest.fail("Parte 2 PostgreSQL data directory is not tmpfs")
    return create_engine(raw)


@pytest.fixture(scope="module")
def engine():
    value = _validated_engine()
    raw = value.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute((ROOT / "tests/fixtures/rfp_response/rfp_intake_part1_baseline.sql").read_text(encoding="utf-8"))
            cursor.execute("""
                INSERT INTO rfp_tickets(ticket_id,status,raw_pdf_path,uploaded_by_subject)
                VALUES (%s,'analisis_completo','rfp_intake/historical/source.pdf','historical-qa');
                INSERT INTO rfp_jobs(job_id,ticket_id,status,attempt_count)
                VALUES (%s,%s,'succeeded',1);
                INSERT INTO rfp_outbox(outbox_id,job_id,attempt_no,status)
                VALUES (%s,%s,1,'published');
            """, (str(HISTORICAL_TICKET_ID), str(HISTORICAL_JOB_ID),
                  str(HISTORICAL_TICKET_ID), str(HISTORICAL_OUTBOX_ID),
                  str(HISTORICAL_JOB_ID)))
            cursor.execute((ROOT / "infra/sql/rfp_response_generation.sql").read_text(encoding="utf-8"))
            cursor.execute((ROOT / "infra/sql/rfp_response_generation.sql").read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    yield value
    value.dispose()


def _seed_intake(session: Session, ticket_id, departments: list[str]) -> None:
    session.execute(insert(rfp_tickets).values(ticket_id=ticket_id, status="analisis_completo",
        raw_pdf_path=f"rfp_intake/{ticket_id}/source.pdf", markdown_path=f"rfp_intake/{ticket_id}/source.md",
        uploaded_by_subject="isolated-qa"))
    source_ref = {"line_start": 1, "line_end": 2}
    services = [{"service_id": department, "requested": True,
                 "source_ref": source_ref} for department in departments]
    session.execute(insert(rfp_metadata).values(ticket_id=ticket_id,
        client_name="Isolated QA Client", client_country_code="US",
        client_country_original="United States", operation_country_codes=["US"],
        currency="USD", services_requested=services, monthly_volume_value=20,
        monthly_volume_unit="m3", monthly_volume_source_ref=source_ref,
        departments_needed=departments, readability_metrics={}, conversion_metrics={}))
    for department in departments:
        session.execute(insert(rfp_department_sections).values(ticket_id=ticket_id,
            department_id=department,
            key_aspects=[{"fact": f"Client requests {department} service in the United States.",
                          "source_ref": source_ref,
                          "requested_action": f"Provide a {department} response."}],
            open_questions=[], source_references=[source_ref], detected_conflicts=[]))
    references = [{"department_id": department,
                   "responsible": RESPONSIBLE_BY_DEPARTMENT[DepartmentId(department)],
                   "ticket_id": str(ticket_id)} for department in departments]
    payload = {"contract_version": "rfp-intake-v1", "ticket_id": str(ticket_id),
        "metadata_reference": str(ticket_id),
        "summary_for_sales": "Synthetic isolated QA handoff with grounded sections.",
        "selected_departments": departments,
        "department_section_references": references, "open_questions": [],
        "detected_conflicts": [], "created_at": NOW.isoformat()}
    session.execute(insert(rfp_handoffs).values(ticket_id=ticket_id,
        contract_version="rfp-intake-v1", summary_for_sales=payload["summary_for_sales"],
        payload=payload))


class Score:
    def __init__(self, score: float) -> None:
        self.score = score


class PassingEnglishReadability:
    def __init__(self, _text: str) -> None:
        pass

    def flesch(self) -> Score:
        return Score(70)

    def flesch_kincaid(self) -> Score:
        return Score(8)

    def gunning_fog(self) -> Score:
        return Score(10)


def test_forward_migration_preserves_part1_history_and_is_repeatable(engine) -> None:
    with Session(engine) as session:
        ticket = session.execute(select(rfp_tickets).where(
            rfp_tickets.c.ticket_id == HISTORICAL_TICKET_ID)).mappings().one()
        job = session.execute(select(rfp_jobs).where(
            rfp_jobs.c.job_id == HISTORICAL_JOB_ID)).mappings().one()
        outbox = session.execute(select(rfp_outbox).where(
            rfp_outbox.c.outbox_id == HISTORICAL_OUTBOX_ID)).mappings().one()
    assert ticket["status"] == "analisis_completo"
    assert job["status"] == "succeeded" and job["job_kind"] == "intake"
    assert outbox["status"] == "published"


def test_llm_invocation_metadata_is_fenced_content_free_and_idempotent(engine) -> None:
    ticket_id = uuid4()
    with Session(engine) as session:
        _seed_intake(session, ticket_id, ["warehouse"])
        run = start_response_run(session, ticket_id=ticket_id)
        session.commit()
    with Session(engine) as session:
        claimed = claim_response_run(session, run["run_id"], lease_seconds=60)
        assert claimed is not None
        session.commit()
    invocation = LLMInvocationRecord(
        invocation_id=uuid4(), ticket_id=ticket_id, run_id=run["run_id"],
        artifact_id=uuid4(), provider="fourgeeks_gateway", model="qa-model",
        stage="department_generation", department=DepartmentId.warehouse,
        call_index=1, provider_request_id="safe-request-id", latency_ms=12.5,
        estimated_input_tokens=120, prompt_tokens=100, completion_tokens=20,
        total_tokens=120, outcome="succeeded", error_category=None,
        started_at=NOW, finished_at=NOW,
    )
    rejected = LLMInvocationRecord(
        invocation_id=uuid4(), ticket_id=ticket_id, run_id=run["run_id"],
        artifact_id=uuid4(), provider="fourgeeks_gateway", model="qa-model",
        stage="department_generation", department=DepartmentId.lastmile,
        call_index=2, provider_request_id=None, latency_ms=8.0,
        estimated_input_tokens=90, prompt_tokens=70, completion_tokens=10,
        total_tokens=80, outcome="invalid_output",
        error_category="deterministic_validation",
        validation_path="pending_codes", validation_error_type="value_error",
        validation_received_type="array",
        violated_constraint="exact_pending_codes",
        started_at=NOW, finished_at=NOW,
    )
    with Session(engine) as session:
        persist_llm_invocation(
            session, invocation=invocation, token=claimed["claim_token"],
            epoch=claimed["execution_epoch"])
        persist_llm_invocation(
            session, invocation=rejected, token=claimed["claim_token"],
            epoch=claimed["execution_epoch"])
        session.commit()
    with Session(engine) as session:
        persist_llm_invocation(
            session, invocation=invocation, token=claimed["claim_token"],
            epoch=claimed["execution_epoch"])
        session.commit()
        rows = session.execute(select(rfp_response_llm_invocations).where(
            rfp_response_llm_invocations.c.run_id == run["run_id"]
        )).mappings().all()
    assert len(rows) == 2
    by_id = {row["invocation_id"]: row for row in rows}
    assert by_id[invocation.invocation_id]["total_tokens"] == 120
    rejected_row = by_id[rejected.invocation_id]
    assert rejected_row["validation_path"] == "pending_codes"
    assert rejected_row["validation_received_type"] == "array"
    assert rejected_row["violated_constraint"] == "exact_pending_codes"
    assert all("prompt" not in row and "response" not in row for row in rows)


def test_abbreviated_handoff_is_rejected_before_start(engine) -> None:
    ticket_id = uuid4()
    with Session(engine) as session:
        session.execute(insert(rfp_tickets).values(ticket_id=ticket_id,
            status="analisis_completo", raw_pdf_path="rfp_intake/invalid/source.pdf",
            uploaded_by_subject="isolated-qa"))
        session.execute(insert(rfp_handoffs).values(ticket_id=ticket_id,
            contract_version="rfp-intake-v1", summary_for_sales="Invalid abbreviated handoff",
            payload={"contract_version": "rfp-intake-v1", "ticket_id": str(ticket_id),
                     "selected_departments": ["warehouse"]}))
        with pytest.raises(ResponsePersistenceConflict, match="not rfp-intake-v1"):
            start_response_run(session, ticket_id=ticket_id)
        session.rollback()


def _evidence() -> EvidenceReference:
    return EvidenceReference(document_id="trackflow-storage", section_id="rates", evidence_hash=HASH)


def _readability() -> ReadabilityEvaluation:
    return ReadabilityEvaluation(execution_status="evaluated", language="en",
        readability_score=70, metrics=[ReadabilityMetric(name="flesch_reading_ease", value=70)],
        **{"pass": True}, not_applicable_reason=None)


def test_real_postgres_idempotency_fencing_cas_and_complete_handoff(engine) -> None:
    ticket_id = uuid4()
    with Session(engine) as session:
        _seed_intake(session, ticket_id, ["warehouse"])
        first = start_response_run(session, ticket_id=ticket_id)
        session.commit()
    with Session(engine) as session:
        repeated = start_response_run(session, ticket_id=ticket_id)
        session.commit()
    assert repeated["run_id"] == first["run_id"]

    with Session(engine) as session:
        claimed = claim_response_run(session, first["run_id"], lease_seconds=60)
        assert claimed is not None
        session.commit()

    run_id, token, epoch = claimed["run_id"], claimed["claim_token"], claimed["execution_epoch"]
    with Session(engine) as session:
        response_request = load_response_start(session, run_id=run_id)
    assert response_request.customer_managed_carrier is None
    draft = generate_department_draft(response_request,
        department=DepartmentId.warehouse, iteration_count=1, generated_at=NOW)
    preview = build_safe_preview(draft, inspect_department_safety(
        response_request, draft, inspected_at=NOW))
    section_result, feedback = evaluate_department(response_request, draft,
        evaluated_at=NOW, english_factory=PassingEnglishReadability)
    assert feedback == []
    assert section_result.draft_hash == canonical_hash(draft)
    global_draft = generate_global_terms(response_request,
        department_draft_ids=[draft.draft_id], iteration_count=1, generated_at=NOW)
    global_preview_value = build_global_safe_preview(global_draft,
        inspect_global_safety(response_request, global_draft, inspected_at=NOW))
    global_compliance, global_feedback = evaluate_global(response_request, global_draft,
        section_currencies=frozenset({"USD"}), consistency_issues=[], evaluated_at=NOW)
    assert global_feedback == []
    proposal = reduce_proposal_evaluations(ticket_id=ticket_id, run_id=run_id,
        section_results=[section_result], global_draft_id=global_draft.draft_id,
        global_iteration_count=1, global_compliance=global_compliance, evaluated_at=NOW)
    handoff = RFPResponseHandoff(ticket_id=ticket_id, run_id=run_id,
        source_handoff_hash=first["source_handoff_hash"], outcome="ready_for_part3",
        all_sections_passed=True, global_compliance_passed=True,
        selected_departments=[DepartmentId.warehouse],
        department_results=[DepartmentHandoffResult(department=DepartmentId.warehouse,
            terminal_status="passed", iteration_count=1, final_draft_id=draft.draft_id,
            evaluation_result_id=section_result.evaluation_id, safe_preview_id=preview.preview_id,
            blocks_commercial_completeness=False, requires_human_input=False, blocking_rule_ids=[])],
        global_draft_id=global_draft.draft_id,
        global_evaluation_result_id=proposal.evaluation_id, unresolved_items=[], created_at=NOW)

    with Session(engine) as session:
        with pytest.raises(StaleResponseExecution):
            persist_department_draft(session, draft=draft, preview=preview,
                token=uuid4(), epoch=epoch)
        session.rollback()

    with Session(engine) as session:
        persist_department_draft(session, draft=draft, preview=preview, token=token, epoch=epoch)
        persist_section_evaluation(session, result=section_result, token=token, epoch=epoch)
        persist_global_draft(session, draft=global_draft,
            preview=global_preview_value, token=token, epoch=epoch)
        persist_proposal_evaluation(session, result=proposal, token=token, epoch=epoch)
        finalize_response_handoff(session, handoff=handoff, token=token, epoch=epoch)
        session.commit()

    # Reconciliation after a hypothetical lost COMMIT acknowledgement must be a no-op,
    # even though the original fence has already been released.
    with Session(engine) as session:
        stale_token = uuid4()
        persist_department_draft(session, draft=draft, preview=preview,
            token=stale_token, epoch=999)
        persist_section_evaluation(session, result=section_result,
            token=stale_token, epoch=999)
        persist_global_draft(session, draft=global_draft, preview=global_preview_value,
            token=stale_token, epoch=999)
        persist_proposal_evaluation(session, result=proposal,
            token=stale_token, epoch=999)
        finalize_response_handoff(session, handoff=handoff,
            token=stale_token, epoch=999)
        session.commit()

    with Session(engine) as session:
        # Simulate a preview persisted before the public allowlist existed. The
        # HTTP projection must rebuild it from the validated internal draft.
        session.execute(update(rfp_response_previews).where(
            rfp_response_previews.c.draft_id == global_draft.draft_id).values(
                safe_content=global_draft.model_dump_json()))
        session.commit()
        ticket_status = session.execute(select(rfp_tickets.c.status).where(
            rfp_tickets.c.ticket_id == ticket_id)).scalar_one()
        stored = session.execute(select(rfp_response_handoffs.c.payload).where(
            rfp_response_handoffs.c.run_id == run_id)).scalar_one()
        assert ticket_status == "en_evaluación"
        assert stored["outcome"] == "ready_for_part3"
        assert session.execute(select(rfp_response_evaluations).where(
            rfp_response_evaluations.c.run_id == run_id)).mappings().all()
        public = get_response_generation(session, ticket_id=ticket_id, run_id=run_id)
    assert public is not None
    assert public["outcome"] == "ready_for_part3"
    assert public["departments"][0]["preview"]["gate_status"] == "passed"
    assert public["departments"][0]["preview"]["content"]
    public_global = json.loads(public["global_terms"]["preview"]["content"])
    assert set(public_global) == {
        "currency", "sla", "volume_discounts", "pending_commercial_items"
    }
    assert "draft_id" not in public["global_terms"]["preview"]["content"]
    assert "evidence_hash" not in public["global_terms"]["preview"]["content"]
    assert "generated_content" not in public["departments"][0]
    assert "payload" not in public["departments"][0]


@pytest.mark.parametrize(("carrier_model", "tri_state"), [
    ("unknown", None),
    ("trackflow_managed", False),
    ("customer_managed", True),
])
def test_carrier_model_snapshot_is_immutable_auditable_and_drives_execution(
        engine, carrier_model, tri_state) -> None:
    ticket_id = uuid4()
    with Session(engine) as session:
        _seed_intake(session, ticket_id, ["lastmile"])
        accepted = accept_response_generation(
            session, ticket_id=ticket_id, carrier_model=carrier_model,
            confirmation_actor="phase61-admin", confirmation_source="admin_response_start")
        session.commit()
    with Session(engine) as session:
        repeated = accept_response_generation(
            session, ticket_id=ticket_id, carrier_model=carrier_model,
            confirmation_actor="second-admin", confirmation_source="admin_response_start")
        session.commit()
        row = session.execute(select(rfp_response_runs).where(
            rfp_response_runs.c.run_id == accepted["run_id"])).mappings().one()
        request = load_response_start(session, run_id=accepted["run_id"])
    assert repeated == accepted
    assert row["carrier_model"] == carrier_model
    assert row["carrier_confirmation_actor"] == "phase61-admin"
    assert row["carrier_confirmation_source"] == "admin_response_start"
    assert row["carrier_confirmed_at"] is not None
    assert request.customer_managed_carrier is tri_state

    incompatible = "customer_managed" if carrier_model != "customer_managed" else "unknown"
    with Session(engine) as session:
        with pytest.raises(ResponsePersistenceConflict, match="different carrier model"):
            accept_response_generation(
                session, ticket_id=ticket_id, carrier_model=incompatible,
                confirmation_actor="phase61-admin",
                confirmation_source="admin_response_start")
        session.rollback()
    with Session(engine) as session:
        persisted = session.execute(select(rfp_response_runs).where(
            rfp_response_runs.c.run_id == accepted["run_id"])).mappings().one()
    assert persisted["carrier_model"] == carrier_model
    assert persisted["carrier_confirmation_actor"] == "phase61-admin"


def test_terminal_technical_failure_has_diagnostics_but_no_handoff_or_evaluation(engine) -> None:
    ticket_id = uuid4()
    with Session(engine) as session:
        _seed_intake(session, ticket_id, ["reverse"])
        run = start_response_run(session, ticket_id=ticket_id)
        session.commit()
    with Session(engine) as session:
        claimed = claim_response_run(session, run["run_id"], lease_seconds=60)
        assert claimed is not None
        failure = EvaluatorExecutionFailure(failure_id=uuid4(), ticket_id=ticket_id,
            run_id=run["run_id"], draft_id=None, evaluator="readability",
            infrastructure_attempts=3, error_code="dependency_unavailable",
            diagnostic_reference="isolated-qa/diagnostics/1", terminal=True, occurred_at=NOW)
        record_evaluator_failure(session, failure=failure,
            token=claimed["claim_token"], epoch=claimed["execution_epoch"])
        mark_response_technical_failure(session, run_id=run["run_id"],
            token=claimed["claim_token"], epoch=claimed["execution_epoch"])
        session.commit()
    with Session(engine) as session:
        record_evaluator_failure(session, failure=failure, token=uuid4(), epoch=999)
        session.commit()
    with Session(engine) as session:
        status = session.execute(select(rfp_response_runs.c.status).where(
            rfp_response_runs.c.run_id == run["run_id"])).scalar_one()
        assert status == "technical_failure"
        assert session.execute(select(rfp_response_execution_failures).where(
            rfp_response_execution_failures.c.run_id == run["run_id"])).mappings().one()
        assert session.execute(select(rfp_response_evaluations).where(
            rfp_response_evaluations.c.run_id == run["run_id"])).first() is None
        assert session.execute(select(rfp_response_handoffs).where(
            rfp_response_handoffs.c.run_id == run["run_id"])).first() is None


def test_real_postgres_concurrent_start_and_claim_have_single_winners(engine) -> None:
    ticket_id = uuid4()
    with Session(engine) as session:
        _seed_intake(session, ticket_id, ["warehouse", "reverse"])
        session.commit()

    start_barrier = Barrier(2)

    def start_once():
        with Session(engine) as session:
            start_barrier.wait(timeout=10)
            value = start_response_run(session, ticket_id=ticket_id)
            session.commit()
            return value

    with ThreadPoolExecutor(max_workers=2) as pool:
        starts = list(pool.map(lambda _item: start_once(), range(2)))
    assert starts[0]["run_id"] == starts[1]["run_id"]

    claim_barrier = Barrier(2)

    def claim_once():
        with Session(engine) as session:
            claim_barrier.wait(timeout=10)
            value = claim_response_run(session, starts[0]["run_id"], lease_seconds=60)
            session.commit()
            return value

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _item: claim_once(), range(2)))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0]["execution_epoch"] == 1


def test_concurrent_response_acceptance_creates_one_job_and_one_outbox(engine) -> None:
    ticket_id = uuid4()
    with Session(engine) as session:
        _seed_intake(session, ticket_id, ["warehouse"])
        session.commit()
    barrier = Barrier(2)

    def accept_once():
        with Session(engine) as session:
            barrier.wait(timeout=10)
            accepted = accept_response_generation(session, ticket_id=ticket_id)
            session.commit()
            return accepted

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(lambda _item: accept_once(), range(2)))
    assert accepted[0] == accepted[1]
    with Session(engine) as session:
        jobs = session.execute(select(rfp_jobs).where(
            rfp_jobs.c.ticket_id == ticket_id,
            rfp_jobs.c.job_kind == "response_generation")).mappings().all()
        outboxes = session.execute(select(rfp_outbox).where(
            rfp_outbox.c.job_id == accepted[0]["job_id"])).mappings().all()
    assert len(jobs) == len(outboxes) == 1


def test_langgraph_persists_complete_response_job_and_redelivery_is_idempotent(engine) -> None:
    ticket_id = uuid4()
    with Session(engine) as session:
        _seed_intake(session, ticket_id, ["warehouse"])
        accepted = accept_response_generation(session, ticket_id=ticket_id)
        session.commit()
    with Session(engine) as session:
        job = session.execute(select(rfp_jobs).where(
            rfp_jobs.c.job_id == accepted["job_id"])).mappings().one()
        outbox = session.execute(select(rfp_outbox).where(
            rfp_outbox.c.job_id == accepted["job_id"])).mappings().one()
        assert job["job_kind"] == "response_generation"
        assert outbox["status"] == "pending"
        claimed = claim_response_run(session, accepted["run_id"], lease_seconds=60)
        session.commit()
    assert claimed is not None
    with Session(engine) as session:
        item_request = load_response_start(session, run_id=accepted["run_id"])
    repository = PostgresResponseRunRepository(
        engine, run_id=accepted["run_id"], token=claimed["claim_token"],
        epoch=claimed["execution_epoch"])
    result = RFPResponseGraph().execute(
        request=item_request, repository=repository, now=lambda: NOW,
        readability_evaluator=lambda _draft: ReadabilityEvaluation(
            execution_status="evaluated", language="en", readability_score=70,
            metrics=[ReadabilityMetric(name="flesch_reading_ease", value=70),
                     ReadabilityMetric(name="flesch_kincaid_grade", value=8),
                     ReadabilityMetric(name="gunning_fog", value=10),
                     ReadabilityMetric(name="average_sentence_words", value=12)],
            **{"pass": True}))
    assert result.handoff is not None and result.handoff.outcome == "ready_for_part3"

    with Session(engine) as session:
        claimed_job = claim_job(session, accepted["job_id"], lease_seconds=60)
        session.commit()
    assert claimed_job is not None
    execute_claimed_response_job(engine, claimed_job,
        RFPSettings(artifact_root=ROOT / "unused", max_upload_bytes=1,
                    lease_seconds=60, heartbeat_seconds=10))
    with Session(engine) as session:
        assert claim_job(session, accepted["job_id"], lease_seconds=60) is None
        job_status = session.execute(select(rfp_jobs.c.status).where(
            rfp_jobs.c.job_id == accepted["job_id"])).scalar_one()
    assert job_status == "succeeded"

    with Session(engine) as session:
        assert claim_response_run(session, accepted["run_id"], lease_seconds=60) is None
        counts = {
            "drafts": len(session.execute(select(rfp_response_drafts).where(
                rfp_response_drafts.c.run_id == accepted["run_id"])).all()),
            "evaluations": len(session.execute(select(rfp_response_evaluations).where(
                rfp_response_evaluations.c.run_id == accepted["run_id"])).all()),
            "handoffs": len(session.execute(select(rfp_response_handoffs).where(
                rfp_response_handoffs.c.run_id == accepted["run_id"])).all()),
        }
        session.rollback()
    assert counts == {"drafts": 2, "evaluations": 2, "handoffs": 1}


def test_langgraph_recovers_persisted_unevaluated_draft_without_increment(engine) -> None:
    ticket_id = uuid4()
    with Session(engine) as session:
        _seed_intake(session, ticket_id, ["warehouse"])
        run = start_response_run(session, ticket_id=ticket_id)
        session.commit()
    with Session(engine) as session:
        claimed = claim_response_run(session, run["run_id"], lease_seconds=60)
        session.commit()
    assert claimed is not None
    with Session(engine) as session:
        item_request = load_response_start(session, run_id=run["run_id"])
    draft = generate_department_draft(
        item_request, department=DepartmentId.warehouse,
        iteration_count=1, generated_at=NOW)
    preview = build_safe_preview(
        draft, inspect_department_safety(item_request, draft, inspected_at=NOW))
    with Session(engine) as session:
        persist_department_draft(session, draft=draft, preview=preview,
            token=claimed["claim_token"], epoch=claimed["execution_epoch"])
        session.commit()

    repository = PostgresResponseRunRepository(
        engine, run_id=run["run_id"], token=claimed["claim_token"],
        epoch=claimed["execution_epoch"])
    result = RFPResponseGraph().execute(
        request=item_request, repository=repository, now=lambda: NOW,
        department_generator=lambda *_args: (_ for _ in ()).throw(
            AssertionError("recovery generated an unnecessary department draft")),
        readability_evaluator=lambda _draft: ReadabilityEvaluation(
            execution_status="evaluated", language="en", readability_score=70,
            metrics=[ReadabilityMetric(name="flesch_reading_ease", value=70),
                     ReadabilityMetric(name="flesch_kincaid_grade", value=8),
                     ReadabilityMetric(name="gunning_fog", value=10),
                     ReadabilityMetric(name="average_sentence_words", value=12)],
            **{"pass": True}))
    assert result.handoff is not None
    with Session(engine) as session:
        section = session.execute(select(rfp_response_sections).where(
            rfp_response_sections.c.run_id == run["run_id"])).mappings().one()
        previews = session.execute(select(rfp_response_previews).join(
            rfp_response_drafts,
            rfp_response_previews.c.draft_id == rfp_response_drafts.c.draft_id).where(
                rfp_response_drafts.c.run_id == run["run_id"])).all()
    assert section["iteration_count"] == 1
    assert len(previews) == 2


def test_forward_migration_preserves_pre_snapshot_response_run(engine) -> None:
    """A run created by the prior Parte 2 schema survives the additive upgrade."""
    ticket_id = uuid4()
    with Session(engine) as session:
        _seed_intake(session, ticket_id, ["warehouse"])
        run = start_response_run(session, ticket_id=ticket_id)
        session.commit()

    raw = engine.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute("""
                ALTER TABLE rfp_response_runs
                  DROP CONSTRAINT IF EXISTS ck_rfp_response_carrier_model;
                ALTER TABLE rfp_response_runs DROP COLUMN carrier_confirmed_at;
                ALTER TABLE rfp_response_runs DROP COLUMN carrier_confirmation_source;
                ALTER TABLE rfp_response_runs DROP COLUMN carrier_confirmation_actor;
                ALTER TABLE rfp_response_runs DROP COLUMN carrier_model;
            """)
            migration = (ROOT / "infra/sql/rfp_response_generation.sql").read_text(
                encoding="utf-8")
            cursor.execute(migration)
            cursor.execute(migration)
            cursor.execute("""
                SELECT run_id,carrier_model,carrier_confirmation_actor,
                       carrier_confirmation_source,carrier_confirmed_at
                FROM rfp_response_runs WHERE run_id=%s
            """, (str(run["run_id"]),))
            preserved = cursor.fetchone()
        raw.commit()
    finally:
        raw.close()
    assert preserved == (
        run["run_id"], "unknown", None, "legacy_migration", None,
    )
