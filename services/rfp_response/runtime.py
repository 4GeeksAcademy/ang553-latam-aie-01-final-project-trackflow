"""PostgreSQL adapter for the persistence-neutral RFP response graph."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    DepartmentDraft,
    EvaluatorExecutionFailure,
    GlobalCommercialTermsDraft,
    LLMInvocationRecord,
    ProposalEvaluationResult,
    RFPResponseHandoff,
    SafePreview,
    SectionEvaluationResult,
)
from data.pipelines.rfp_response.graph import (
    DepartmentCheckpoint,
    GlobalCheckpoint,
    RFPResponseGraph,
    ResponseExecutionResult,
)
from services.rfp_response.models import (
    rfp_response_drafts,
    rfp_response_evaluations,
    rfp_response_previews,
    rfp_response_runs,
    rfp_response_sections,
)
from services.rfp_response.store import (
    finalize_response_handoff,
    load_response_start,
    mark_response_technical_failure,
    persist_department_draft,
    persist_global_draft,
    persist_llm_invocation,
    persist_proposal_evaluation,
    persist_section_evaluation,
    record_evaluator_failure,
)
from services.rfp_response.llm import (
    FourGeeksRFPProvider,
    LLMCallBudget,
    RFPResponseLLMConfigurationError,
    default_fourgeeks_gateway_factory,
    load_rfp_response_llm_settings,
)


class PostgresResponseRunRepository:
    """Thread-safe adapter: every graph operation owns a fresh transaction."""

    def __init__(self, engine: Any, *, run_id: UUID, token: UUID, epoch: int) -> None:
        self.engine = engine
        self.run_id = run_id
        self.token = token
        self.epoch = epoch

    @staticmethod
    def _preview(row: Any) -> SafePreview | None:
        if row is None:
            return None
        return SafePreview(
            preview_id=row["preview_id"], draft_id=row["draft_id"],
            gate_status=row["gate_status"], content=row["safe_content"],
            reason_codes=row["reason_codes"])

    def department_checkpoint(self, department: DepartmentId) -> DepartmentCheckpoint:
        with Session(self.engine) as session:
            section = session.execute(select(rfp_response_sections).where(
                rfp_response_sections.c.run_id == self.run_id,
                rfp_response_sections.c.department_id == department.value,
            )).mappings().one()
            draft_row = session.execute(select(rfp_response_drafts).where(
                rfp_response_drafts.c.run_id == self.run_id,
                rfp_response_drafts.c.scope_key == department.value,
            ).order_by(rfp_response_drafts.c.iteration_count.desc()).limit(1)
            ).mappings().one_or_none()
            if draft_row is None:
                return DepartmentCheckpoint(section["status"], section["iteration_count"])
            preview_row = session.execute(select(rfp_response_previews).where(
                rfp_response_previews.c.draft_id == draft_row["draft_id"])
            ).mappings().one_or_none()
            evaluation_row = session.execute(select(rfp_response_evaluations).where(
                rfp_response_evaluations.c.draft_id == draft_row["draft_id"])
            ).mappings().one_or_none()
            return DepartmentCheckpoint(
                status=section["status"], iteration_count=section["iteration_count"],
                draft=DepartmentDraft.model_validate(draft_row["payload"], strict=False),
                preview=self._preview(preview_row),
                evaluation=(None if evaluation_row is None else
                            SectionEvaluationResult.model_validate(
                                evaluation_row["payload"], strict=False)))

    def global_checkpoint(self) -> GlobalCheckpoint:
        with Session(self.engine) as session:
            count = session.execute(select(rfp_response_runs.c.global_iteration_count).where(
                rfp_response_runs.c.run_id == self.run_id)).scalar_one()
            draft_row = session.execute(select(rfp_response_drafts).where(
                rfp_response_drafts.c.run_id == self.run_id,
                rfp_response_drafts.c.scope_key == "proposal",
            ).order_by(rfp_response_drafts.c.iteration_count.desc()).limit(1)
            ).mappings().one_or_none()
            if draft_row is None:
                return GlobalCheckpoint(iteration_count=count)
            preview_row = session.execute(select(rfp_response_previews).where(
                rfp_response_previews.c.draft_id == draft_row["draft_id"])
            ).mappings().one_or_none()
            evaluation_row = session.execute(select(rfp_response_evaluations).where(
                rfp_response_evaluations.c.draft_id == draft_row["draft_id"])
            ).mappings().one_or_none()
            return GlobalCheckpoint(
                iteration_count=count,
                draft=GlobalCommercialTermsDraft.model_validate(
                    draft_row["payload"], strict=False),
                preview=self._preview(preview_row),
                evaluation=(None if evaluation_row is None else
                            ProposalEvaluationResult.model_validate(
                                evaluation_row["payload"], strict=False)))

    def _transaction(self, operation) -> None:
        with Session(self.engine) as session:
            with session.begin():
                operation(session)

    def persist_department_draft(self, draft: DepartmentDraft, preview: SafePreview) -> None:
        self._transaction(lambda session: persist_department_draft(
            session, draft=draft, preview=preview, token=self.token, epoch=self.epoch))

    def persist_global_draft(self, draft: GlobalCommercialTermsDraft, preview: SafePreview) -> None:
        self._transaction(lambda session: persist_global_draft(
            session, draft=draft, preview=preview, token=self.token, epoch=self.epoch))

    def persist_section_evaluation(self, result: SectionEvaluationResult) -> None:
        self._transaction(lambda session: persist_section_evaluation(
            session, result=result, token=self.token, epoch=self.epoch))

    def persist_proposal_evaluation(self, result: ProposalEvaluationResult) -> None:
        self._transaction(lambda session: persist_proposal_evaluation(
            session, result=result, token=self.token, epoch=self.epoch))

    def record_evaluator_failure(self, failure: EvaluatorExecutionFailure) -> None:
        self._transaction(lambda session: record_evaluator_failure(
            session, failure=failure, token=self.token, epoch=self.epoch))

    def record_llm_invocation(self, invocation: LLMInvocationRecord) -> None:
        self._transaction(lambda session: persist_llm_invocation(
            session, invocation=invocation, token=self.token, epoch=self.epoch))

    def mark_technical_failure(self) -> None:
        self._transaction(lambda session: mark_response_technical_failure(
            session, run_id=self.run_id, token=self.token, epoch=self.epoch))

    def finalize_handoff(self, handoff: RFPResponseHandoff) -> None:
        self._transaction(lambda session: finalize_response_handoff(
            session, handoff=handoff, token=self.token, epoch=self.epoch))


def process_response_run(engine: Any, *, claimed_run: dict[str, Any]) -> ResponseExecutionResult:
    """Load the approved intake handoff and execute/recover one response run."""
    run_id = UUID(str(claimed_run["run_id"]))
    with Session(engine) as session:
        request = load_response_start(session, run_id=run_id)
    repository = PostgresResponseRunRepository(
        engine, run_id=run_id, token=UUID(str(claimed_run["claim_token"])),
        epoch=int(claimed_run["execution_epoch"]))
    try:
        llm_settings = load_rfp_response_llm_settings()
    except RFPResponseLLMConfigurationError:
        repository.record_evaluator_failure(EvaluatorExecutionFailure(
            failure_id=uuid4(), ticket_id=request.ticket_id, run_id=request.run_id,
            draft_id=None, evaluator="department_generator", department=None,
            infrastructure_attempts=1,
            error_code="provider_configuration_invalid",
            diagnostic_reference=f"rfp-response/{request.run_id}/provider-configuration/1",
            terminal=True, occurred_at=datetime.now(timezone.utc),
        ))
        repository.mark_technical_failure()
        return ResponseExecutionResult(request.run_id, None, True)
    if llm_settings.mode == "deterministic":
        return RFPResponseGraph().execute(request=request, repository=repository)

    provider = FourGeeksRFPProvider(
        settings=llm_settings,
        gateway_factory=default_fourgeeks_gateway_factory(llm_settings),
        budget=LLMCallBudget(llm_settings),
        invocation_sink=repository.record_llm_invocation,
    )
    return RFPResponseGraph().execute(
        request=request,
        repository=repository,
        department_generator=provider.generate_department,
        relevance_evaluator=provider.evaluate_relevance,
    )
