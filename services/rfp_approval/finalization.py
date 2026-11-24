"""Atomic final-document synthesis over already approved persisted artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, insert, select, update
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.hashing import canonical_hash
from pipelines.rfp_approval.contracts import (
    FinalDepartmentSection,
    FinalDocumentManifest,
    FinalDocumentPayload,
    FinalGlobalTerms,
    PersistedFinalDocument,
    PublicFinalDocument,
    public_final_document,
)
from pipelines.rfp_approval.fan_in import determine_fan_in_readiness
from services.rfp_approval.models import (
    rfp_approval_department_revisions,
    rfp_approval_proposal_revisions,
    rfp_approval_runs,
    rfp_arbitration_conflicts,
    rfp_arbitration_resolutions,
    rfp_final_documents,
    rfp_human_decisions,
    rfp_workflow_events,
)
from services.rfp_intake.models import rfp_metadata, rfp_tickets


class FinalizationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("RFP finalization was rejected")
        self.code = code


@dataclass(frozen=True, slots=True)
class FinalizationCommand:
    approval_run_id: UUID
    ticket_id: UUID
    final_document_id: UUID
    execution_id: UUID
    generated_at: datetime

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    document: PersistedFinalDocument
    replayed: bool


@dataclass(frozen=True, slots=True)
class FinalDocumentLocator:
    ticket_id: UUID
    approval_run_id: UUID
    final_document_id: UUID


_TITLES = {
    "warehouse": "Warehouse Operations",
    "lastmile": "Last Mile and Carrier Management",
    "reverse": "Reverse Logistics",
}

_GLOBAL_PENDING_DESCRIPTIONS = {
    "MONTHLY_STORAGE_VOLUME_REQUIRED": (
        "Average monthly storage volume must be confirmed before discount "
        "applicability can be determined."
    ),
    "VOLUME_DISCOUNT_APPROVAL_REQUIRED": (
        "Any preferential storage discount remains subject to authorized "
        "commercial negotiation and approval."
    ),
}


def render_final_markdown(payload: FinalDocumentPayload) -> str:
    """Deterministically render only public proposal content from the payload."""

    lines = [f"# {payload.title}", "", payload.executive_summary_markdown.strip(), ""]
    for section in payload.sections:
        lines.extend([f"## {section.title}", "", section.markdown.strip(), ""])
    currency = payload.global_terms.currency.value
    lines.extend(["## Global Commercial Terms", "", f"Currency: {currency}", ""])
    sla = payload.global_terms.sla
    if sla.applicability == "applicable":
        lines.append(f"On-time delivery SLA: {sla.percentage}%")
    else:
        lines.append("On-time delivery SLA: not applicable")
    if payload.global_terms.unresolved_input_codes:
        lines.extend([
            "",
            "### Global commercial items pending confirmation",
            "",
        ])
        for code in payload.global_terms.unresolved_input_codes:
            lines.append(f"- `{code}` — {_GLOBAL_PENDING_DESCRIPTIONS[code]}")
    lines.extend([
        "",
        "### Volume discounts",
        "",
        "| Volume range (m³/month) | Status | Discount | Condition |",
        "| --- | --- | --- | --- |",
    ])
    for row in payload.global_terms.volume_discounts:
        lower = "all documented volumes" if row.volume_from is None else row.volume_from
        upper = "and above" if row.volume_to is None else f"to {row.volume_to}"
        if row.status == "documented":
            status = "Documented"
            discount = f"{row.discount_percentage}%"
        else:
            status = "Pending — no percentage authorized"
            discount = "Not documented"
        condition = row.condition.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {lower} {upper} | {status} | {discount} | {condition} |")
    return "\n".join(lines).strip() + "\n"


def _load_document(row: dict[str, Any]) -> PersistedFinalDocument:
    return PersistedFinalDocument(
        final_document_id=row["final_document_id"],
        payload=FinalDocumentPayload.model_validate_json(json.dumps(row["payload"])),
        payload_hash=row["payload_hash"],
        markdown=row["markdown"],
        markdown_hash=row["markdown_hash"],
        storage_reference=row["storage_reference"],
        generated_at=row["generated_at"],
    )


def get_public_final_document(
    session: Session, *, approval_run_id: UUID, ticket_id: UUID
) -> PublicFinalDocument | None:
    """Load the validated public projection without exposing internal manifest data."""

    row = session.execute(
        select(rfp_final_documents).where(
            rfp_final_documents.c.approval_run_id == approval_run_id,
            rfp_final_documents.c.ticket_id == ticket_id,
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    document = _load_document(dict(row))
    return public_final_document(
        document,
        download_filename=f"trackflow-rfp-{ticket_id}.md",
    )


def get_final_document_locator(
    session: Session, *, ticket_id: UUID, environment: str
) -> FinalDocumentLocator | None:
    """Locate the newest completed document without projecting its contents."""

    row = session.execute(
        select(
            rfp_final_documents.c.ticket_id,
            rfp_final_documents.c.approval_run_id,
            rfp_final_documents.c.final_document_id,
        )
        .select_from(
            rfp_final_documents.join(
                rfp_approval_runs,
                and_(
                    rfp_approval_runs.c.approval_run_id
                    == rfp_final_documents.c.approval_run_id,
                    rfp_approval_runs.c.ticket_id == rfp_final_documents.c.ticket_id,
                ),
            )
        )
        .where(
            rfp_final_documents.c.ticket_id == ticket_id,
            rfp_approval_runs.c.environment == environment,
            rfp_approval_runs.c.status == "completed",
        )
        .order_by(rfp_final_documents.c.generated_at.desc())
        .limit(1)
    ).mappings().one_or_none()
    if row is None:
        return None
    return FinalDocumentLocator(
        ticket_id=row["ticket_id"],
        approval_run_id=row["approval_run_id"],
        final_document_id=row["final_document_id"],
    )


def _event(
    session: Session,
    *,
    command: FinalizationCommand,
    event_type: str,
    sequence_no: int,
    safe_input: dict[str, Any],
    safe_output: dict[str, Any],
    result: str,
    proposal_revision_id: UUID | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    thread_id = f"rfp-{command.ticket_id}:finalization:{command.approval_run_id}"
    event_key = "finalization:" + canonical_hash(
        {"run_id": command.approval_run_id, "event_type": event_type}
    )
    session.execute(
        insert(rfp_workflow_events).values(
            event_id=uuid4(),
            event_key=event_key,
            approval_run_id=command.approval_run_id,
            ticket_id=command.ticket_id,
            thread_id=thread_id,
            execution_id=command.execution_id,
            sequence_no=sequence_no,
            node_name=event_type,
            agent_name="trackflow_final_document_synthesizer",
            event_type=event_type,
            safe_input_ref=safe_input,
            safe_output_ref=safe_output,
            result=result,
            from_state="esperando_aprobación",
            to_state=("terminado" if event_type.endswith("ticket_closed") else None),
            proposal_revision_id=proposal_revision_id,
            started_at=now,
            finished_at=now,
        )
    )


def finalize_approved_proposal(
    session: Session, *, command: FinalizationCommand
) -> FinalizationResult:
    """Persist document and close ticket atomically in the caller's transaction."""

    if not session.in_transaction():
        raise FinalizationError("explicit_transaction_required")

    run = session.execute(
        select(rfp_approval_runs)
        .where(
            rfp_approval_runs.c.approval_run_id == command.approval_run_id,
            rfp_approval_runs.c.ticket_id == command.ticket_id,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if run is None:
        raise FinalizationError("approval_run_not_found")

    ticket = session.execute(
        select(rfp_tickets)
        .where(rfp_tickets.c.ticket_id == command.ticket_id)
        .with_for_update()
    ).mappings().one_or_none()
    if ticket is None:
        raise FinalizationError("ticket_not_found")
    metadata = session.execute(
        select(rfp_metadata).where(rfp_metadata.c.ticket_id == command.ticket_id)
    ).mappings().one_or_none()
    if metadata is None or metadata["client_country_code"] not in {"US", "ES"}:
        raise FinalizationError("client_country_not_documented")

    existing = session.execute(
        select(rfp_final_documents).where(
            rfp_final_documents.c.approval_run_id == command.approval_run_id
        )
    ).mappings().one_or_none()
    if existing is not None:
        if run["status"] != "completed" or ticket["status"] != "terminado":
            raise FinalizationError("persisted_document_without_closed_ticket")
        return FinalizationResult(document=_load_document(existing), replayed=True)

    if run["status"] not in {"active", "finalizing"}:
        raise FinalizationError("approval_run_not_finalizable")
    if ticket["status"] != "esperando_aprobación":
        raise FinalizationError("ticket_not_waiting_for_approval")

    revisions = session.execute(
        select(rfp_approval_department_revisions)
        .where(
            rfp_approval_department_revisions.c.approval_run_id
            == command.approval_run_id,
            rfp_approval_department_revisions.c.ticket_id == command.ticket_id,
            rfp_approval_department_revisions.c.is_current.is_(True),
        )
        .order_by(rfp_approval_department_revisions.c.department_id)
        .with_for_update()
    ).mappings().all()
    proposal = session.execute(
        select(rfp_approval_proposal_revisions)
        .where(
            rfp_approval_proposal_revisions.c.approval_run_id
            == command.approval_run_id,
            rfp_approval_proposal_revisions.c.ticket_id == command.ticket_id,
            rfp_approval_proposal_revisions.c.is_current.is_(True),
        )
        .with_for_update()
    ).mappings().one_or_none()
    if proposal is None:
        raise FinalizationError("current_proposal_not_found")

    decision_ids = [row["applied_decision_id"] for row in revisions]
    if any(item is None for item in decision_ids):
        raise FinalizationError("department_decision_not_applied")
    session.execute(
        select(rfp_human_decisions)
        .where(rfp_human_decisions.c.decision_id.in_(decision_ids))
        .with_for_update()
    ).mappings().all()
    session.execute(
        select(rfp_arbitration_conflicts)
        .where(
            rfp_arbitration_conflicts.c.approval_run_id == command.approval_run_id,
            rfp_arbitration_conflicts.c.status.in_(["open", "changes_requested"]),
        )
        .with_for_update()
    ).mappings().all()

    readiness = determine_fan_in_readiness(
        session,
        approval_run_id=command.approval_run_id,
        ticket_id=command.ticket_id,
    )
    if not readiness.ready_for_synthesis:
        raise FinalizationError("fan_in_blocked:" + ",".join(readiness.blockers))

    active = list(run["active_departments"])
    by_department = {row["department_id"]: row for row in revisions}
    ordered_revisions = [by_department[department] for department in active]
    sections = []
    for department in active:
        row = by_department[department]
        sections.append(
            FinalDepartmentSection(
                department=DepartmentId(department),
                title=_TITLES[department],
                markdown=row["content_payload"]["content"],
                approved_revision_id=row["revision_id"],
                approved_content_hash=row["content_hash"],
                approval_decision_id=row["applied_decision_id"],
            )
        )

    accepted_resolutions = session.execute(
        select(rfp_arbitration_resolutions.c.resolution_id).where(
            rfp_arbitration_resolutions.c.approval_run_id == command.approval_run_id,
            rfp_arbitration_resolutions.c.action == "accept_resolution",
        )
    ).scalars().all()
    proposal_content = proposal["content_payload"]
    raw_global_pending_codes = proposal.get("validation_payload", {}).get(
        "unresolved_input_codes", []
    )
    if not isinstance(raw_global_pending_codes, list) or any(
        not isinstance(item, str) for item in raw_global_pending_codes
    ):
        raise FinalizationError("proposal_global_pending_codes_invalid")
    global_pending_codes = sorted(set(raw_global_pending_codes))
    has_indicative_terms = any(
        row["content_payload"].get("pending_commercial_items")
        for row in ordered_revisions
    ) or bool(global_pending_codes)
    expected_currency = "USD" if metadata["client_country_code"] == "US" else "EUR"
    if proposal_content.get("currency", {}).get("value") != expected_currency:
        raise FinalizationError("proposal_currency_country_mismatch")
    payload = FinalDocumentPayload(
        title=(
            f"TrackFlow indicative logistics proposal — {command.ticket_id}"
            if has_indicative_terms
            else f"TrackFlow logistics proposal — {command.ticket_id}"
        ),
        executive_summary_markdown=(
            "**Indicative, non-binding proposal.** Commercial values explicitly "
            "marked as pending are not negotiated commitments and require later "
            "commercial confirmation. The document consolidates the independently "
            "approved TrackFlow service sections and documented global terms."
            if has_indicative_terms
            else "This proposal consolidates the independently approved TrackFlow "
            "service sections and documented global commercial terms."
        ),
        sections=sections,
        global_terms=FinalGlobalTerms(
            currency=proposal_content["currency"],
            sla=proposal_content["sla"],
            volume_discounts=proposal_content["volume_discounts"],
            unresolved_input_codes=global_pending_codes,
            proposal_revision_id=proposal["proposal_revision_id"],
            proposal_content_hash=proposal["content_hash"],
        ),
        manifest=FinalDocumentManifest(
            ticket_id=command.ticket_id,
            approval_run_id=command.approval_run_id,
            response_run_id=run["response_run_id"],
            department_revision_ids=[row["revision_id"] for row in ordered_revisions],
            approval_decision_ids=[
                row["applied_decision_id"] for row in ordered_revisions
            ],
            proposal_revision_id=proposal["proposal_revision_id"],
            arbitration_resolution_ids=list(accepted_resolutions),
            authorized_input_ids=[
                UUID(item) for item in proposal_content.get("authorized_input_ids", [])
            ],
        ),
    )
    markdown = render_final_markdown(payload)
    document = PersistedFinalDocument(
        final_document_id=command.final_document_id,
        payload=payload,
        payload_hash=canonical_hash(payload),
        markdown=markdown,
        markdown_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        storage_reference=f"postgres://rfp-final/{command.final_document_id}",
        generated_at=command.generated_at,
    )

    refs = {
        f"{item.department.value}_revision_id": item.revision_id
        for item in readiness.approved_revisions
    }
    session.execute(
        insert(rfp_final_documents).values(
            final_document_id=document.final_document_id,
            approval_run_id=command.approval_run_id,
            ticket_id=command.ticket_id,
            proposal_revision_id=proposal["proposal_revision_id"],
            **refs,
            contract_version=document.contract_version,
            payload=document.payload.model_dump(mode="json"),
            payload_hash=document.payload_hash,
            markdown=document.markdown,
            markdown_hash=document.markdown_hash,
            storage_reference=document.storage_reference,
            generated_at=document.generated_at,
        )
    )
    ticket_changed = session.execute(
        update(rfp_tickets)
        .where(
            rfp_tickets.c.ticket_id == command.ticket_id,
            rfp_tickets.c.status == "esperando_aprobación",
        )
        .values(status="terminado", updated_at=func.clock_timestamp())
    ).rowcount
    if ticket_changed != 1:
        raise FinalizationError("ticket_close_cas_failed")
    run_changed = session.execute(
        update(rfp_approval_runs)
        .where(
            rfp_approval_runs.c.approval_run_id == command.approval_run_id,
            rfp_approval_runs.c.status.in_(["active", "finalizing"]),
        )
        .values(
            status="completed",
            completed_at=func.clock_timestamp(),
            updated_at=func.clock_timestamp(),
            state_version=rfp_approval_runs.c.state_version + 1,
        )
    ).rowcount
    if run_changed != 1:
        raise FinalizationError("approval_run_close_cas_failed")

    first_sequence = session.execute(
        select(func.coalesce(func.max(rfp_workflow_events.c.sequence_no), 0) + 1).where(
            rfp_workflow_events.c.approval_run_id == command.approval_run_id,
            rfp_workflow_events.c.thread_id
            == f"rfp-{command.ticket_id}:finalization:{command.approval_run_id}",
        )
    ).scalar_one()
    events = [
        ("fan_in_evaluated", {"blocker_count": 0}),
        ("final_document_synthesized", {"payload_hash": document.payload_hash}),
        (
            "final_document_persisted_ticket_closed",
            {"final_document_id": str(document.final_document_id)},
        ),
    ]
    for offset, (event_type, output) in enumerate(events):
        _event(
            session,
            command=command,
            event_type=event_type,
            sequence_no=first_sequence + offset,
            safe_input={"proposal_revision_id": str(proposal["proposal_revision_id"])},
            safe_output=output,
            result="succeeded",
            proposal_revision_id=proposal["proposal_revision_id"],
        )
    return FinalizationResult(document=document, replayed=False)
