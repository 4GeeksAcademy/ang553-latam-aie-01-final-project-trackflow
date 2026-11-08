"""Immutable stage checkpoints for interrupted holdout evaluations.

The checkpoint layer is evaluation instrumentation. It does not change the
frozen retrieval or generation implementation. Successful external work may be
reused only after its artifact, question and frozen-system bindings validate.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from services.rag.chunking import load_knowledge_chunks
from services.rag.contracts import (
    KnowledgeChunk,
    RetrievalCandidateTrace,
    RetrievalResult,
    RetrievalTrace,
)


CheckpointStage = Literal[
    "embedding_completed",
    "retrieval_completed",
    "generation_completed",
    "fidelity_adjudicated",
]
STAGE_ORDER: tuple[CheckpointStage, ...] = (
    "embedding_completed",
    "retrieval_completed",
    "generation_completed",
    "fidelity_adjudicated",
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def question_sha256(question: str) -> str:
    return sha256_bytes(question.encode("utf-8"))


def validate_freeze_manifest(manifest_path: Path, repository_root: Path) -> dict[str, Any]:
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) != 29:
        raise ValueError("freeze manifest must contain the expected 29 files")
    corpus_hashes: dict[str, str] = {}
    for entry in entries:
        relative = str(entry["path"])
        expected = str(entry["sha256"])
        path = repository_root / relative
        if sha256_file(path) != expected:
            raise ValueError("frozen file hash mismatch")
        if entry.get("role") == "canonical_corpus":
            corpus_hashes[relative] = expected
    if len(corpus_hashes) != 4:
        raise ValueError("freeze manifest must bind the four corpus documents")
    return {
        "freeze_manifest_sha256": sha256_bytes(raw),
        "corpus_sha256": corpus_hashes,
    }


def build_checkpoint_binding(
    *,
    system_version: str,
    evaluation_version: str,
    holdout_sha256: str,
    rubric_sha256: str,
    binding_sha256: str,
    freeze_manifest_sha256: str,
    corpus_sha256: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "system_version": system_version,
        "evaluation_version": evaluation_version,
        "holdout_sha256": holdout_sha256,
        "rubric_sha256": rubric_sha256,
        "binding_sha256": binding_sha256,
        "freeze_manifest_sha256": freeze_manifest_sha256,
        "corpus_sha256": dict(sorted(corpus_sha256.items())),
    }


class CheckpointStore:
    def __init__(self, root: Path, binding: Mapping[str, Any]) -> None:
        self.root = root
        self.binding = dict(binding)

    def path(self, case_id: str, stage: CheckpointStage) -> Path:
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
        if not case_id or any(char not in allowed for char in case_id):
            raise ValueError("checkpoint case id is invalid")
        return self.root / f"{case_id}.{stage}.json"

    def existing_stages(self, case_id: str) -> tuple[CheckpointStage, ...]:
        return tuple(stage for stage in STAGE_ORDER if self.path(case_id, stage).is_file())

    def write(
        self,
        *,
        case_id: str,
        split: str,
        question: str,
        stage: CheckpointStage,
        payload: Mapping[str, Any],
        attempt_number: int,
        previous_path: Path | None = None,
    ) -> Path:
        if stage not in STAGE_ORDER:
            raise ValueError("unsupported checkpoint stage")
        if attempt_number <= 0:
            raise ValueError("checkpoint attempt number must be positive")
        target = self.path(case_id, stage)
        pending = target.with_suffix(target.suffix + ".pending")
        if target.exists() or pending.exists():
            raise ValueError("refusing to overwrite an evaluation checkpoint")
        stage_index = STAGE_ORDER.index(stage)
        if stage_index == 0:
            if previous_path is not None:
                raise ValueError("embedding checkpoint cannot have a predecessor")
            previous_sha256 = None
        else:
            expected_previous = self.path(case_id, STAGE_ORDER[stage_index - 1])
            if previous_path != expected_previous or not expected_previous.is_file():
                raise ValueError("checkpoint predecessor is missing")
            previous_sha256 = sha256_file(expected_previous)
        artifact = {
            "schema_version": 1,
            "status": "completed",
            "case_id": case_id,
            "split": split,
            "question_sha256": question_sha256(question),
            "stage": stage,
            "attempt_number": attempt_number,
            "created_at": datetime.now(UTC).isoformat(),
            "binding": self.binding,
            "previous_checkpoint_sha256": previous_sha256,
            "payload": dict(payload),
        }
        self._validate_payload(stage, artifact["payload"])
        self.root.mkdir(parents=True, exist_ok=True)
        pending.write_text(
            json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        pending.replace(target)
        return target

    def load(
        self,
        *,
        case_id: str,
        split: str,
        question: str,
        stage: CheckpointStage,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        path = self.path(case_id, stage)
        if expected_sha256 is not None and sha256_file(path) != expected_sha256:
            raise ValueError("checkpoint artifact hash mismatch")
        artifact = json.loads(path.read_text(encoding="utf-8"))
        if artifact.get("schema_version") != 1 or artifact.get("status") != "completed":
            raise ValueError("checkpoint artifact is incomplete")
        if artifact.get("case_id") != case_id or artifact.get("split") != split:
            raise ValueError("checkpoint case binding mismatch")
        if artifact.get("question_sha256") != question_sha256(question):
            raise ValueError("checkpoint question binding mismatch")
        if artifact.get("stage") != stage or artifact.get("binding") != self.binding:
            raise ValueError("checkpoint frozen-system binding mismatch")
        stage_index = STAGE_ORDER.index(stage)
        if stage_index == 0:
            if artifact.get("previous_checkpoint_sha256") is not None:
                raise ValueError("embedding checkpoint has an unexpected predecessor")
        else:
            previous = self.path(case_id, STAGE_ORDER[stage_index - 1])
            if not previous.is_file() or artifact.get(
                "previous_checkpoint_sha256"
            ) != sha256_file(previous):
                raise ValueError("checkpoint predecessor hash mismatch")
        payload = artifact.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("checkpoint payload is invalid")
        self._validate_payload(stage, payload)
        return artifact

    @staticmethod
    def _validate_payload(stage: CheckpointStage, payload: Mapping[str, Any]) -> None:
        if stage == "embedding_completed":
            vector = payload.get("vector")
            if not isinstance(vector, list) or not vector:
                raise ValueError("embedding checkpoint vector is missing")
            if not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in vector
            ):
                raise ValueError("embedding checkpoint vector is invalid")
            if payload.get("vector_size") != len(vector):
                raise ValueError("embedding checkpoint vector size differs")
        elif stage == "retrieval_completed":
            candidates = payload.get("candidates")
            if not isinstance(candidates, list):
                raise ValueError("retrieval checkpoint candidates are missing")
        elif stage == "generation_completed":
            answer = payload.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("generation checkpoint answer is missing")
        elif stage == "fidelity_adjudicated":
            if payload.get("verdict") not in {"PASS", "FAIL"}:
                raise ValueError("fidelity checkpoint verdict is invalid")


def next_stage(completed_stages: Sequence[CheckpointStage]) -> str:
    present = set(completed_stages)
    for index, stage in enumerate(STAGE_ORDER):
        if stage not in present:
            if any(later in present for later in STAGE_ORDER[index + 1 :]):
                raise ValueError("checkpoint stage sequence has a gap")
            return stage
    return "case_completed"


def maximum_external_requests_for_resume(
    *, next_case_stage: str, remaining_cases_after_current: int
) -> dict[str, int]:
    if remaining_cases_after_current < 0:
        raise ValueError("remaining case count must not be negative")
    current_embedding = int(next_case_stage == "embedding_completed")
    current_generation = int(
        next_case_stage
        in {"embedding_completed", "retrieval_completed", "generation_completed"}
    )
    embedding = current_embedding + remaining_cases_after_current
    generation = current_generation + remaining_cases_after_current
    return {
        "embedding_requests_maximum": embedding,
        "generation_requests_maximum": generation,
        "external_requests_maximum": embedding + generation,
    }


def serialize_trace(trace: RetrievalTrace) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "point_id": candidate.chunk.point_id,
                "source_document": candidate.chunk.source_document,
                "section": candidate.chunk.section,
                "chunk_index": candidate.chunk.chunk_index,
                "text_sha256": sha256_bytes(candidate.chunk.text.encode("utf-8")),
                "score": candidate.score,
                "retrieval_kind": candidate.retrieval_kind,
                "evidence_basis": candidate.evidence_basis,
            }
            for candidate in trace.candidates
        ]
    }


def deserialize_trace(payload: Mapping[str, Any]) -> RetrievalTrace:
    canonical: dict[str, KnowledgeChunk] = {
        chunk.point_id: chunk for chunk in load_knowledge_chunks()
    }
    candidates: list[RetrievalCandidateTrace] = []
    selected: list[RetrievalResult] = []
    for item in payload.get("candidates", []):
        if not isinstance(item, Mapping):
            raise ValueError("retrieval checkpoint candidate is invalid")
        chunk = canonical.get(str(item.get("point_id")))
        if chunk is None:
            raise ValueError("retrieval checkpoint references an unknown chunk")
        if (
            item.get("source_document") != chunk.source_document
            or item.get("section") != chunk.section
            or item.get("chunk_index") != chunk.chunk_index
            or item.get("text_sha256")
            != sha256_bytes(chunk.text.encode("utf-8"))
        ):
            raise ValueError("retrieval checkpoint chunk binding mismatch")
        score = float(item["score"])
        if not math.isfinite(score):
            raise ValueError("retrieval checkpoint score is invalid")
        retrieval_kind = str(item["retrieval_kind"])
        evidence_basis = str(item["evidence_basis"])
        if retrieval_kind not in {"primary", "complementary", "excluded"}:
            raise ValueError("retrieval checkpoint kind is invalid")
        if evidence_basis not in {"semantic", "lexical", "none"}:
            raise ValueError("retrieval checkpoint evidence is invalid")
        candidate = RetrievalCandidateTrace(  # type: ignore[arg-type]
            chunk, score, retrieval_kind, evidence_basis
        )
        candidates.append(candidate)
        if retrieval_kind != "excluded":
            selected.append(
                RetrievalResult(  # type: ignore[arg-type]
                    chunk, score, retrieval_kind=retrieval_kind
                )
            )
    return RetrievalTrace(tuple(selected), tuple(candidates))
