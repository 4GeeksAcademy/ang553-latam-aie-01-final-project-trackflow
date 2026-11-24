"""Dedicated orchestrator-worker-synthesizer graph for TrackFlow RFP intake."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import operator
from typing import Annotated, TypedDict, cast
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from data.pipelines.rfp_intake.analyzer import (
    ProviderResponseDiagnostic,
    RFPAnalyzer,
    StructuredGenerationContext,
    structured_contract_error,
)
from data.pipelines.rfp_intake.contracts import (
    ClassificationDecision,
    ConversionMetrics,
    DepartmentId,
    DepartmentSection,
    DepartmentSectionReference,
    ReadabilityReport,
    RESPONSIBLE_BY_DEPARTMENT,
    RFPExecutionResult,
    RFPHandoff,
    RFPMetadata,
    RFPMetadataDraft,
    RFPResultSink,
    SourceReference,
    SynthesisDraft,
)
from data.pipelines.rfp_intake.routing import department_extract, selected_departments


class RFPGraphError(RuntimeError):
    """A graph invariant failed; the job must not be marked complete."""


class RFPGraphState(TypedDict, total=False):
    ticket_id: UUID
    markdown: str
    conversion_metrics: ConversionMetrics
    readability_metrics: ReadabilityReport
    classification: ClassificationDecision
    metadata_draft: RFPMetadataDraft
    metadata: RFPMetadata
    selected_departments: list[DepartmentId]
    department_id: DepartmentId
    relevant_extract: str
    source_line_count: int
    department_results: Annotated[list[DepartmentSection], operator.add]
    validated_sections: list[DepartmentSection]
    synthesis: SynthesisDraft
    handoff: RFPHandoff
    result: RFPExecutionResult


@dataclass(frozen=True)
class RFPGraphContext:
    analyzer: RFPAnalyzer
    sink: RFPResultSink
    now: Callable[[], datetime]


def _all_references(section: DepartmentSection) -> list[SourceReference]:
    refs = list(section.source_references)
    refs.extend(item.source_ref for item in section.key_aspects)
    refs.extend(item.source_ref for item in section.open_questions if item.source_ref)
    for conflict in section.detected_conflicts:
        refs.extend(conflict.source_refs)
    return refs


def _validate_reference_bounds(
    line_count: int,
    references: list[SourceReference],
    *,
    analyzer: RFPAnalyzer,
    operation: str,
    path: str,
) -> None:
    if line_count < 1:
        raise RFPGraphError("Markdown has no lines")
    if any(ref.line_end > line_count for ref in references):
        getter = getattr(analyzer, "diagnostic_context", None)
        context = getter(operation) if callable(getter) else None
        if not isinstance(context, StructuredGenerationContext):
            context = StructuredGenerationContext(
                operation=operation,
                invocation_id=uuid4(),
                provider=ProviderResponseDiagnostic(),
            )
        raise structured_contract_error(
            context=context,
            category="reference_bounds",
            error_type="source_reference_out_of_bounds",
            path=path,
            received_type="integer",
            violated_constraint="markdown_line_bounds",
            message="structured output cites a nonexistent Markdown line",
        )


def _validate_references(
    markdown: str,
    references: list[SourceReference],
    *,
    analyzer: RFPAnalyzer,
    operation: str,
    path: str,
) -> None:
    _validate_reference_bounds(
        len(markdown.splitlines()),
        references,
        analyzer=analyzer,
        operation=operation,
        path=path,
    )


def _derive_metadata(draft: RFPMetadataDraft) -> RFPMetadata:
    departments = selected_departments(draft)
    currency = {"ES": "EUR", "US": "USD"}.get(draft.client_country_code)
    return RFPMetadata(
        **draft.model_dump(),
        currency=currency,
        departments_needed=departments,
    )


class RFPIntakeGraph:
    """Compile and execute the one canonical RFP intake graph."""

    def __init__(self) -> None:
        builder = StateGraph(RFPGraphState, context_schema=RFPGraphContext)
        builder.add_node("validate_input", self._validate_input)
        builder.add_node("classify", self._classify)
        builder.add_node("persist_discard", self._persist_discard)
        builder.add_node("extract_metadata", self._extract_metadata)
        builder.add_node("orchestrator_route", self._orchestrator_route)
        builder.add_node("department_worker", self._department_worker)
        builder.add_node("validate_join", self._validate_join)
        builder.add_node("synthesizer", self._synthesizer)
        builder.add_node("transactional_commit", self._transactional_commit)
        builder.add_edge(START, "validate_input")
        builder.add_edge("validate_input", "classify")
        builder.add_conditional_edges(
            "classify",
            self._after_classification,
            {"valid": "extract_metadata", "discard": "persist_discard"},
        )
        builder.add_edge("persist_discard", END)
        builder.add_edge("extract_metadata", "orchestrator_route")
        builder.add_conditional_edges("orchestrator_route", self._dispatch_workers)
        builder.add_edge("department_worker", "validate_join")
        builder.add_edge("validate_join", "synthesizer")
        builder.add_edge("synthesizer", "transactional_commit")
        builder.add_edge("transactional_commit", END)
        self.graph = builder.compile(name="trackflow_rfp_intake")

    @staticmethod
    def _validate_input(state: RFPGraphState) -> RFPGraphState:
        if not state["markdown"].strip():
            raise RFPGraphError("Markdown must not be empty")
        return {}

    @staticmethod
    def _classify(state: RFPGraphState, runtime: Runtime[RFPGraphContext]) -> RFPGraphState:
        classification = runtime.context.analyzer.classify(state["markdown"])
        _validate_references(
            state["markdown"],
            classification.evidence_refs,
            analyzer=runtime.context.analyzer,
            operation="trackflow_rfp_classification",
            path="evidence_refs",
        )
        return {"classification": classification}

    @staticmethod
    def _after_classification(state: RFPGraphState) -> str:
        return "valid" if state["classification"].is_valid_rfp else "discard"

    @staticmethod
    def _persist_discard(state: RFPGraphState, runtime: Runtime[RFPGraphContext]) -> RFPGraphState:
        runtime.context.sink.persist_discard(
            ticket_id=state["ticket_id"],
            classification=state["classification"],
            conversion_metrics=state["conversion_metrics"],
            readability_metrics=state["readability_metrics"],
        )
        result = RFPExecutionResult(
            ticket_id=state["ticket_id"],
            outcome="discarded",
            classification=state["classification"],
            metadata=None,
            sections=[],
            handoff=None,
        )
        return {"result": result}

    @staticmethod
    def _extract_metadata(state: RFPGraphState, runtime: Runtime[RFPGraphContext]) -> RFPGraphState:
        draft = runtime.context.analyzer.extract_metadata(state["markdown"])
        references = [item.source_ref for item in draft.services_requested]
        for ref in (
            draft.monthly_volume_source_ref,
            draft.deadline_source_ref,
            draft.onboarding_source_ref,
        ):
            if ref is not None:
                references.append(ref)
        if draft.budget_range is not None:
            references.append(draft.budget_range.source_ref)
        _validate_references(
            state["markdown"],
            references,
            analyzer=runtime.context.analyzer,
            operation="trackflow_rfp_metadata",
            path="source_references",
        )
        return {"metadata_draft": draft}

    @staticmethod
    def _orchestrator_route(state: RFPGraphState) -> RFPGraphState:
        metadata = _derive_metadata(state["metadata_draft"])
        departments = metadata.departments_needed
        if not departments:
            raise RFPGraphError("valid RFP has no supported requested department")
        return {"metadata": metadata, "selected_departments": departments}

    @staticmethod
    def _dispatch_workers(state: RFPGraphState) -> list[Send]:
        sends: list[Send] = []
        for department in state["selected_departments"]:
            extract = department_extract(state["markdown"], department)
            if not extract:
                raise RFPGraphError(f"no relevant extract for {department.value}")
            sends.append(
                Send(
                    "department_worker",
                    {
                        "metadata": state["metadata"],
                        "department_id": department,
                        "relevant_extract": extract,
                        "source_line_count": len(state["markdown"].splitlines()),
                    },
                )
            )
        return sends

    @staticmethod
    def _department_worker(state: RFPGraphState, runtime: Runtime[RFPGraphContext]) -> RFPGraphState:
        section = runtime.context.analyzer.analyze_department(
            metadata=state["metadata"],
            department_id=state["department_id"],
            relevant_extract=state["relevant_extract"],
        )
        _validate_reference_bounds(
            state["source_line_count"],
            _all_references(section),
            analyzer=runtime.context.analyzer,
            operation=f"trackflow_rfp_{state['department_id'].value}_section",
            path="source_references",
        )
        return {"department_results": [section]}

    @staticmethod
    def _validate_join(state: RFPGraphState) -> RFPGraphState:
        selected = state["selected_departments"]
        results = state.get("department_results", [])
        result_ids = [item.department_id for item in results]
        if len(result_ids) != len(set(result_ids)):
            raise RFPGraphError("worker result contains duplicate departments")
        if len(result_ids) != len(selected) or set(result_ids) != set(selected):
            raise RFPGraphError("worker results do not match selected departments")
        ordered = sorted(results, key=lambda item: selected.index(item.department_id))
        return {"validated_sections": ordered}

    @staticmethod
    def _synthesizer(state: RFPGraphState, runtime: Runtime[RFPGraphContext]) -> RFPGraphState:
        sections = state["validated_sections"]
        synthesis = runtime.context.analyzer.synthesize(
            metadata=state["metadata"], sections=sections
        )
        refs: list[SourceReference] = []
        refs.extend(item.source_ref for item in synthesis.open_questions if item.source_ref)
        for conflict in synthesis.detected_conflicts:
            refs.extend(conflict.source_refs)
        _validate_references(
            state["markdown"],
            refs,
            analyzer=runtime.context.analyzer,
            operation="trackflow_rfp_synthesis",
            path="source_references",
        )
        handoff = RFPHandoff(
            ticket_id=state["ticket_id"],
            metadata_reference=state["ticket_id"],
            summary_for_sales=synthesis.summary_for_sales,
            selected_departments=state["selected_departments"],
            department_section_references=[
                DepartmentSectionReference(
                    department_id=department,
                    responsible=RESPONSIBLE_BY_DEPARTMENT[department],
                    ticket_id=state["ticket_id"],
                )
                for department in state["selected_departments"]
            ],
            open_questions=synthesis.open_questions,
            detected_conflicts=synthesis.detected_conflicts,
            created_at=runtime.context.now(),
        )
        return {"synthesis": synthesis, "handoff": handoff}

    @staticmethod
    def _transactional_commit(
        state: RFPGraphState, runtime: Runtime[RFPGraphContext]
    ) -> RFPGraphState:
        sections = state["validated_sections"]
        runtime.context.sink.persist_complete(
            ticket_id=state["ticket_id"],
            metadata=state["metadata"],
            sections=sections,
            handoff=state["handoff"],
            conversion_metrics=state["conversion_metrics"],
            readability_metrics=state["readability_metrics"],
        )
        result = RFPExecutionResult(
            ticket_id=state["ticket_id"],
            outcome="analysis_complete",
            classification=state["classification"],
            metadata=state["metadata"],
            sections=sections,
            handoff=state["handoff"],
        )
        return {"result": result}

    def execute(
        self,
        *,
        ticket_id: UUID,
        markdown: str,
        conversion_metrics: ConversionMetrics,
        readability_metrics: ReadabilityReport,
        analyzer: RFPAnalyzer,
        sink: RFPResultSink,
        now: Callable[[], datetime] | None = None,
    ) -> RFPExecutionResult:
        output = self.graph.invoke(
            {
                "ticket_id": ticket_id,
                "markdown": markdown,
                "conversion_metrics": conversion_metrics,
                "readability_metrics": readability_metrics,
                "department_results": [],
            },
            context=RFPGraphContext(
                analyzer=analyzer,
                sink=sink,
                now=now or (lambda: datetime.now(timezone.utc)),
            ),
        )
        return cast(RFPExecutionResult, output["result"])
