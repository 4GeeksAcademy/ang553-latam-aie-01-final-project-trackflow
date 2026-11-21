"""Transactional, idempotent and fenced PostgreSQL store for Parte 2."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

from data.pipelines.rfp_response.contracts import (
    DepartmentDraft, EvaluatorExecutionFailure, GlobalCommercialTermsDraft,
    LLMInvocationRecord,
    ProposalEvaluationResult, RequestedAspect, RFPResponseHandoff, RFPResponseStart,
    SafePreview, SectionEvaluationResult,
)
from data.pipelines.rfp_response.compliance import serialize_global_public_preview
from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from data.pipelines.rfp_intake.contracts import (
    BudgetRange, DepartmentId, DepartmentSection, RFPHandoff, ServiceRequest,
    SourceReference,
)
from data.pipelines.rfp_intake.contracts import RESPONSIBLE_BY_DEPARTMENT
from services.rfp_intake.models import (
    rfp_department_sections, rfp_handoffs, rfp_jobs, rfp_metadata, rfp_outbox,
    rfp_tickets,
)
from services.rfp_response.models import (
    rfp_response_drafts, rfp_response_evaluations, rfp_response_evaluator_results,
    rfp_response_execution_failures, rfp_response_handoffs,
    rfp_response_llm_invocations, rfp_response_previews,
    rfp_response_runs, rfp_response_sections,
)


class StaleResponseExecution(RuntimeError):
    pass


class ResponsePersistenceConflict(RuntimeError):
    pass


def _dump(value: Any) -> Any:
    return canonical_value(value)


def _validated_intake_context(session: Session, *, ticket_id: UUID,
                              handoff_row: dict[str, Any]) -> tuple[RFPHandoff, dict[str, Any], list[DepartmentSection]]:
    try:
        handoff = RFPHandoff.model_validate(handoff_row["payload"], strict=False)
    except Exception as exc:
        raise ResponsePersistenceConflict("intake handoff payload is not rfp-intake-v1") from exc
    if (handoff_row["contract_version"] != handoff.contract_version
            or handoff_row["summary_for_sales"] != handoff.summary_for_sales
            or handoff.ticket_id != ticket_id):
        raise ResponsePersistenceConflict("intake handoff columns and payload disagree")
    metadata_row = session.execute(select(rfp_metadata).where(
        rfp_metadata.c.ticket_id == ticket_id)).mappings().one_or_none()
    section_rows = session.execute(select(rfp_department_sections).where(
        rfp_department_sections.c.ticket_id == ticket_id)).mappings().all()
    if metadata_row is None:
        raise ResponsePersistenceConflict("intake metadata is required")
    try:
        metadata = {key: metadata_row[key] for key in (
            "client_name", "client_country_code", "client_country_original",
            "operation_country_codes", "services_requested", "monthly_volume_value",
            "monthly_volume_unit", "monthly_volume_source_ref", "deadline",
            "onboarding_target", "budget_range", "currency", "departments_needed",
        )}
        if metadata["client_country_code"] not in {None, "US", "ES"}:
            raise ValueError("invalid client country")
        countries = metadata["operation_country_codes"]
        if (not isinstance(countries, list) or len(countries) != len(set(countries))
                or any(item not in {"US", "ES"} for item in countries)):
            raise ValueError("invalid operation countries")
        if metadata["currency"] not in {None, "USD", "EUR"}:
            raise ValueError("invalid currency")
        services = [ServiceRequest.model_validate(item, strict=False)
                    for item in metadata["services_requested"]]
        departments = [DepartmentId(item) for item in metadata["departments_needed"]]
        if not services or len({item.service_id for item in services}) != len(services):
            raise ValueError("invalid service requests")
        if departments != [item.service_id for item in services if item.requested]:
            raise ValueError("requested services and departments disagree")
        monthly_values = (metadata["monthly_volume_value"], metadata["monthly_volume_unit"],
                          metadata["monthly_volume_source_ref"])
        if any(item is None for item in monthly_values) != all(item is None for item in monthly_values):
            raise ValueError("monthly volume fields are incomplete")
        if metadata["monthly_volume_source_ref"] is not None:
            SourceReference.model_validate(metadata["monthly_volume_source_ref"], strict=False)
            metadata["monthly_volume_value"] = float(metadata["monthly_volume_value"])
        if metadata["budget_range"] is not None:
            BudgetRange.model_validate(metadata["budget_range"], strict=False)
        raw_sections = [DepartmentSection.model_validate({
            "department_id": row["department_id"], "key_aspects": row["key_aspects"],
            "open_questions": row["open_questions"],
            "source_references": row["source_references"],
            "detected_conflicts": row["detected_conflicts"],
        }, strict=False) for row in section_rows]
    except Exception as exc:
        raise ResponsePersistenceConflict("intake metadata or sections are invalid") from exc
    selected = handoff.selected_departments
    section_map = {item.department_id: item for item in raw_sections}
    sections = [section_map[item] for item in selected if item in section_map]
    if departments != selected or [item.department_id for item in sections] != selected:
        raise ResponsePersistenceConflict("intake handoff, metadata and sections disagree")
    for section in sections:
        declared = {(ref.line_start, ref.line_end) for ref in section.source_references}
        if any((aspect.source_ref.line_start, aspect.source_ref.line_end) not in declared
               for aspect in section.key_aspects):
            raise ResponsePersistenceConflict("intake key aspect lacks a declared source reference")
    return handoff, metadata, sections


_CARRIER_MODELS = {"unknown", "trackflow_managed", "customer_managed"}


def start_response_run(session: Session, *, ticket_id: UUID,
                       carrier_model: str = "unknown",
                       confirmation_actor: str | None = None,
                       confirmation_source: str | None = None) -> dict[str, Any]:
    """Idempotently create a run and its selected sections in the caller transaction."""
    if carrier_model not in _CARRIER_MODELS:
        raise ResponsePersistenceConflict("unsupported carrier model")
    if confirmation_actor is not None and not confirmation_actor.strip():
        raise ResponsePersistenceConflict("carrier confirmation actor is invalid")
    source = confirmation_source or (
        "admin_response_start" if confirmation_actor is not None
        else "legacy_compatible_start"
    )
    if not source.strip() or len(source) > 64:
        raise ResponsePersistenceConflict("carrier confirmation source is invalid")
    ticket = session.execute(select(rfp_tickets.c.status).where(
        rfp_tickets.c.ticket_id == ticket_id)).scalar_one_or_none()
    handoff = session.execute(select(
        rfp_handoffs.c.contract_version, rfp_handoffs.c.summary_for_sales,
        rfp_handoffs.c.payload).where(
        rfp_handoffs.c.ticket_id == ticket_id)).mappings().first()
    if ticket is None or handoff is None:
        raise ResponsePersistenceConflict("completed intake handoff is required")
    if ticket not in {"analisis_completo", "generando_borrador", "en_evaluación", "needs_human_review"}:
        raise ResponsePersistenceConflict("ticket is not eligible for response generation")
    validated_handoff, _metadata, _sections = _validated_intake_context(
        session, ticket_id=ticket_id, handoff_row=dict(handoff))
    payload = handoff["payload"]
    departments = [item.value for item in validated_handoff.selected_departments]
    source_hash = canonical_hash(payload)
    run_id = uuid4()
    created = session.execute(pg_insert(rfp_response_runs).values(
        run_id=run_id, ticket_id=ticket_id, source_handoff_version=handoff["contract_version"],
        source_handoff_hash=source_hash, status="pending", carrier_model=carrier_model,
        carrier_confirmation_actor=confirmation_actor,
        carrier_confirmation_source=source,
        carrier_confirmed_at=(datetime.now(timezone.utc)
                              if confirmation_actor is not None else None),
    ).on_conflict_do_nothing(
        index_elements=[rfp_response_runs.c.ticket_id, rfp_response_runs.c.source_handoff_hash]
    ).returning(rfp_response_runs.c.run_id)).scalar_one_or_none()
    if created is not None:
        for department in departments:
            session.execute(insert(rfp_response_sections).values(
                run_id=created, department_id=department, status="pending"))
        changed = session.execute(update(rfp_tickets).where(
            rfp_tickets.c.ticket_id == ticket_id,
            rfp_tickets.c.status == "analisis_completo",
        ).values(status="generando_borrador")).rowcount
        if changed != 1:
            raise StaleResponseExecution("ticket transition lost its compare-and-set")
    row = session.execute(select(rfp_response_runs).where(
        rfp_response_runs.c.ticket_id == ticket_id,
        rfp_response_runs.c.source_handoff_hash == source_hash,
    )).mappings().one()
    if row["carrier_model"] != carrier_model:
        raise ResponsePersistenceConflict(
            "response run already exists with a different carrier model")
    return dict(row)


def accept_response_generation(session: Session, *, ticket_id: UUID,
                               carrier_model: str = "unknown",
                               confirmation_actor: str | None = None,
                               confirmation_source: str | None = None) -> dict[str, Any]:
    """Create the durable response run, response job and outbox exactly once."""
    run = start_response_run(
        session, ticket_id=ticket_id, carrier_model=carrier_model,
        confirmation_actor=confirmation_actor,
        confirmation_source=confirmation_source,
    )
    session.execute(select(rfp_response_runs.c.run_id).where(
        rfp_response_runs.c.run_id == run["run_id"]).with_for_update()).scalar_one()
    existing = session.execute(select(rfp_jobs).where(
        rfp_jobs.c.ticket_id == ticket_id,
        rfp_jobs.c.job_kind == "response_generation",
    ).order_by(rfp_jobs.c.created_at.desc()).limit(1)).mappings().one_or_none()
    if existing is not None:
        return {"run_id": run["run_id"], "job_id": existing["job_id"]}
    job_id = uuid4()
    session.execute(insert(rfp_jobs).values(
        job_id=job_id, ticket_id=ticket_id, job_kind="response_generation",
        status="queued"))
    session.execute(insert(rfp_outbox).values(
        outbox_id=uuid4(), job_id=job_id, attempt_no=1, status="pending"))
    return {"run_id": run["run_id"], "job_id": job_id}


def _public_pending(payload: Any) -> list[dict[str, str]]:
    return [{"code": item.code, "description": item.description, "owner": item.owner}
            for item in payload.pending_commercial_items]


def _public_preview(row: Any, *, verified_content: str | None = None) -> dict[str, Any] | None:
    if row is None:
        return None
    status = row["gate_status"]
    content = (verified_content if verified_content is not None else row["safe_content"])
    content = content if status == "passed" else None
    preview = SafePreview(
        preview_id=row["preview_id"], draft_id=row["draft_id"],
        gate_status=status, content=content, reason_codes=row["reason_codes"],
    )
    return {"gate_status": preview.gate_status, "content": preview.content,
            "reason_codes": preview.reason_codes}


def _public_evaluation(row: Any, *, expected_draft_id: UUID,
                       global_scope: bool = False) -> dict[str, Any] | None:
    if row is None:
        return None
    if canonical_hash(row["payload"]) != row["payload_hash"]:
        raise ResponsePersistenceConflict("persisted evaluation hash is invalid")
    if global_scope:
        result = ProposalEvaluationResult.model_validate(row["payload"], strict=False)
        feedback = [finding.message for finding in result.global_compliance.findings
                    if finding.blocking]
        missing: list[str] = []
        contradicted: list[str] = []
    else:
        result = SectionEvaluationResult.model_validate(row["payload"], strict=False)
        feedback = result.actionable_feedback
        missing = result.missing_aspect_ids
        contradicted = result.contradicted_aspect_ids
    result_draft_id = result.global_draft_id if global_scope else result.draft_id
    if (result.evaluation_id != row["evaluation_id"]
            or result_draft_id != expected_draft_id
            or row["draft_id"] != expected_draft_id
            or result.overall_pass != row["overall_pass"]):
        raise ResponsePersistenceConflict("persisted evaluation columns and payload disagree")
    return {
        "evaluation_id": result.evaluation_id,
        "overall_pass": result.overall_pass,
        "terminal_status": result.terminal_status,
        "actionable_feedback": feedback,
        "missing_aspect_ids": missing,
        "contradicted_aspect_ids": contradicted,
        "blocks_commercial_completeness": result.blocks_commercial_completeness,
        "requires_human_input": result.requires_human_input,
        "blocking_rule_ids": result.blocking_rule_ids,
    }


def _global_evaluation_status(*, run_status: str, global_draft_present: bool,
                              compliance_pass: bool | None) -> str:
    """Derive presentation state without changing the persisted business outcome."""
    if compliance_pass is True:
        return "passed"
    if compliance_pass is False:
        return "failed"
    if run_status == "technical_failure":
        return "technical_failure"
    return "en_evaluación" if global_draft_present else "not_started"


def get_response_generation(session: Session, *, ticket_id: UUID,
                            run_id: UUID | None = None) -> dict[str, Any] | None:
    """Return a validated, fail-closed projection; never return internal drafts."""
    query = select(rfp_response_runs).where(rfp_response_runs.c.ticket_id == ticket_id)
    if run_id is not None:
        query = query.where(rfp_response_runs.c.run_id == run_id)
    run = session.execute(query.order_by(
        rfp_response_runs.c.created_at.desc()).limit(1)).mappings().one_or_none()
    if run is None:
        return None
    run = dict(run)
    sections = session.execute(select(rfp_response_sections).where(
        rfp_response_sections.c.run_id == run["run_id"]
    ).order_by(rfp_response_sections.c.department_id)).mappings().all()
    drafts = session.execute(select(rfp_response_drafts).where(
        rfp_response_drafts.c.run_id == run["run_id"]
    ).order_by(rfp_response_drafts.c.iteration_count.desc())).mappings().all()
    latest_drafts: dict[str, Any] = {}
    for draft in drafts:
        latest_drafts.setdefault(draft["scope_key"], draft)
    evaluations = session.execute(select(rfp_response_evaluations).where(
        rfp_response_evaluations.c.run_id == run["run_id"])).mappings().all()
    evaluations_by_draft = {row["draft_id"]: row for row in evaluations}
    draft_ids = [row["draft_id"] for row in latest_drafts.values()]
    previews = {} if not draft_ids else {
        row["draft_id"]: row for row in session.execute(select(rfp_response_previews).where(
            rfp_response_previews.c.draft_id.in_(draft_ids))).mappings().all()
    }

    public_sections = []
    for section in sections:
        scope = section["department_id"]
        draft_row = latest_drafts.get(scope)
        draft = None
        evaluation = None
        preview = None
        pending: list[dict[str, str]] = []
        if draft_row is not None:
            if canonical_hash(draft_row["payload"]) != draft_row["payload_hash"]:
                raise ResponsePersistenceConflict("persisted department draft hash is invalid")
            draft = DepartmentDraft.model_validate(draft_row["payload"], strict=False)
            if (draft.draft_id != draft_row["draft_id"] or draft.department.value != scope
                    or draft.run_id != run["run_id"] or draft.ticket_id != ticket_id
                    or draft.iteration_count != draft_row["iteration_count"]):
                raise ResponsePersistenceConflict("persisted department draft identity is invalid")
            pending = _public_pending(draft)
            preview = _public_preview(previews.get(draft.draft_id))
            evaluation = _public_evaluation(evaluations_by_draft.get(draft.draft_id),
                                            expected_draft_id=draft.draft_id)
        public_sections.append({
            "department": scope,
            "responsible": RESPONSIBLE_BY_DEPARTMENT[DepartmentId(scope)],
            "status": section["status"],
            "iteration_count": section["iteration_count"],
            "preview": preview,
            "evaluation": evaluation,
            "pending_commercial_items": pending,
        })

    global_row = latest_drafts.get("proposal")
    global_preview = None
    global_evaluation = None
    global_compliance_pass = None
    global_pending: list[dict[str, str]] = []
    if global_row is not None:
        if canonical_hash(global_row["payload"]) != global_row["payload_hash"]:
            raise ResponsePersistenceConflict("persisted global draft hash is invalid")
        global_draft = GlobalCommercialTermsDraft.model_validate(
            global_row["payload"], strict=False)
        if (global_draft.draft_id != global_row["draft_id"]
                or global_draft.run_id != run["run_id"]
                or global_draft.ticket_id != ticket_id
                or global_draft.global_iteration_count != global_row["iteration_count"]):
            raise ResponsePersistenceConflict("persisted global draft identity is invalid")
        global_pending = _public_pending(global_draft)
        global_preview = _public_preview(
            previews.get(global_draft.draft_id),
            verified_content=serialize_global_public_preview(global_draft),
        )
        global_evaluation_row = evaluations_by_draft.get(global_draft.draft_id)
        global_evaluation = _public_evaluation(
            global_evaluation_row,
            expected_draft_id=global_draft.draft_id, global_scope=True)
        if global_evaluation_row is not None:
            persisted_global_evaluation = ProposalEvaluationResult.model_validate(
                global_evaluation_row["payload"], strict=False)
            global_compliance_pass = persisted_global_evaluation.global_compliance.passed

    job = session.execute(select(rfp_jobs).where(
        rfp_jobs.c.ticket_id == ticket_id,
        rfp_jobs.c.job_kind == "response_generation",
    ).order_by(rfp_jobs.c.created_at.desc()).limit(1)).mappings().one_or_none()
    failures = session.execute(select(rfp_response_execution_failures).where(
        rfp_response_execution_failures.c.run_id == run["run_id"]
    ).order_by(rfp_response_execution_failures.c.occurred_at)).mappings().all()
    handoff_row = session.execute(select(rfp_response_handoffs).where(
        rfp_response_handoffs.c.run_id == run["run_id"])).mappings().one_or_none()
    handoff = None
    if handoff_row is not None:
        if canonical_hash(handoff_row["payload"]) != handoff_row["payload_hash"]:
            raise ResponsePersistenceConflict("persisted response handoff hash is invalid")
        handoff_value = RFPResponseHandoff.model_validate(handoff_row["payload"], strict=False)
        if (handoff_value.run_id != run["run_id"] or handoff_value.ticket_id != ticket_id
                or handoff_value.outcome != handoff_row["outcome"]):
            raise ResponsePersistenceConflict("persisted response handoff identity is invalid")
        handoff = handoff_value.outcome
    if run["status"] in {"ready_for_part3", "needs_human_review"} and handoff != run["status"]:
        raise ResponsePersistenceConflict("terminal response run lacks its durable handoff")
    outcome = handoff if handoff in {"ready_for_part3", "needs_human_review"} else None
    global_compliance_status = _global_evaluation_status(
        run_status=run["status"], global_draft_present=global_row is not None,
        compliance_pass=global_compliance_pass)
    return {
        "ticket_id": ticket_id,
        "run_id": run["run_id"],
        "carrier_model": run["carrier_model"],
        "status": run["status"],
        "outcome": outcome,
        "technical_failure": run["status"] == "technical_failure",
        "job": None if job is None else {
            **{key: job[key] for key in (
                "job_id", "status", "attempt_count", "max_attempts", "last_error_code")},
            "last_error_summary": (
                "Response processing failed; an administrator must inspect the worker logs."
                if job["status"] in {"failed", "dead_letter"} else None
            ),
        },
        "departments": public_sections,
        "global_terms": {
            "status": global_compliance_status,
            "compliance_status": global_compliance_status,
            "compliance_pass": global_compliance_pass,
            "evaluation_executed": global_evaluation is not None,
            "iteration_count": run["global_iteration_count"],
            "preview": global_preview,
            "evaluation": global_evaluation,
            "pending_commercial_items": global_pending,
        },
        "operational_failures": [{key: failure[key] for key in (
            "evaluator", "infrastructure_attempts", "error_code", "terminal",
            "occurred_at")} for failure in failures],
        "updated_at": run["updated_at"],
    }


def load_response_start(session: Session, *, run_id: UUID,
                        language: str = "en") -> RFPResponseStart:
    run = session.execute(select(rfp_response_runs).where(
        rfp_response_runs.c.run_id == run_id)).mappings().one_or_none()
    if run is None:
        raise ResponsePersistenceConflict("response run does not exist")
    carrier_model = run["carrier_model"]
    if carrier_model not in _CARRIER_MODELS:
        raise ResponsePersistenceConflict("response run has an invalid carrier model")
    customer_managed_carrier = {
        "unknown": None,
        "trackflow_managed": False,
        "customer_managed": True,
    }[carrier_model]
    handoff_row = session.execute(select(
        rfp_handoffs.c.contract_version, rfp_handoffs.c.summary_for_sales,
        rfp_handoffs.c.payload).where(
        rfp_handoffs.c.ticket_id == run["ticket_id"])).mappings().one()
    handoff, metadata, sections = _validated_intake_context(
        session, ticket_id=run["ticket_id"], handoff_row=dict(handoff_row))
    if canonical_hash(handoff_row["payload"]) != run["source_handoff_hash"]:
        raise ResponsePersistenceConflict("response run no longer matches its intake handoff")
    aspects = [RequestedAspect(
        aspect_id=f"{section.department_id.value}:{index:02d}:{aspect.source_ref.line_start}-{aspect.source_ref.line_end}",
        department=section.department_id, fact=aspect.fact,
        requested_action=aspect.requested_action,
        line_start=aspect.source_ref.line_start, line_end=aspect.source_ref.line_end,
    ) for section in sections for index, aspect in enumerate(section.key_aspects, start=1)]
    unit = (metadata["monthly_volume_unit"] or "").casefold().replace("³", "3")
    volume_m3 = metadata["monthly_volume_value"] if unit in {
        "m3", "cubic meter", "cubic meters", "metro cubico", "metros cubicos",
        "metro cúbico", "metros cúbicos",
    } else None
    return RFPResponseStart(ticket_id=run["ticket_id"], run_id=run_id,
        source_handoff_hash=run["source_handoff_hash"],
        summary_for_sales=handoff.summary_for_sales, key_aspects=aspects,
        selected_departments=handoff.selected_departments,
        client_country_code=metadata["client_country_code"],
        operation_countries=metadata["operation_country_codes"],
        proposal_currency=metadata["currency"],
        average_monthly_volume_m3=volume_m3,
        customer_managed_carrier=customer_managed_carrier,
        carrier_confirmation_hash=canonical_hash({
            "carrier_model": run["carrier_model"],
            "actor": run["carrier_confirmation_actor"],
            "source": run["carrier_confirmation_source"],
            "confirmed_at": run["carrier_confirmed_at"],
        }),
        language=language)


def claim_response_run(session: Session, run_id: UUID, *, lease_seconds: int) -> dict[str, Any] | None:
    token = uuid4()
    row = session.execute(text("""
      UPDATE rfp_response_runs SET
        status=CASE WHEN status='pending' THEN 'generando_borrador' ELSE status END,
        claim_token=:token,execution_epoch=execution_epoch+1,state_version=state_version+1,
        lease_until=clock_timestamp()+(:lease*interval '1 second'),heartbeat_at=clock_timestamp(),
        updated_at=clock_timestamp()
      WHERE run_id=:run_id AND status IN ('pending','generando_borrador','en_evaluación','ready_for_part3','needs_human_review')
        AND (claim_token IS NULL OR lease_until<=clock_timestamp())
        AND NOT EXISTS (SELECT 1 FROM rfp_response_handoffs h WHERE h.run_id=rfp_response_runs.run_id)
      RETURNING *
    """), {"run_id": run_id, "token": token, "lease": lease_seconds}).mappings().first()
    return None if row is None else dict(row)


def heartbeat_response_run(session: Session, *, run_id: UUID, token: UUID,
                           epoch: int, lease_seconds: int) -> bool:
    return session.execute(update(rfp_response_runs).where(
        rfp_response_runs.c.run_id == run_id, rfp_response_runs.c.claim_token == token,
        rfp_response_runs.c.execution_epoch == epoch,
        rfp_response_runs.c.status.in_(["generando_borrador", "en_evaluación"]),
    ).values(lease_until=text("clock_timestamp()+(:lease*interval '1 second')"),
             heartbeat_at=text("clock_timestamp()"), updated_at=text("clock_timestamp()")),
        {"lease": lease_seconds}).rowcount == 1


def _fence(session: Session, *, run_id: UUID, token: UUID, epoch: int,
           statuses: tuple[str, ...] = ("generando_borrador", "en_evaluación")) -> None:
    changed = session.execute(update(rfp_response_runs).where(
        rfp_response_runs.c.run_id == run_id,
        rfp_response_runs.c.claim_token == token,
        rfp_response_runs.c.execution_epoch == epoch,
        rfp_response_runs.c.status.in_(statuses),
    ).values(state_version=rfp_response_runs.c.state_version + 1,
             updated_at=text("clock_timestamp()"))).rowcount
    if changed != 1:
        raise StaleResponseExecution("response execution fence is stale")


def persist_department_draft(session: Session, *, draft: DepartmentDraft,
        preview: SafePreview, token: UUID, epoch: int) -> None:
    if preview.draft_id != draft.draft_id:
        raise ValueError("preview does not belong to draft")
    payload = _dump(draft)
    payload_hash = canonical_hash(payload)
    existing = session.execute(select(rfp_response_drafts).where(
        rfp_response_drafts.c.draft_id == draft.draft_id)).mappings().one_or_none()
    if existing is not None:
        saved_preview = session.execute(select(rfp_response_previews).where(
            rfp_response_previews.c.draft_id == draft.draft_id)).mappings().one_or_none()
        if (existing["run_id"] != draft.run_id or existing["scope_key"] != draft.department.value
                or existing["iteration_count"] != draft.iteration_count
                or existing["payload_hash"] != payload_hash or saved_preview is None
                or saved_preview["preview_id"] != preview.preview_id
                or saved_preview["gate_status"] != preview.gate_status
                or saved_preview["safe_content"] != preview.content
                or saved_preview["reason_codes"] != preview.reason_codes):
            raise ResponsePersistenceConflict("draft id was reused with different content")
        return
    _fence(session, run_id=draft.run_id, token=token, epoch=epoch)
    section = session.execute(select(rfp_response_sections).where(
        rfp_response_sections.c.run_id == draft.run_id,
        rfp_response_sections.c.department_id == draft.department.value,
    ).with_for_update()).mappings().one_or_none()
    if section is None or draft.iteration_count != int(section["iteration_count"]) + 1:
        raise ResponsePersistenceConflict("department draft iteration is not the next real generation")
    session.execute(insert(rfp_response_drafts).values(
        draft_id=draft.draft_id, run_id=draft.run_id, scope_key=draft.department.value,
        iteration_count=draft.iteration_count, contract_version=draft.contract_version,
        payload=payload, payload_hash=payload_hash))
    session.execute(insert(rfp_response_previews).values(
        preview_id=preview.preview_id, draft_id=draft.draft_id,
        gate_status=preview.gate_status, safe_content=preview.content,
        reason_codes=preview.reason_codes))
    changed = session.execute(update(rfp_response_sections).where(
        rfp_response_sections.c.run_id == draft.run_id,
        rfp_response_sections.c.department_id == draft.department.value,
        rfp_response_sections.c.iteration_count == section["iteration_count"],
    ).values(iteration_count=draft.iteration_count, status="en_evaluación",
             updated_at=text("clock_timestamp()"))).rowcount
    if changed != 1:
        raise StaleResponseExecution("department draft lost its compare-and-set")
    session.execute(update(rfp_response_runs).where(
        rfp_response_runs.c.run_id == draft.run_id).values(status="en_evaluación"))
    session.execute(update(rfp_tickets).where(
        rfp_tickets.c.ticket_id == draft.ticket_id,
        rfp_tickets.c.status == "generando_borrador").values(status="en_evaluación"))


def persist_global_draft(session: Session, *, draft: GlobalCommercialTermsDraft,
        preview: SafePreview, token: UUID, epoch: int) -> None:
    if preview.draft_id != draft.draft_id:
        raise ValueError("preview does not belong to global draft")
    payload = _dump(draft)
    payload_hash = canonical_hash(payload)
    existing = session.execute(select(rfp_response_drafts).where(
        rfp_response_drafts.c.draft_id == draft.draft_id)).mappings().one_or_none()
    if existing is not None:
        saved_preview = session.execute(select(rfp_response_previews).where(
            rfp_response_previews.c.draft_id == draft.draft_id)).mappings().one_or_none()
        if (existing["run_id"] != draft.run_id or existing["scope_key"] != "proposal"
                or existing["iteration_count"] != draft.global_iteration_count
                or existing["payload_hash"] != payload_hash or saved_preview is None
                or saved_preview["preview_id"] != preview.preview_id
                or saved_preview["gate_status"] != preview.gate_status
                or saved_preview["safe_content"] != preview.content
                or saved_preview["reason_codes"] != preview.reason_codes):
            raise ResponsePersistenceConflict("global draft id was reused with different content")
        return
    _fence(session, run_id=draft.run_id, token=token, epoch=epoch)
    current = session.execute(select(rfp_response_runs.c.global_iteration_count).where(
        rfp_response_runs.c.run_id == draft.run_id).with_for_update()).scalar_one()
    if draft.global_iteration_count != int(current) + 1:
        raise ResponsePersistenceConflict("global draft iteration is not the next real generation")
    session.execute(insert(rfp_response_drafts).values(
        draft_id=draft.draft_id, run_id=draft.run_id, scope_key="proposal",
        iteration_count=draft.global_iteration_count, contract_version=draft.contract_version,
        payload=payload, payload_hash=payload_hash))
    session.execute(insert(rfp_response_previews).values(
        preview_id=preview.preview_id, draft_id=draft.draft_id,
        gate_status=preview.gate_status, safe_content=preview.content,
        reason_codes=preview.reason_codes))
    changed = session.execute(update(rfp_response_runs).where(
        rfp_response_runs.c.run_id == draft.run_id,
        rfp_response_runs.c.global_iteration_count == current,
    ).values(global_iteration_count=draft.global_iteration_count, status="en_evaluación")).rowcount
    if changed != 1:
        raise StaleResponseExecution("global draft lost its compare-and-set")


def _persist_evaluator_result(session: Session, *, draft_id: UUID,
                              evaluator: str, value: Any) -> None:
    payload = _dump(value)
    session.execute(insert(rfp_response_evaluator_results).values(
        evaluator_result_id=uuid4(), draft_id=draft_id, evaluator=evaluator,
        contract_version=payload["contract_version"], payload=payload,
        payload_hash=canonical_hash(payload)))


def persist_section_evaluation(session: Session, *, result: SectionEvaluationResult,
        token: UUID, epoch: int) -> None:
    payload = _dump(result)
    payload_hash = canonical_hash(payload)
    existing = session.execute(select(rfp_response_evaluations).where(
        rfp_response_evaluations.c.evaluation_id == result.evaluation_id)).mappings().one_or_none()
    if existing is not None:
        if (existing["run_id"] != result.run_id or existing["draft_id"] != result.draft_id
                or existing["payload_hash"] != payload_hash):
            raise ResponsePersistenceConflict("evaluation id was reused with different content")
        return
    _fence(session, run_id=result.run_id, token=token, epoch=epoch)
    draft_row = session.execute(select(rfp_response_drafts.c.payload_hash).where(
        rfp_response_drafts.c.draft_id == result.draft_id,
        rfp_response_drafts.c.run_id == result.run_id,
        rfp_response_drafts.c.scope_key == result.department.value,
    )).scalar_one_or_none()
    if draft_row != result.draft_hash:
        raise ResponsePersistenceConflict("evaluation draft hash does not match persisted draft")
    for evaluator, value in (("readability", result.readability),
                             ("relevance", result.relevance),
                             ("section_compliance", result.compliance)):
        _persist_evaluator_result(session, draft_id=result.draft_id, evaluator=evaluator, value=value)
    session.execute(insert(rfp_response_evaluations).values(
        evaluation_id=result.evaluation_id, run_id=result.run_id, draft_id=result.draft_id,
        scope_key=result.department.value, contract_version=result.contract_version,
        overall_pass=result.overall_pass, terminal_status=result.terminal_status,
        payload=payload, payload_hash=payload_hash))
    next_status = result.terminal_status or "generando_borrador"
    changed = session.execute(update(rfp_response_sections).where(
        rfp_response_sections.c.run_id == result.run_id,
        rfp_response_sections.c.department_id == result.department.value,
        rfp_response_sections.c.iteration_count == result.iteration_count,
    ).values(status=next_status, final_draft_id=result.draft_id,
             final_evaluation_id=result.evaluation_id,
             updated_at=text("clock_timestamp()"))).rowcount
    if changed != 1:
        raise StaleResponseExecution("section evaluation lost its compare-and-set")


def persist_proposal_evaluation(session: Session, *, result: ProposalEvaluationResult,
        token: UUID, epoch: int) -> None:
    payload = _dump(result)
    payload_hash = canonical_hash(payload)
    existing = session.execute(select(rfp_response_evaluations).where(
        rfp_response_evaluations.c.evaluation_id == result.evaluation_id)).mappings().one_or_none()
    if existing is not None:
        if (existing["run_id"] != result.run_id or existing["draft_id"] != result.global_draft_id
                or existing["payload_hash"] != payload_hash):
            raise ResponsePersistenceConflict("proposal evaluation id was reused")
        return
    _fence(session, run_id=result.run_id, token=token, epoch=epoch)
    draft = session.execute(select(rfp_response_drafts.c.draft_id).where(
        rfp_response_drafts.c.draft_id == result.global_draft_id,
        rfp_response_drafts.c.run_id == result.run_id,
        rfp_response_drafts.c.scope_key == "proposal",
    )).scalar_one_or_none()
    if draft is None:
        raise ResponsePersistenceConflict("global evaluation requires its persisted draft")
    _persist_evaluator_result(session, draft_id=result.global_draft_id,
                              evaluator="global_compliance", value=result.global_compliance)
    session.execute(insert(rfp_response_evaluations).values(
        evaluation_id=result.evaluation_id, run_id=result.run_id,
        draft_id=result.global_draft_id, scope_key="proposal",
        contract_version=result.contract_version, overall_pass=result.overall_pass,
        terminal_status=result.terminal_status, payload=payload, payload_hash=payload_hash))
    run_status = "generando_borrador" if result.outcome == "regenerate" else result.outcome
    session.execute(update(rfp_response_runs).where(
        rfp_response_runs.c.run_id == result.run_id).values(
            status=run_status, updated_at=text("clock_timestamp()")))


def record_evaluator_failure(session: Session, *, failure: EvaluatorExecutionFailure,
        token: UUID, epoch: int) -> None:
    payload = _dump(failure)
    existing = session.execute(select(rfp_response_execution_failures).where(
        rfp_response_execution_failures.c.failure_id == failure.failure_id)).mappings().one_or_none()
    if existing is not None:
        comparable = {key: existing[key] for key in payload}
        if EvaluatorExecutionFailure.model_validate(comparable) != failure:
            raise ResponsePersistenceConflict("failure id was reused with different content")
        return
    _fence(session, run_id=failure.run_id, token=token, epoch=epoch)
    session.execute(insert(rfp_response_execution_failures).values(**payload))
    if failure.terminal and failure.department is not None:
        session.execute(update(rfp_response_sections).where(
            rfp_response_sections.c.run_id == failure.run_id,
            rfp_response_sections.c.department_id == failure.department.value,
        ).values(status="technical_failure", updated_at=text("clock_timestamp()")))
    if failure.terminal and failure.draft_id is not None:
        scope = session.execute(select(rfp_response_drafts.c.scope_key).where(
            rfp_response_drafts.c.draft_id == failure.draft_id)).scalar_one_or_none()
        if scope in {"warehouse", "lastmile", "reverse"}:
            session.execute(update(rfp_response_sections).where(
                rfp_response_sections.c.run_id == failure.run_id,
                rfp_response_sections.c.department_id == scope,
            ).values(status="technical_failure", updated_at=text("clock_timestamp()")))


def persist_llm_invocation(session: Session, *, invocation: LLMInvocationRecord,
        token: UUID, epoch: int) -> None:
    """Persist content-free provider evidence under the response-run fence."""
    payload = _dump(invocation)
    existing = session.execute(select(rfp_response_llm_invocations).where(
        rfp_response_llm_invocations.c.invocation_id == invocation.invocation_id
    )).mappings().one_or_none()
    if existing is not None:
        comparable = {key: existing[key] for key in payload}
        comparable["department"] = DepartmentId(comparable["department"])
        if LLMInvocationRecord.model_validate(comparable) != invocation:
            raise ResponsePersistenceConflict(
                "LLM invocation id was reused with different metadata")
        return
    _fence(session, run_id=invocation.run_id, token=token, epoch=epoch)
    session.execute(insert(rfp_response_llm_invocations).values(**payload))


def mark_response_technical_failure(session: Session, *, run_id: UUID,
        token: UUID, epoch: int) -> None:
    """Close the operational run only after parallel branches persisted their work."""
    changed = session.execute(update(rfp_response_runs).where(
        rfp_response_runs.c.run_id == run_id,
        rfp_response_runs.c.claim_token == token,
        rfp_response_runs.c.execution_epoch == epoch,
        rfp_response_runs.c.status.in_(["generando_borrador", "en_evaluación"]),
    ).values(status="technical_failure", claim_token=None, lease_until=None,
             updated_at=text("clock_timestamp()"))).rowcount
    if changed != 1:
        current = session.execute(select(rfp_response_runs.c.status).where(
            rfp_response_runs.c.run_id == run_id)).scalar_one_or_none()
        if current != "technical_failure":
            raise StaleResponseExecution("technical-failure transition lost its fence")


def finalize_response_handoff(session: Session, *, handoff: RFPResponseHandoff,
        token: UUID, epoch: int) -> None:
    payload = _dump(handoff)
    payload_hash = canonical_hash(payload)
    existing = session.execute(select(rfp_response_handoffs).where(
        rfp_response_handoffs.c.run_id == handoff.run_id)).mappings().one_or_none()
    if existing is not None:
        if existing["ticket_id"] != handoff.ticket_id or existing["payload_hash"] != payload_hash:
            raise ResponsePersistenceConflict("handoff run was reused with different content")
        return
    _fence(session, run_id=handoff.run_id, token=token, epoch=epoch,
           statuses=("ready_for_part3", "needs_human_review"))
    run = session.execute(select(rfp_response_runs).where(
        rfp_response_runs.c.run_id == handoff.run_id,
        rfp_response_runs.c.ticket_id == handoff.ticket_id,
        rfp_response_runs.c.status == handoff.outcome,
    )).mappings().one_or_none()
    if run is None or run["source_handoff_hash"] != handoff.source_handoff_hash:
        raise ResponsePersistenceConflict("only a completed business evaluation can produce a handoff")
    sections = session.execute(select(rfp_response_sections).where(
        rfp_response_sections.c.run_id == handoff.run_id)).mappings().all()
    persisted = {row["department_id"]: row for row in sections}
    if set(persisted) != {item.value for item in handoff.selected_departments}:
        raise ResponsePersistenceConflict("handoff department scope differs from persisted run")
    for result in handoff.department_results:
        row = persisted.get(result.department.value)
        if (row is None or row["status"] != result.terminal_status
                or row["final_draft_id"] != result.final_draft_id
                or row["final_evaluation_id"] != result.evaluation_result_id):
            raise ResponsePersistenceConflict("handoff references an incomplete department result")
        draft_row = session.execute(select(
            rfp_response_drafts.c.payload, rfp_response_drafts.c.payload_hash).where(
            rfp_response_drafts.c.draft_id == result.final_draft_id,
            rfp_response_drafts.c.run_id == handoff.run_id,
            rfp_response_drafts.c.scope_key == result.department.value,
        )).mappings().one_or_none()
        evaluation_row = session.execute(select(
            rfp_response_evaluations.c.payload,
            rfp_response_evaluations.c.overall_pass).where(
            rfp_response_evaluations.c.evaluation_id == result.evaluation_result_id,
            rfp_response_evaluations.c.draft_id == result.final_draft_id,
            rfp_response_evaluations.c.run_id == handoff.run_id,
        )).mappings().one_or_none()
        preview_row = session.execute(select(
            rfp_response_previews.c.preview_id,
            rfp_response_previews.c.gate_status).where(
            rfp_response_previews.c.draft_id == result.final_draft_id)).mappings().one_or_none()
        if draft_row is None or canonical_hash(draft_row["payload"]) != draft_row["payload_hash"]:
            raise ResponsePersistenceConflict("handoff department draft is not recoverable")
        if evaluation_row is None:
            raise ResponsePersistenceConflict("handoff department evaluation is not recoverable")
        persisted_result = SectionEvaluationResult.model_validate(
            evaluation_row["payload"], strict=False)
        if (persisted_result.evaluation_id != result.evaluation_result_id
                or persisted_result.draft_id != result.final_draft_id
                or persisted_result.blocks_commercial_completeness != result.blocks_commercial_completeness
                or persisted_result.requires_human_input != result.requires_human_input
                or persisted_result.blocking_rule_ids != result.blocking_rule_ids
                or persisted_result.overall_pass != evaluation_row["overall_pass"]):
            raise ResponsePersistenceConflict("handoff department result disagrees with persisted evaluation")
        if handoff.outcome == "ready_for_part3" and (
                not persisted_result.overall_pass or preview_row is None
                or preview_row["preview_id"] != result.safe_preview_id
                or preview_row["gate_status"] != "passed"):
            raise ResponsePersistenceConflict("ready handoff requires safe, passing department artifacts")
    global_eval = session.execute(select(
        rfp_response_evaluations.c.evaluation_id,
        rfp_response_evaluations.c.draft_id,
        rfp_response_evaluations.c.overall_pass,
    ).where(
        rfp_response_evaluations.c.evaluation_id == handoff.global_evaluation_result_id,
        rfp_response_evaluations.c.run_id == handoff.run_id,
        rfp_response_evaluations.c.scope_key == "proposal",
    )).mappings().one_or_none()
    expected_global_pass = handoff.outcome == "ready_for_part3"
    if (global_eval is None or global_eval["draft_id"] != handoff.global_draft_id
            or global_eval["overall_pass"] != expected_global_pass):
        raise ResponsePersistenceConflict("handoff requires a persisted global evaluation")
    global_draft = session.execute(select(
        rfp_response_drafts.c.payload, rfp_response_drafts.c.payload_hash).where(
        rfp_response_drafts.c.draft_id == handoff.global_draft_id,
        rfp_response_drafts.c.run_id == handoff.run_id,
        rfp_response_drafts.c.scope_key == "proposal",
    )).mappings().one_or_none()
    if global_draft is None or canonical_hash(global_draft["payload"]) != global_draft["payload_hash"]:
        raise ResponsePersistenceConflict("handoff global draft is not recoverable")
    global_preview_status = session.execute(select(rfp_response_previews.c.gate_status).where(
        rfp_response_previews.c.draft_id == handoff.global_draft_id)).scalar_one_or_none()
    if handoff.outcome == "ready_for_part3" and global_preview_status != "passed":
        raise ResponsePersistenceConflict("ready handoff requires a safe global preview")
    session.execute(insert(rfp_response_handoffs).values(
        run_id=handoff.run_id, ticket_id=handoff.ticket_id,
        contract_version=handoff.contract_version, outcome=handoff.outcome,
        payload=payload, payload_hash=payload_hash, created_at=handoff.created_at))
    target_ticket_status = "en_evaluación" if handoff.outcome == "ready_for_part3" else "needs_human_review"
    changed = session.execute(update(rfp_tickets).where(
        rfp_tickets.c.ticket_id == handoff.ticket_id,
        rfp_tickets.c.status == "en_evaluación",
    ).values(status=target_ticket_status)).rowcount
    if changed != 1:
        raise StaleResponseExecution("handoff ticket transition lost its compare-and-set")
    session.execute(update(rfp_response_runs).where(
        rfp_response_runs.c.run_id == handoff.run_id).values(
            claim_token=None, lease_until=None, updated_at=text("clock_timestamp()")))
