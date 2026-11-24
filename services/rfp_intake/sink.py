"""Transactional graph-result sink for PostgreSQL."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import insert, update
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId, RESPONSIBLE_BY_DEPARTMENT, RFPIntakeDecision
from services.rfp_intake.intake_events import get_intake_decision, record_intake_decision, require_intake_claim
from services.rfp_intake.models import rfp_department_sections, rfp_handoffs, rfp_metadata, rfp_tickets
from services.rfp_intake.store import finish_job


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, tuple):
        return [_dump(item) for item in value]
    if isinstance(value, list):
        return [_dump(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _dump(item) for key, item in value.items()}
    return value


class PostgresRFPResultSink:
    """Persist one terminal result and fence its owning job in one transaction."""

    def __init__(self, session: Session, job: dict[str, Any]) -> None:
        self._session, self._job = session, job

    def persist_intake_decision(self, decision: RFPIntakeDecision) -> None:
        record_intake_decision(self._session, self._job, decision)
        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            with Session(self._session.get_bind()) as reconciliation:
                if get_intake_decision(reconciliation, decision.ticket_id) != decision:
                    raise
                require_intake_claim(reconciliation, self._job)

    def persist_discard(self, ticket_id: UUID, classification: Any,
                        conversion_metrics: Any, readability_metrics: Any) -> None:
        require_intake_claim(self._session, self._job)
        if get_intake_decision(self._session, ticket_id) is not None:
            raise ValueError("A committed intake decision cannot be discarded")
        classification_data = _dump(classification)
        self._session.execute(insert(rfp_metadata).values(
            ticket_id=ticket_id, services_requested=[], departments_needed=[],
            readability_metrics=_dump(readability_metrics), conversion_metrics=_dump(conversion_metrics),
        ))
        changed = self._session.execute(update(rfp_tickets).where(
            rfp_tickets.c.ticket_id == ticket_id, rfp_tickets.c.status == "analizando",
        ).values(status="descartado", discard_reason_code=classification_data.get("reason_code", "not_rfp"),
                 discard_summary="Document is not a TrackFlow client RFP")).rowcount == 1
        if not changed or not finish_job(self._session, job=self._job, status="succeeded"):
            raise RuntimeError("stale RFP job cannot persist discard")
        self._session.commit()

    def persist_complete(self, ticket_id: UUID, metadata: Any, sections: Any, handoff: Any,
                         conversion_metrics: Any, readability_metrics: Any) -> None:
        require_intake_claim(self._session, self._job)
        metadata_data, section_rows, handoff_data = _dump(metadata), _dump(sections), _dump(handoff)
        departments = list(metadata_data.get("departments_needed", []))
        section_ids = [row["department_id"] for row in section_rows]
        handoff_ids = [row["department_id"] for row in handoff_data.get("department_section_references", [])]
        if len(section_ids) != len(set(section_ids)) or set(departments) != set(section_ids) or set(departments) != set(handoff_ids):
            raise ValueError("RFP result department sets do not match")
        self._session.execute(insert(rfp_metadata).values(
            ticket_id=ticket_id,
            client_name=metadata_data.get("client_name"), client_country_code=metadata_data.get("client_country_code"),
            client_country_original=metadata_data.get("client_country_original"), operation_country_codes=metadata_data.get("operation_country_codes"),
            currency=metadata_data.get("currency"), services_requested=metadata_data.get("services_requested", []),
            monthly_volume_value=metadata_data.get("monthly_volume_value"), monthly_volume_unit=metadata_data.get("monthly_volume_unit"),
            monthly_volume_source_ref=metadata_data.get("monthly_volume_source_ref"), deadline=metadata_data.get("deadline"),
            onboarding_target=metadata_data.get("onboarding_target"), budget_range=metadata_data.get("budget_range"), departments_needed=departments,
            readability_metrics=_dump(readability_metrics), conversion_metrics=_dump(conversion_metrics),
        ))
        for row in section_rows:
            self._session.execute(insert(rfp_department_sections).values(
                ticket_id=ticket_id, department_id=row["department_id"], key_aspects=row.get("key_aspects", []),
                open_questions=row.get("open_questions", []), source_references=row.get("source_references", []),
                detected_conflicts=row.get("detected_conflicts", []),
            ))
        handoff_data.setdefault("contract_version", "rfp-intake-v1")
        handoff_data.setdefault("ticket_id", str(ticket_id))
        for reference in handoff_data.get("department_section_references", []):
            department_id = DepartmentId(reference["department_id"])
            reference["responsible"] = RESPONSIBLE_BY_DEPARTMENT[department_id]
        self._session.execute(insert(rfp_handoffs).values(
            ticket_id=ticket_id, contract_version="rfp-intake-v1",
            summary_for_sales=handoff_data["summary_for_sales"], payload=handoff_data,
        ))
        changed = self._session.execute(update(rfp_tickets).where(
            rfp_tickets.c.ticket_id == ticket_id, rfp_tickets.c.status == "analizando",
        ).values(status="analisis_completo", rfp_id=metadata_data.get("rfp_id"))).rowcount == 1
        if not changed or not finish_job(self._session, job=self._job, status="succeeded"):
            raise RuntimeError("stale RFP job cannot persist result")
        self._session.commit()
