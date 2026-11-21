"""Recoverable LangGraph orchestration for TrackFlow response generation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import operator
from typing import Annotated, Protocol, TypedDict, cast
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.compliance import (
    build_global_safe_preview,
    build_safe_preview,
    evaluate_global_compliance,
    evaluate_section_compliance,
)
from data.pipelines.rfp_response.contracts import (
    DepartmentDraft,
    DepartmentHandoffResult,
    EvaluatorExecutionFailure,
    GlobalCommercialTermsDraft,
    ProposalEvaluationResult,
    ReadabilityEvaluation,
    RelevanceEvaluation,
    RFPResponseHandoff,
    RFPResponseStart,
    SafePreview,
    SafetyInspection,
    SectionComplianceEvaluation,
    SectionEvaluationResult,
)
from data.pipelines.rfp_response.evaluation import (
    reduce_proposal_evaluations,
    reduce_section_evaluations,
)
from data.pipelines.rfp_response.evaluators import (
    evaluate_relevance,
    inspect_department_safety,
    inspect_global_safety,
    required_terms,
)
from data.pipelines.rfp_response.generators import (
    generate_department_draft,
    generate_global_terms,
    volume_discount_required,
)
from data.pipelines.rfp_response.hashing import canonical_hash
from data.pipelines.rfp_response.readability import evaluate_readability


class ResponseGraphError(RuntimeError):
    """A durable or orchestration invariant was violated."""


class NonRetryableResponseOperation(RuntimeError):
    """A deterministic contract violation cannot benefit from an identical retry."""


@dataclass(frozen=True)
class DepartmentCheckpoint:
    status: str
    iteration_count: int
    draft: DepartmentDraft | None = None
    preview: SafePreview | None = None
    evaluation: SectionEvaluationResult | None = None


@dataclass(frozen=True)
class GlobalCheckpoint:
    iteration_count: int
    draft: GlobalCommercialTermsDraft | None = None
    preview: SafePreview | None = None
    evaluation: ProposalEvaluationResult | None = None


class ResponseRunRepository(Protocol):
    def department_checkpoint(self, department: DepartmentId) -> DepartmentCheckpoint: ...
    def global_checkpoint(self) -> GlobalCheckpoint: ...
    def persist_department_draft(self, draft: DepartmentDraft, preview: SafePreview) -> None: ...
    def persist_global_draft(self, draft: GlobalCommercialTermsDraft, preview: SafePreview) -> None: ...
    def persist_section_evaluation(self, result: SectionEvaluationResult) -> None: ...
    def persist_proposal_evaluation(self, result: ProposalEvaluationResult) -> None: ...
    def record_evaluator_failure(self, failure: EvaluatorExecutionFailure) -> None: ...
    def mark_technical_failure(self) -> None: ...
    def finalize_handoff(self, handoff: RFPResponseHandoff) -> None: ...


DepartmentGenerator = Callable[
    [RFPResponseStart, DepartmentId, int, Sequence[str], datetime], DepartmentDraft
]
GlobalGenerator = Callable[
    [RFPResponseStart, list, int, Sequence[str], datetime], GlobalCommercialTermsDraft
]


@dataclass(frozen=True)
class ResponseGraphContext:
    repository: ResponseRunRepository
    now: Callable[[], datetime]
    department_generator: DepartmentGenerator
    global_generator: GlobalGenerator
    readability_evaluator: Callable[[DepartmentDraft], ReadabilityEvaluation]
    relevance_evaluator: Callable[[RFPResponseStart, DepartmentDraft], RelevanceEvaluation]
    section_compliance_evaluator: Callable[
        [RFPResponseStart, DepartmentDraft, SafePreview], SectionComplianceEvaluation]
    evaluator_attempts: int = 3


@dataclass(frozen=True)
class DepartmentBranchResult:
    department: DepartmentId
    draft: DepartmentDraft | None
    preview: SafePreview | None
    evaluation: SectionEvaluationResult | None
    technical_failure: bool = False


@dataclass(frozen=True)
class ResponseExecutionResult:
    run_id: UUID
    handoff: RFPResponseHandoff | None
    technical_failure: bool


class ResponseGraphState(TypedDict, total=False):
    request: RFPResponseStart
    department: DepartmentId
    department_results: Annotated[list[DepartmentBranchResult], operator.add]
    ordered_results: list[DepartmentBranchResult]
    global_draft: GlobalCommercialTermsDraft
    global_preview: SafePreview
    proposal_evaluation: ProposalEvaluationResult
    handoff: RFPResponseHandoff
    result: ResponseExecutionResult


def _default_department_generator(request: RFPResponseStart, department: DepartmentId,
                                  iteration: int, feedback: Sequence[str],
                                  generated_at: datetime) -> DepartmentDraft:
    return generate_department_draft(request, department=department,
                                     iteration_count=iteration,
                                     generated_at=generated_at,
                                     generation_feedback=feedback)


def _default_global_generator(request: RFPResponseStart, section_ids: list,
                              iteration: int, feedback: Sequence[str],
                              generated_at: datetime) -> GlobalCommercialTermsDraft:
    return generate_global_terms(request, department_draft_ids=section_ids,
                                 iteration_count=iteration,
                                 generated_at=generated_at,
                                 generation_feedback=feedback)


def _failure(request: RFPResponseStart, *, draft_id, evaluator: str, attempt: int,
             terminal: bool, occurred_at: datetime,
             department: DepartmentId | None = None) -> EvaluatorExecutionFailure:
    return EvaluatorExecutionFailure(
        failure_id=uuid4(), ticket_id=request.ticket_id,
        run_id=request.run_id, draft_id=draft_id, evaluator=evaluator,
        department=department,
        infrastructure_attempts=attempt, error_code="evaluator_execution_failed",
        diagnostic_reference=f"rfp-response/{request.run_id}/{evaluator}/{attempt}",
        terminal=terminal, occurred_at=occurred_at,
    )


def _run_with_retries(*, request: RFPResponseStart, repository: ResponseRunRepository,
                      draft_id, evaluator: str, attempts: int,
                      now: Callable[[], datetime], operation: Callable[[], object],
                      department: DepartmentId | None = None) -> object | None:
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except NonRetryableResponseOperation:
            repository.record_evaluator_failure(_failure(
                request, draft_id=draft_id, evaluator=evaluator, attempt=attempt,
                terminal=True, occurred_at=now(), department=department))
            return None
        except Exception:
            repository.record_evaluator_failure(_failure(
                request, draft_id=draft_id, evaluator=evaluator, attempt=attempt,
                terminal=attempt == attempts, occurred_at=now(),
                department=department))
    return None


def _safe_department_preview(request: RFPResponseStart, draft: DepartmentDraft, *,
                             context: ResponseGraphContext) -> tuple[
                                 SafePreview, list[EvaluatorExecutionFailure]]:
    failures: list[EvaluatorExecutionFailure] = []
    for attempt in range(1, context.evaluator_attempts + 1):
        try:
            inspection = inspect_department_safety(
                request, draft, inspected_at=context.now())
            return build_safe_preview(draft, inspection), failures
        except Exception:
            failures.append(_failure(
                request, draft_id=draft.draft_id, evaluator="section_compliance",
                attempt=attempt, terminal=attempt == context.evaluator_attempts,
                occurred_at=context.now()))
    inspection = SafetyInspection(
        draft_id=draft.draft_id, status="technical_error",
        reason_codes=["safety_inspection_failed"], inspected_at=context.now())
    return build_safe_preview(draft, inspection), failures


def _evaluate_department_parallel(request: RFPResponseStart, draft: DepartmentDraft,
                                  preview: SafePreview, *,
                                  context: ResponseGraphContext) -> SectionEvaluationResult | None:
    operations: dict[str, Callable[[], object]] = {
        "readability": lambda: context.readability_evaluator(draft),
        "relevance": lambda: context.relevance_evaluator(request, draft),
        "section_compliance": lambda: context.section_compliance_evaluator(
            request, draft, preview),
    }
    values: dict[str, object | None] = {}
    with ThreadPoolExecutor(max_workers=3,
                            thread_name_prefix=f"rfp-eval-{draft.department.value}") as pool:
        futures = {
            name: pool.submit(_run_with_retries, request=request,
                              repository=context.repository, draft_id=draft.draft_id,
                              evaluator=name, attempts=context.evaluator_attempts,
                              now=context.now, operation=operation)
            for name, operation in operations.items()
        }
        for name, future in futures.items():
            values[name] = future.result()
    if any(value is None for value in values.values()):
        return None
    return reduce_section_evaluations(
        ticket_id=request.ticket_id, run_id=request.run_id, department=draft.department,
        draft_id=draft.draft_id, draft_hash=canonical_hash(draft),
        iteration_count=draft.iteration_count,
        readability=cast(ReadabilityEvaluation, values["readability"]),
        relevance=cast(RelevanceEvaluation, values["relevance"]),
        compliance=cast(SectionComplianceEvaluation, values["section_compliance"]),
        evaluated_at=context.now(),
    )


def _run_department(request: RFPResponseStart, department: DepartmentId, *,
                    context: ResponseGraphContext) -> DepartmentBranchResult:
    checkpoint = context.repository.department_checkpoint(department)
    if checkpoint.status == "technical_failure":
        return DepartmentBranchResult(department, checkpoint.draft, checkpoint.preview,
                                      checkpoint.evaluation, technical_failure=True)
    if checkpoint.status in {"passed", "needs_human_input", "iteration_limit_reached"}:
        if checkpoint.draft is None or checkpoint.preview is None or checkpoint.evaluation is None:
            raise ResponseGraphError("terminal department checkpoint is incomplete")
        return DepartmentBranchResult(department, checkpoint.draft, checkpoint.preview,
                                      checkpoint.evaluation)

    draft, preview, evaluation = checkpoint.draft, checkpoint.preview, checkpoint.evaluation
    feedback: Sequence[str] = evaluation.actionable_feedback if evaluation else ()
    iteration = checkpoint.iteration_count
    while True:
        if draft is None or evaluation is not None:
            iteration += 1
            if iteration > 3:
                raise ResponseGraphError("department exceeded three real generations")
            generated = _run_with_retries(
                request=request, repository=context.repository, draft_id=None,
                evaluator="department_generator",
                attempts=context.evaluator_attempts, now=context.now,
                department=department,
                operation=lambda: context.department_generator(
                    request, department, iteration, feedback, context.now()),
            )
            if generated is None:
                return DepartmentBranchResult(
                    department, None, None, None, technical_failure=True)
            draft = cast(DepartmentDraft, generated)
            preview, inspection_failures = _safe_department_preview(
                request, draft, context=context)
            context.repository.persist_department_draft(draft, preview)
            for failure in inspection_failures:
                context.repository.record_evaluator_failure(failure)
            if inspection_failures and inspection_failures[-1].terminal:
                return DepartmentBranchResult(department, draft, preview, None,
                                              technical_failure=True)
            evaluation = None
        if preview is None:
            raise ResponseGraphError("persisted department draft lacks preview")
        evaluation = _evaluate_department_parallel(request, draft, preview, context=context)
        if evaluation is None:
            return DepartmentBranchResult(department, draft, preview, None,
                                          technical_failure=True)
        context.repository.persist_section_evaluation(evaluation)
        if evaluation.terminal_status is not None:
            return DepartmentBranchResult(department, draft, preview, evaluation)
        if not evaluation.regenerable:
            raise ResponseGraphError("non-terminal department evaluation cannot continue")
        feedback = evaluation.actionable_feedback


def _section_currencies(results: Sequence[DepartmentBranchResult]) -> frozenset[str]:
    return frozenset(
        term.currency for item in results if item.draft is not None
        for term in item.draft.commercial_terms if term.currency is not None
    )


def _run_global(request: RFPResponseStart, results: list[DepartmentBranchResult], *,
                context: ResponseGraphContext) -> tuple[
                    GlobalCommercialTermsDraft | None, SafePreview | None,
                    ProposalEvaluationResult | None, bool]:
    checkpoint = context.repository.global_checkpoint()
    draft, preview, proposal = checkpoint.draft, checkpoint.preview, checkpoint.evaluation
    iteration = checkpoint.iteration_count
    if proposal is not None and proposal.outcome != "regenerate":
        if draft is None or preview is None:
            raise ResponseGraphError("terminal global checkpoint is incomplete")
        return draft, preview, proposal, False
    feedback: Sequence[str] = (
        [finding.message for finding in proposal.global_compliance.findings if finding.blocking]
        if proposal else ())
    section_evaluations = [cast(SectionEvaluationResult, item.evaluation) for item in results]
    section_ids = [cast(DepartmentDraft, item.draft).draft_id for item in results]
    while True:
        if draft is None or proposal is not None:
            iteration += 1
            if iteration > 3:
                raise ResponseGraphError("global terms exceeded three real generations")
            draft = context.global_generator(request, section_ids, iteration,
                                             feedback, context.now())
            inspection_failures: list[EvaluatorExecutionFailure] = []
            inspection = None
            for attempt in range(1, context.evaluator_attempts + 1):
                try:
                    inspection = inspect_global_safety(
                        request, draft, inspected_at=context.now())
                    break
                except Exception:
                    inspection_failures.append(_failure(
                        request, draft_id=draft.draft_id,
                        evaluator="global_compliance", attempt=attempt,
                        terminal=attempt == context.evaluator_attempts,
                        occurred_at=context.now()))
            if inspection is None:
                inspection = SafetyInspection(
                    draft_id=draft.draft_id, status="technical_error",
                    reason_codes=["safety_inspection_failed"],
                    inspected_at=context.now())
            preview = build_global_safe_preview(draft, inspection)
            context.repository.persist_global_draft(draft, preview)
            for failure in inspection_failures:
                context.repository.record_evaluator_failure(failure)
            if inspection_failures and inspection_failures[-1].terminal:
                return draft, preview, None, True
            proposal = None
        if preview is None:
            raise ResponseGraphError("persisted global draft lacks preview")
        compliance = _run_with_retries(
            request=request, repository=context.repository, draft_id=draft.draft_id,
            evaluator="global_compliance", attempts=context.evaluator_attempts,
            now=context.now,
            operation=lambda: evaluate_global_compliance(
                draft, volume_discount_required=volume_discount_required(request),
                section_currencies=_section_currencies(results), consistency_issues=[],
                preview=preview,
                sla_required=(DepartmentId.lastmile in request.selected_departments
                              and request.customer_managed_carrier is False),
                operation_countries=frozenset(
                    request.operation_countries
                    or ([request.client_country_code] if request.client_country_code else [])),
                client_country_code=request.client_country_code),
        )
        if compliance is None:
            return draft, preview, None, True
        proposal = reduce_proposal_evaluations(
            ticket_id=request.ticket_id, run_id=request.run_id,
            section_results=section_evaluations, global_draft_id=draft.draft_id,
            global_iteration_count=draft.global_iteration_count,
            global_compliance=compliance, evaluated_at=context.now())
        context.repository.persist_proposal_evaluation(proposal)
        if proposal.outcome != "regenerate":
            return draft, preview, proposal, False
        feedback = [finding.message for finding in proposal.global_compliance.findings
                    if finding.blocking]


class RFPResponseGraph:
    """LangGraph fan-out/fan-in around durable business iterations."""

    def __init__(self) -> None:
        builder = StateGraph(ResponseGraphState, context_schema=ResponseGraphContext)
        builder.add_node("prepare", self._prepare)
        builder.add_node("department_worker", self._department_worker)
        builder.add_node("join_departments", self._join_departments)
        builder.add_node("global_terms", self._global_terms)
        builder.add_node("synthesize", self._synthesize)
        builder.add_edge(START, "prepare")
        builder.add_conditional_edges("prepare", self._dispatch_departments)
        builder.add_edge("department_worker", "join_departments")
        builder.add_conditional_edges(
            "join_departments", self._after_departments,
            {"continue": "global_terms", "technical_failure": END})
        builder.add_conditional_edges(
            "global_terms", self._after_global,
            {"continue": "synthesize", "technical_failure": END})
        builder.add_edge("synthesize", END)
        self.graph = builder.compile(name="trackflow_rfp_response_generation")

    @staticmethod
    def _prepare(state: ResponseGraphState) -> ResponseGraphState:
        request = state["request"]
        if not request.selected_departments:
            raise ResponseGraphError("response graph requires selected departments")
        return {"department_results": []}

    @staticmethod
    def _dispatch_departments(state: ResponseGraphState) -> list[Send]:
        request = state["request"]
        return [Send("department_worker", {"request": request, "department": department})
                for department in request.selected_departments]

    @staticmethod
    def _department_worker(state: ResponseGraphState,
                           runtime: Runtime[ResponseGraphContext]) -> ResponseGraphState:
        return {"department_results": [
            _run_department(state["request"], state["department"], context=runtime.context)
        ]}

    @staticmethod
    def _join_departments(state: ResponseGraphState,
                          runtime: Runtime[ResponseGraphContext]) -> ResponseGraphState:
        request = state["request"]
        by_department = {item.department: item for item in state["department_results"]}
        if len(by_department) != len(request.selected_departments):
            raise ResponseGraphError("department fan-in is incomplete or duplicated")
        ordered = [by_department[item] for item in request.selected_departments]
        if any(item.technical_failure for item in ordered):
            runtime.context.repository.mark_technical_failure()
            return {"ordered_results": ordered,
                    "result": ResponseExecutionResult(request.run_id, None, True)}
        return {"ordered_results": ordered}

    @staticmethod
    def _after_departments(state: ResponseGraphState) -> str:
        return "technical_failure" if state.get("result") else "continue"

    @staticmethod
    def _global_terms(state: ResponseGraphState,
                      runtime: Runtime[ResponseGraphContext]) -> ResponseGraphState:
        draft, preview, proposal, failed = _run_global(
            state["request"], state["ordered_results"], context=runtime.context)
        if failed:
            runtime.context.repository.mark_technical_failure()
            return {"result": ResponseExecutionResult(state["request"].run_id, None, True)}
        return {"global_draft": cast(GlobalCommercialTermsDraft, draft),
                "global_preview": cast(SafePreview, preview),
                "proposal_evaluation": cast(ProposalEvaluationResult, proposal)}

    @staticmethod
    def _after_global(state: ResponseGraphState) -> str:
        return "technical_failure" if state.get("result") else "continue"

    @staticmethod
    def _synthesize(state: ResponseGraphState,
                    runtime: Runtime[ResponseGraphContext]) -> ResponseGraphState:
        request = state["request"]
        proposal = state["proposal_evaluation"]
        if proposal.outcome == "regenerate":
            raise ResponseGraphError("synthesizer received non-terminal global evaluation")
        results = state["ordered_results"]
        department_results = [DepartmentHandoffResult(
            department=item.department,
            terminal_status=cast(SectionEvaluationResult, item.evaluation).terminal_status,
            iteration_count=cast(DepartmentDraft, item.draft).iteration_count,
            final_draft_id=cast(DepartmentDraft, item.draft).draft_id,
            evaluation_result_id=cast(SectionEvaluationResult, item.evaluation).evaluation_id,
            safe_preview_id=(cast(SafePreview, item.preview).preview_id
                             if cast(SafePreview, item.preview).gate_status == "passed" else None),
            blocks_commercial_completeness=cast(
                SectionEvaluationResult, item.evaluation).blocks_commercial_completeness,
            requires_human_input=cast(
                SectionEvaluationResult, item.evaluation).requires_human_input,
            blocking_rule_ids=cast(
                SectionEvaluationResult, item.evaluation).blocking_rule_ids,
        ) for item in results]
        unresolved = [pending for item in results for pending in cast(
            DepartmentDraft, item.draft).pending_commercial_items]
        unresolved.extend(state["global_draft"].pending_commercial_items)
        unresolved_by_code = {item.code: item for item in unresolved}
        handoff = RFPResponseHandoff(
            ticket_id=request.ticket_id, run_id=request.run_id,
            source_handoff_hash=request.source_handoff_hash,
            outcome=proposal.outcome,
            all_sections_passed=proposal.all_departments_passed,
            global_compliance_passed=proposal.global_compliance.passed,
            selected_departments=request.selected_departments,
            department_results=department_results,
            global_draft_id=state["global_draft"].draft_id,
            global_evaluation_result_id=proposal.evaluation_id,
            unresolved_items=list(unresolved_by_code.values()), created_at=runtime.context.now())
        runtime.context.repository.finalize_handoff(handoff)
        return {"handoff": handoff,
                "result": ResponseExecutionResult(request.run_id, handoff, False)}

    def execute(self, *, request: RFPResponseStart, repository: ResponseRunRepository,
                now: Callable[[], datetime] | None = None,
                department_generator: DepartmentGenerator = _default_department_generator,
                global_generator: GlobalGenerator = _default_global_generator,
                readability_evaluator: Callable[[DepartmentDraft], ReadabilityEvaluation] | None = None,
                relevance_evaluator: Callable[[RFPResponseStart, DepartmentDraft], RelevanceEvaluation] | None = None,
                section_compliance_evaluator: Callable[
                    [RFPResponseStart, DepartmentDraft, SafePreview],
                    SectionComplianceEvaluation] | None = None,
                evaluator_attempts: int = 3) -> ResponseExecutionResult:
        if evaluator_attempts < 1:
            raise ValueError("evaluator_attempts must be positive")
        output = self.graph.invoke(
            {"request": request, "department_results": []},
            context=ResponseGraphContext(
                repository=repository,
                now=now or (lambda: datetime.now(timezone.utc)),
                department_generator=department_generator,
                global_generator=global_generator,
                readability_evaluator=(readability_evaluator or (
                    lambda draft: evaluate_readability(
                        draft.generated_content, language=draft.language))),
                relevance_evaluator=relevance_evaluator or evaluate_relevance,
                section_compliance_evaluator=(section_compliance_evaluator or (
                    lambda request, draft, preview: evaluate_section_compliance(
                        draft, required_commercial_terms=required_terms(
                            request, draft.department), preview=preview))),
                evaluator_attempts=evaluator_attempts,
            ),
        )
        return cast(ResponseExecutionResult, output["result"])
