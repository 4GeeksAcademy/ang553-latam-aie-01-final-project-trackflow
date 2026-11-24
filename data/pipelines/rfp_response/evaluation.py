"""Business reducers; technical execution failures never enter these functions."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    GlobalComplianceEvaluation, ProposalEvaluationResult, ReadabilityEvaluation,
    RelevanceEvaluation, SectionComplianceEvaluation, SectionEvaluationResult,
)


def reduce_section_evaluations(*, ticket_id: UUID, run_id: UUID, department: DepartmentId,
        draft_id: UUID, draft_hash: str, iteration_count: int,
        readability: ReadabilityEvaluation, relevance: RelevanceEvaluation,
        compliance: SectionComplianceEvaluation, evaluated_at: datetime) -> SectionEvaluationResult:
    blocking = [f for f in compliance.findings if f.blocking]
    blocks = any(f.blocks_commercial_completeness for f in compliance.findings)
    human = any(f.requires_human_input for f in compliance.findings)
    passed = readability.passed is not False and relevance.passed and compliance.passed and not blocks and not human
    terminal = "passed" if passed else "needs_human_input" if human else "iteration_limit_reached" if iteration_count == 3 else None
    evaluator_failed = readability.passed is False or not relevance.passed
    regenerable = (
        not passed and not human and iteration_count < 3
        and (evaluator_failed or bool(blocking))
        and all(f.regenerable for f in blocking)
    )
    return SectionEvaluationResult(evaluation_id=uuid4(), ticket_id=ticket_id, run_id=run_id,
        department=department, draft_id=draft_id, draft_hash=draft_hash,
        iteration_count=iteration_count, readability=readability, relevance=relevance,
        compliance=compliance,
        actionable_feedback=(list(relevance.feedback)
            + [f.message for f in compliance.findings if f.blocking or f.requires_human_input]
            + (["Shorten sentences and use clearer language before regenerating the section."]
               if readability.passed is False else []))[:60],
        missing_aspect_ids=[item.aspect_id for item in relevance.aspect_results
                            if item.status == "missing"],
        contradicted_aspect_ids=[item.aspect_id for item in relevance.aspect_results
                                 if item.status == "contradicted"],
        blocks_commercial_completeness=blocks,
        blocking_rule_ids=[f.rule_id for f in blocking], requires_human_input=human,
        regenerable=regenerable,
        overall_pass=passed, terminal_status=terminal, evaluated_at=evaluated_at)


def reduce_proposal_evaluations(*, ticket_id: UUID, run_id: UUID,
        section_results: list[SectionEvaluationResult], global_draft_id: UUID,
        global_iteration_count: int, global_compliance: GlobalComplianceEvaluation,
        evaluated_at: datetime) -> ProposalEvaluationResult:
    if not section_results:
        raise ValueError("proposal evaluation requires department evaluations")
    if any(r.ticket_id != ticket_id or r.run_id != run_id for r in section_results):
        raise ValueError("section evaluations belong to a different proposal")
    blocking = [f for f in global_compliance.findings if f.blocking]
    blocks = any(f.blocks_commercial_completeness for f in global_compliance.findings)
    human = any(f.requires_human_input for f in global_compliance.findings)
    all_passed = all(
        r.terminal_status == "passed" and r.overall_pass
        and not r.blocks_commercial_completeness
        and not r.requires_human_input and not r.blocking_rule_ids
        for r in section_results
    )
    passed = all_passed and global_compliance.passed and not blocks and not human
    regenerable = (not passed and all_passed and not human and bool(blocking)
                   and global_iteration_count < 3 and all(f.regenerable for f in blocking))
    outcome = "ready_for_part3" if passed else "regenerate" if regenerable else "needs_human_review"
    terminal = "ready_for_part3" if passed else "needs_human_input" if human else "iteration_limit_reached" if global_iteration_count == 3 else None
    return ProposalEvaluationResult(evaluation_id=uuid4(), ticket_id=ticket_id, run_id=run_id,
        section_evaluation_ids=[r.evaluation_id for r in section_results],
        global_draft_id=global_draft_id, global_iteration_count=global_iteration_count,
        global_compliance=global_compliance,
        all_departments_passed=all_passed, blocks_commercial_completeness=blocks,
        requires_human_input=human, blocking_rule_ids=[f.rule_id for f in blocking],
        regenerable=regenerable, overall_pass=passed, outcome=outcome,
        terminal_status=terminal,
        evaluated_at=evaluated_at)
