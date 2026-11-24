from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from threading import Barrier, Lock, current_thread
from uuid import uuid4

import pytest

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.compliance import evaluate_section_compliance
from data.pipelines.rfp_response.contracts import (
    DepartmentDraft,
    EvaluatorExecutionFailure,
    GlobalCommercialTermsDraft,
    ProposalEvaluationResult,
    ReadabilityEvaluation,
    ReadabilityMetric,
    RequestedAspect,
    RFPResponseHandoff,
    RFPResponseStart,
    SafePreview,
    SectionEvaluationResult,
)
from data.pipelines.rfp_response.evaluators import evaluate_relevance, required_terms
from data.pipelines.rfp_response.generators import (
    generate_department_draft,
    generate_global_terms,
)
from data.pipelines.rfp_response.graph import (
    DepartmentCheckpoint,
    GlobalCheckpoint,
    NonRetryableResponseOperation,
    RFPResponseGraph,
)


NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
HASH = "d" * 64


def request(*, departments=(DepartmentId.warehouse,), own_carrier=True,
            volume=20, country="US", currency="USD") -> RFPResponseStart:
    aspects = [RequestedAspect(
        aspect_id=f"{department.value}:01:1-2", department=department,
        fact=f"Client requests {department.value} service in scope.",
        requested_action=f"Provide a {department.value} response.",
        line_start=1, line_end=2) for department in departments]
    return RFPResponseStart(
        ticket_id=uuid4(), run_id=uuid4(), source_handoff_hash=HASH,
        summary_for_sales="Prepare the requested TrackFlow service response.",
        key_aspects=aspects, selected_departments=list(departments),
        client_country_code=country, operation_countries=[country],
        proposal_currency=currency, average_monthly_volume_m3=volume,
        customer_managed_carrier=own_carrier, language="en")


def passing_readability(_draft: DepartmentDraft) -> ReadabilityEvaluation:
    return ReadabilityEvaluation(
        execution_status="evaluated", language="en", readability_score=70,
        metrics=[ReadabilityMetric(name="flesch_reading_ease", value=70),
                 ReadabilityMetric(name="flesch_kincaid_grade", value=8),
                 ReadabilityMetric(name="gunning_fog", value=10),
                 ReadabilityMetric(name="average_sentence_words", value=12)],
        **{"pass": True})


class MemoryRepository:
    def __init__(self, item_request: RFPResponseStart) -> None:
        self.lock = Lock()
        self.departments = {
            department: DepartmentCheckpoint("pending", 0)
            for department in item_request.selected_departments
        }
        self.global_state = GlobalCheckpoint(0)
        self.failures: list[EvaluatorExecutionFailure] = []
        self.handoff: RFPResponseHandoff | None = None
        self.technical_failure = False
        self.draft_counts = {department: 0 for department in item_request.selected_departments}

    def department_checkpoint(self, department):
        with self.lock:
            return self.departments[department]

    def global_checkpoint(self):
        with self.lock:
            return self.global_state

    def persist_department_draft(self, draft, preview):
        with self.lock:
            current = self.departments[draft.department]
            assert draft.iteration_count == current.iteration_count + 1
            self.departments[draft.department] = DepartmentCheckpoint(
                "en_evaluación", draft.iteration_count, draft, preview, None)
            self.draft_counts[draft.department] += 1

    def persist_global_draft(self, draft, preview):
        with self.lock:
            assert draft.global_iteration_count == self.global_state.iteration_count + 1
            self.global_state = GlobalCheckpoint(
                draft.global_iteration_count, draft, preview, None)

    def persist_section_evaluation(self, result):
        with self.lock:
            current = self.departments[result.department]
            status = result.terminal_status or "generando_borrador"
            self.departments[result.department] = replace(
                current, status=status, evaluation=result)

    def persist_proposal_evaluation(self, result):
        with self.lock:
            self.global_state = replace(self.global_state, evaluation=result)

    def record_evaluator_failure(self, failure):
        with self.lock:
            self.failures.append(failure)
            if failure.terminal and failure.draft_id is not None:
                for department, checkpoint in self.departments.items():
                    if checkpoint.draft and checkpoint.draft.draft_id == failure.draft_id:
                        self.departments[department] = replace(
                            checkpoint, status="technical_failure")

    def mark_technical_failure(self):
        with self.lock:
            self.technical_failure = True

    def finalize_handoff(self, handoff):
        with self.lock:
            assert self.handoff is None
            self.handoff = handoff


def execute(item_request, repository, **kwargs):
    return RFPResponseGraph().execute(
        request=item_request, repository=repository,
        now=lambda: NOW, readability_evaluator=passing_readability, **kwargs)


def test_departments_and_section_evaluators_run_in_parallel_without_overwrite() -> None:
    item_request = request(departments=(DepartmentId.warehouse, DepartmentId.lastmile))
    repository = MemoryRepository(item_request)
    department_barrier = Barrier(2)
    evaluator_barrier = Barrier(3)
    department_threads: set[str] = set()
    evaluator_threads: set[str] = set()

    def generator(req, department, iteration, feedback, generated_at):
        department_threads.add(current_thread().name)
        department_barrier.wait(timeout=5)
        return generate_department_draft(
            req, department=department, iteration_count=iteration,
            generated_at=generated_at)

    def readability(draft):
        evaluator_threads.add(current_thread().name)
        evaluator_barrier.wait(timeout=5)
        return passing_readability(draft)

    def relevance(req, draft):
        evaluator_threads.add(current_thread().name)
        evaluator_barrier.wait(timeout=5)
        return evaluate_relevance(req, draft)

    def compliance(req, draft, preview):
        evaluator_threads.add(current_thread().name)
        evaluator_barrier.wait(timeout=5)
        return evaluate_section_compliance(
            draft, required_commercial_terms=required_terms(req, draft.department),
            preview=preview)

    result = RFPResponseGraph().execute(
        request=item_request, repository=repository, now=lambda: NOW,
        department_generator=generator, readability_evaluator=readability,
        relevance_evaluator=relevance, section_compliance_evaluator=compliance)

    assert result.handoff is not None and result.handoff.outcome == "ready_for_part3"
    assert [item.department for item in result.handoff.department_results] == [
        DepartmentId.warehouse, DepartmentId.lastmile]
    assert len(department_threads) == 2
    assert len(evaluator_threads) >= 3


def test_default_offline_components_complete_without_provider_or_tokenizer() -> None:
    item_request = request()
    repository = MemoryRepository(item_request)
    result = RFPResponseGraph().execute(
        request=item_request, repository=repository, now=lambda: NOW)
    assert result.technical_failure is False
    assert result.handoff is not None and result.handoff.outcome == "ready_for_part3"
    assert result.handoff.department_results[0].iteration_count == 1


def _bad_first_generator(calls, *, always_bad=False):
    def generator(req, department, iteration, feedback, generated_at):
        calls.append((department, iteration, tuple(feedback)))
        draft = generate_department_draft(
            req, department=department, iteration_count=iteration,
            generated_at=generated_at, generation_feedback=feedback)
        if always_bad or iteration == 1:
            content = "\n".join(line for line in draft.generated_content.splitlines()
                                if not line.startswith("TrackFlow response:"))
            return draft.model_copy(update={"generated_content": content,
                                            "aspect_responses": []})
        return draft
    return generator


def test_regenerable_failure_returns_feedback_and_uses_two_real_drafts() -> None:
    item_request = request()
    repository = MemoryRepository(item_request)
    calls = []
    result = execute(item_request, repository,
                     department_generator=_bad_first_generator(calls))
    assert result.handoff is not None and result.handoff.outcome == "ready_for_part3"
    assert repository.draft_counts[DepartmentId.warehouse] == 2
    assert calls[1][2] and "Address missing RFP aspect" in calls[1][2][0]
    assert repository.departments[DepartmentId.warehouse].draft.generation_feedback == list(
        calls[1][2])


def test_iteration_limit_is_exactly_three_real_drafts() -> None:
    item_request = request()
    repository = MemoryRepository(item_request)
    calls = []
    result = execute(item_request, repository,
                     department_generator=_bad_first_generator(calls, always_bad=True))
    assert result.handoff is not None and result.handoff.outcome == "needs_human_review"
    assert repository.draft_counts[DepartmentId.warehouse] == 3
    assert result.handoff.department_results[0].terminal_status == "iteration_limit_reached"


def test_missing_commercial_data_escalates_after_one_real_draft() -> None:
    item_request = request(departments=(DepartmentId.lastmile,), own_carrier=False)
    repository = MemoryRepository(item_request)
    result = execute(item_request, repository)
    assert result.handoff is not None and result.handoff.outcome == "needs_human_review"
    assert repository.draft_counts[DepartmentId.lastmile] == 1
    assert result.handoff.department_results[0].terminal_status == "needs_human_input"
    assert {item.code for item in result.handoff.unresolved_items} >= {
        "LAST_MILE_RATE_REQUIRED"}


@pytest.mark.parametrize(
    ("departments", "country", "currency", "rate_code", "summary"),
    [
        (
            (DepartmentId.warehouse, DepartmentId.reverse),
            "ES",
            "EUR",
            "RETURN_PROCESSING_RATE_REQUIRED",
            "ModaViva requests Warehouse and Reverse services in Spain.",
        ),
        (
            (DepartmentId.warehouse, DepartmentId.lastmile),
            "US",
            "USD",
            "LAST_MILE_RATE_REQUIRED",
            (
                "Luna Cosmetics reports approximately 5,000 orders per month; "
                "no conversion to cubic metres is documented."
            ),
        ),
    ],
)
def test_official_profiles_retain_real_drafts_and_handoff_for_indicative_review(
    departments, country, currency, rate_code, summary
) -> None:
    item_request = request(
        departments=departments,
        own_carrier=False,
        volume=None,
        country=country,
        currency=currency,
    ).model_copy(update={"summary_for_sales": summary})
    repository = MemoryRepository(item_request)

    result = execute(item_request, repository)

    assert result.technical_failure is False
    assert result.handoff is not None
    assert result.handoff.outcome == "needs_human_review"
    assert repository.draft_counts == {
        DepartmentId.warehouse: 1,
        departments[1]: 1,
    }
    assert repository.departments[DepartmentId.warehouse].evaluation.overall_pass is True
    service = repository.departments[departments[1]]
    assert service.evaluation.overall_pass is False
    assert service.evaluation.terminal_status == "needs_human_input"
    assert service.draft is not None
    assert rate_code in {
        item.code for item in service.draft.pending_commercial_items
    }
    assert f"Pending commercial input: {rate_code}." in service.draft.generated_content
    unresolved = {item.code for item in result.handoff.unresolved_items}
    assert rate_code in unresolved
    assert "MONTHLY_STORAGE_VOLUME_REQUIRED" in unresolved
    assert repository.global_state.draft.currency.value == currency
    assert repository.global_state.draft.volume_discounts[0].discount_percentage is None


def test_passed_department_is_not_restarted_while_other_department_regenerates() -> None:
    item_request = request(departments=(DepartmentId.warehouse, DepartmentId.lastmile))
    repository = MemoryRepository(item_request)
    calls = []

    def generator(req, department, iteration, feedback, generated_at):
        draft = _bad_first_generator(calls)(
            req, department, iteration, feedback, generated_at)
        return draft if department == DepartmentId.lastmile else generate_department_draft(
            req, department=department, iteration_count=iteration,
            generated_at=generated_at)

    result = execute(item_request, repository, department_generator=generator)
    assert result.handoff is not None and result.handoff.outcome == "ready_for_part3"
    assert repository.draft_counts == {
        DepartmentId.warehouse: 1, DepartmentId.lastmile: 2}


def test_recoverable_evaluator_failure_retries_same_draft_without_new_generation() -> None:
    item_request = request()
    repository = MemoryRepository(item_request)
    attempts = 0

    def flaky_relevance(req, draft):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient evaluator outage")
        return evaluate_relevance(req, draft)

    result = execute(item_request, repository, relevance_evaluator=flaky_relevance)
    assert result.handoff is not None and result.handoff.outcome == "ready_for_part3"
    assert repository.draft_counts[DepartmentId.warehouse] == 1
    assert attempts == 2
    assert len(repository.failures) == 1 and repository.failures[0].terminal is False


def test_terminal_evaluator_failure_preserves_draft_and_blocks_handoff() -> None:
    item_request = request()
    repository = MemoryRepository(item_request)

    def broken_relevance(_req, _draft):
        raise RuntimeError("evaluator unavailable")

    result = execute(item_request, repository, relevance_evaluator=broken_relevance)
    assert result.technical_failure is True and result.handoff is None
    assert repository.technical_failure is True
    assert repository.draft_counts[DepartmentId.warehouse] == 1
    assert len(repository.failures) == 3
    assert repository.failures[-1].terminal is True


def test_terminal_generator_failure_creates_no_draft_or_business_handoff() -> None:
    item_request = request()
    repository = MemoryRepository(item_request)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    result = execute(
        item_request, repository, department_generator=unavailable,
        evaluator_attempts=2,
    )

    assert result.technical_failure is True and result.handoff is None
    assert repository.technical_failure is True
    assert repository.draft_counts[DepartmentId.warehouse] == 0
    assert repository.departments[DepartmentId.warehouse].iteration_count == 0
    assert len(repository.failures) == 2
    assert repository.failures[-1].evaluator == "department_generator"
    assert repository.failures[-1].terminal is True
    assert repository.failures[-1].department == DepartmentId.warehouse


def test_non_retryable_contract_failure_stops_after_one_technical_attempt() -> None:
    item_request = request()
    repository = MemoryRepository(item_request)
    calls = 0

    def invalid_contract(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise NonRetryableResponseOperation("content-free contract failure")

    result = execute(
        item_request, repository, department_generator=invalid_contract,
        evaluator_attempts=3,
    )

    assert result.technical_failure is True and result.handoff is None
    assert calls == 1
    assert repository.draft_counts[DepartmentId.warehouse] == 0
    assert repository.departments[DepartmentId.warehouse].iteration_count == 0
    assert len(repository.failures) == 1
    assert repository.failures[0].terminal is True
    assert repository.failures[0].infrastructure_attempts == 1


def test_terminal_preview_inspection_failure_still_persists_internal_draft(monkeypatch) -> None:
    item_request = request()
    repository = MemoryRepository(item_request)
    monkeypatch.setattr(
        "data.pipelines.rfp_response.graph.inspect_department_safety",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("safety dependency unavailable")))
    result = execute(item_request, repository)
    checkpoint = repository.departments[DepartmentId.warehouse]
    assert result.technical_failure is True and result.handoff is None
    assert repository.draft_counts[DepartmentId.warehouse] == 1
    assert checkpoint.draft is not None and checkpoint.preview is not None
    assert checkpoint.preview.gate_status == "technical_error"
    assert checkpoint.preview.content is None


def test_recovery_evaluates_persisted_draft_without_generating_again() -> None:
    item_request = request()
    repository = MemoryRepository(item_request)
    draft = generate_department_draft(
        item_request, department=DepartmentId.warehouse,
        iteration_count=1, generated_at=NOW)
    from data.pipelines.rfp_response.evaluators import inspect_department_safety
    from data.pipelines.rfp_response.compliance import build_safe_preview
    preview = build_safe_preview(
        draft, inspect_department_safety(item_request, draft, inspected_at=NOW))
    repository.departments[DepartmentId.warehouse] = DepartmentCheckpoint(
        "en_evaluación", 1, draft, preview, None)
    repository.draft_counts[DepartmentId.warehouse] = 1

    def forbidden_generator(*_args):
        raise AssertionError("recovery must not create a second draft")

    result = execute(item_request, repository,
                     department_generator=forbidden_generator)
    assert result.handoff is not None and result.handoff.outcome == "ready_for_part3"
    assert repository.draft_counts[DepartmentId.warehouse] == 1


def test_recovery_after_terminal_global_evaluation_only_retries_synthesizer() -> None:
    item_request = request()

    class InterruptedRepository(MemoryRepository):
        interrupt = True

        def finalize_handoff(self, handoff):
            if self.interrupt:
                self.interrupt = False
                raise RuntimeError("simulated interruption before handoff commit")
            super().finalize_handoff(handoff)

    repository = InterruptedRepository(item_request)
    try:
        execute(item_request, repository)
    except RuntimeError as exc:
        assert "simulated interruption" in str(exc)
    else:
        raise AssertionError("the first synthesizer attempt must be interrupted")
    assert repository.global_state.iteration_count == 1
    assert repository.global_state.evaluation is not None

    result = execute(item_request, repository,
        global_generator=lambda *_args: (_ for _ in ()).throw(
            AssertionError("terminal global evaluation must be reused")))
    assert result.handoff is not None and result.handoff.outcome == "ready_for_part3"
    assert repository.global_state.iteration_count == 1
