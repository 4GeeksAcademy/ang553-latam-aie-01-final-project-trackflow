"""Offline contracts for continuing the immutable interrupted RAG evaluation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipelines.eval.rag.evaluation_checkpoints import (
    CheckpointStore,
    maximum_external_requests_for_resume,
    next_stage,
    serialize_trace,
    sha256_file,
)
from pipelines.eval.rag.resume_independent_holdout import (
    _CheckpointedEmbeddingProvider,
    _CheckpointedEvaluationService,
    DEFAULT_PARENT,
    EXPECTED_PARENT_SHA256,
    MAX_NEW_EMBEDDING_REQUESTS,
    MAX_NEW_EXTERNAL_REQUESTS,
    MAX_NEW_GENERATION_REQUESTS,
    ensure_output_available,
    validate_checkpoint_authorization_scope,
    validate_resume_authorization,
    validate_resume_preflight,
)
from pipelines.eval.rag.run_independent_holdout import calculate_metrics
from services.rag.chunking import load_knowledge_chunks
from services.rag.contracts import (
    RAGProviderError,
    RetrievalCandidateTrace,
    RetrievalResult,
    RetrievalTrace,
)


def _authorization() -> dict[str, object]:
    preflight = validate_resume_preflight()
    return {
        "schema_version": 1,
        "approved": True,
        "execution_kind": "resume_interrupted_holdout_v2.1",
        "parent_result_sha256": EXPECTED_PARENT_SHA256,
        "binding_sha256": preflight["binding_sha256"],
        "maximum_one_execution": True,
        "authorized_by": "Attributable TrackFlow reviewer",
        "authorized_at": "2026-11-08T00:00:00Z",
        "budget": {
            "embedding_requests_maximum": MAX_NEW_EMBEDDING_REQUESTS,
            "generation_requests_maximum": MAX_NEW_GENERATION_REQUESTS,
            "external_requests_maximum": MAX_NEW_EXTERNAL_REQUESTS,
            "minimum_interval_seconds": 2.0,
            "automatic_retries": 0,
        },
    }


def test_resume_preflight_preserves_parent_and_skips_only_completed_cases():
    preflight = validate_resume_preflight()

    assert preflight["parent_sha256"] == EXPECTED_PARENT_SHA256
    assert preflight["resume_case_id"] == "ihv2-p03"
    assert preflight["skipped_completed_case_ids"] == ["ihv2-p01", "ihv2-p02"]
    assert preflight["remaining_cases"] == 14
    assert preflight["maximum_new_embedding_requests"] == 14
    assert preflight["maximum_new_generation_requests"] == 14
    assert preflight["maximum_new_external_requests"] == 28


def test_resume_authorization_is_hash_bound_attributable_and_budget_limited(tmp_path):
    preflight = validate_resume_preflight()
    record = _authorization()
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    assert validate_resume_authorization(
        path,
        parent_sha256=preflight["parent_sha256"],
        binding_sha256=preflight["binding_sha256"],
    )["approved"] is True

    record["authorized_by"] = None
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="not attributable"):
        validate_resume_authorization(
            path,
            parent_sha256=preflight["parent_sha256"],
            binding_sha256=preflight["binding_sha256"],
        )


def test_resume_authorization_rejects_wrong_parent_or_budget(tmp_path):
    preflight = validate_resume_preflight()
    record = _authorization()
    record["parent_result_sha256"] = "0" * 64
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="different parent"):
        validate_resume_authorization(
            path,
            parent_sha256=preflight["parent_sha256"],
            binding_sha256=preflight["binding_sha256"],
        )

    record = _authorization()
    record["budget"]["generation_requests_maximum"] = 15  # type: ignore[index]
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected budget"):
        validate_resume_authorization(
            path,
            parent_sha256=preflight["parent_sha256"],
            binding_sha256=preflight["binding_sha256"],
        )


def _checkpoint_binding() -> dict[str, object]:
    return {
        "system_version": "rag-hybrid-v3",
        "evaluation_version": "holdout-v2.1",
        "holdout_sha256": "1" * 64,
        "rubric_sha256": "2" * 64,
        "binding_sha256": "3" * 64,
        "freeze_manifest_sha256": "4" * 64,
        "corpus_sha256": {"document": "5" * 64},
    }


def test_resume_output_lifecycle_is_checked_against_the_requested_path(tmp_path):
    output = tmp_path / "future-attempt.json"
    ensure_output_available(output)

    output.write_text("immutable", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        ensure_output_available(output)

    parent = Path("pipelines/eval/rag/independent-holdout-v2.1-results.json")
    attempt_two = Path(
        "pipelines/eval/rag/independent-holdout-v2.1-resume-01-results.json"
    )
    assert parent.is_file() and attempt_two.is_file()
    assert sha256_file(parent) == EXPECTED_PARENT_SHA256
    assert parent.resolve() != attempt_two.resolve()


def test_embedding_timeout_creates_no_reusable_checkpoint(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoints", _checkpoint_binding())

    class Gateway:
        embedding_request_count = 0
        embedding_usage: dict[str, int] = {}

        def embed(self, _text: str):
            self.embedding_request_count += 1
            raise RAGProviderError("sanitized", category="timeout")

    provider = _CheckpointedEmbeddingProvider(
        Gateway(),  # type: ignore[arg-type]
        store,
        case_id="ihv2-p03",
        split="positive",
        question="sealed question",
        attempt_number=3,
        model="embedding-model",
    )
    with pytest.raises(RAGProviderError, match="sanitized"):
        provider.embed("sealed question")

    assert store.existing_stages("ihv2-p03") == ()
    assert next_stage(store.existing_stages("ihv2-p03")) == "embedding_completed"


def test_generation_timeout_reuses_retrieval_without_another_embedding(tmp_path):
    question = "sealed question"
    store = CheckpointStore(tmp_path / "checkpoints", _checkpoint_binding())
    embedding = store.write(
        case_id="ihv2-p03",
        split="positive",
        question=question,
        stage="embedding_completed",
        payload={"vector": [0.1, 0.2], "vector_size": 2},
        attempt_number=3,
    )
    chunk = load_knowledge_chunks()[0]
    trace = RetrievalTrace(
        (RetrievalResult(chunk, 0.7),),
        (RetrievalCandidateTrace(chunk, 0.7, "primary", "semantic"),),
    )
    store.write(
        case_id="ihv2-p03",
        split="positive",
        question=question,
        stage="retrieval_completed",
        payload=serialize_trace(trace),
        attempt_number=3,
        previous_path=embedding,
    )

    class FailingService:
        def retrieve_with_trace(self, *_args, **_kwargs):
            raise AssertionError("stored retrieval must be reused")

        def generate_answer(self, *_args, **_kwargs):
            raise RAGProviderError("sanitized", category="timeout")

    class Gateway:
        generation_usage: dict[str, int] = {}

    service = _CheckpointedEvaluationService(
        FailingService(),  # type: ignore[arg-type]
        store,
        Gateway(),  # type: ignore[arg-type]
        case_id="ihv2-p03",
        split="positive",
        question=question,
        attempt_number=4,
        generation_model="generation-model",
    )
    recovered = service.retrieve_with_trace(question, k=3, min_score=0.57)
    assert recovered == trace
    with pytest.raises(RAGProviderError, match="sanitized"):
        service.generate_answer(question, recovered.selected)

    assert store.existing_stages("ihv2-p03") == (
        "embedding_completed",
        "retrieval_completed",
    )
    assert next_stage(store.existing_stages("ihv2-p03")) == "generation_completed"
    assert maximum_external_requests_for_resume(
        next_case_stage="generation_completed", remaining_cases_after_current=0
    ) == {
        "embedding_requests_maximum": 0,
        "generation_requests_maximum": 1,
        "external_requests_maximum": 1,
    }


def test_checkpoint_hash_and_predecessor_chain_detect_tampering(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoints", _checkpoint_binding())
    path = store.write(
        case_id="ihv2-p04",
        split="positive",
        question="sealed question",
        stage="embedding_completed",
        payload={"vector": [0.1], "vector_size": 1},
        attempt_number=3,
    )
    expected = sha256_file(path)
    store.load(
        case_id="ihv2-p04",
        split="positive",
        question="sealed question",
        stage="embedding_completed",
        expected_sha256=expected,
    )
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.load(
            case_id="ihv2-p04",
            split="positive",
            question="sealed question",
            stage="embedding_completed",
            expected_sha256=expected,
        )


def test_request_budget_is_reduced_only_by_verified_completed_stages():
    assert maximum_external_requests_for_resume(
        next_case_stage="embedding_completed", remaining_cases_after_current=13
    ) == {
        "embedding_requests_maximum": 14,
        "generation_requests_maximum": 14,
        "external_requests_maximum": 28,
    }
    assert maximum_external_requests_for_resume(
        next_case_stage="retrieval_completed", remaining_cases_after_current=13
    )["external_requests_maximum"] == 27


def test_later_attempt_authorization_binds_output_and_checkpoint_hashes():
    # The production gate restricts these paths to the evaluation directory.
    evaluation_root = Path("pipelines/eval/rag").resolve()
    checkpoint_directory = evaluation_root / "test-checkpoint-scope"
    output = evaluation_root / "test-resume-scope.json"
    store = CheckpointStore(checkpoint_directory, _checkpoint_binding())
    authorization = {
        "attempt_number": 3,
        "output_artifact": output.name,
        "checkpoint_directory": checkpoint_directory.name,
        "reusable_checkpoint_sha256": {},
        "prior_attempt_sha256": {
            DEFAULT_PARENT.name: sha256_file(DEFAULT_PARENT),
            **{
                path.name: sha256_file(path)
                for path in sorted(
                    evaluation_root.glob(
                        "independent-holdout-v2.1-resume-*-results.json"
                    )
                )
            },
        },
        "budget": {
            "embedding_requests_maximum": 0,
            "generation_requests_maximum": 0,
            "external_requests_maximum": 0,
            "minimum_interval_seconds": 2.0,
            "automatic_retries": 0,
        },
    }
    try:
        validate_checkpoint_authorization_scope(
            authorization,
            output_path=output,
            checkpoint_directory=checkpoint_directory,
            checkpoint_store=store,
            remaining_cases=[],
        )
        authorization["reusable_checkpoint_sha256"] = {"unexpected.json": "0" * 64}
        with pytest.raises(ValueError, match="inventory differs"):
            validate_checkpoint_authorization_scope(
                authorization,
                output_path=output,
                checkpoint_directory=checkpoint_directory,
                checkpoint_store=store,
                remaining_cases=[],
            )
    finally:
        # No file or directory is created by the read-only inventory check.
        assert not checkpoint_directory.exists()


def test_technical_errors_are_excluded_from_abstention_metrics():
    metrics = calculate_metrics(
        [
            {
                "id": "ihv2-n01",
                "split": "negative",
                "status": "completed",
                "final_abstention": True,
            },
            {
                "id": "ihv2-n02",
                "split": "negative",
                "status": "technical_error",
                "stage": "retrieval",
                "error_category": "timeout",
            },
        ]
    )
    assert metrics["completed_cases"] == 1
    assert metrics["negative_cases"] == 1
    assert metrics["negative_abstention_rate"] == 1.0
    assert metrics["negative_false_positives"] == 0
