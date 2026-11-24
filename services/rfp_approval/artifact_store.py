"""Transactional persistence for immutable Part 3 approval artifacts.

All functions participate in the caller's transaction and never commit or
roll back.  ``ArtifactProducerContext`` is an internal adapter boundary, not an
authentication mechanism: a future authenticated orchestrator must create it
only after verifying the calling workload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from pipelines.rfp_approval.contracts import (
    ApprovalArtifactProvenance,
    ApprovalValidationArtifact,
    ApprovalValidationBundle,
    ContentEvaluationArtifactPayload,
    CorrectedDraftArtifact,
    ProposalCorrectedDraftPayload,
    SafePreviewArtifactPayload,
    proposal_revision_content_hash,
)
from services.rfp_approval.models import (
    rfp_approval_corrected_drafts,
    rfp_approval_department_revisions,
    rfp_approval_runs,
    rfp_approval_validation_artifacts,
    rfp_approval_validation_bundles,
    rfp_workflow_events,
)
from services.rfp_approval.store import ApprovalStoreError


Environment = Literal["production", "qa"]
_PRODUCER_CONTEXT_SEAL = object()
ContractT = TypeVar(
    "ContractT",
    CorrectedDraftArtifact,
    ApprovalValidationArtifact,
    ApprovalValidationBundle,
)


@dataclass(frozen=True, slots=True)
class ArtifactProducerContext:
    """Internal workload identity already verified by an upstream adapter.

    The underscored factory deliberately states the trust boundary.  Python
    possession of this object is not cryptographic proof and this type must not
    be constructed from public request fields.
    """

    environment: Environment
    producer_node: str
    execution_id: UUID
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _PRODUCER_CONTEXT_SEAL:
            raise ApprovalStoreError("unverified_artifact_producer")
        if self.environment not in {"production", "qa"}:
            raise ApprovalStoreError("artifact_producer_environment_invalid")
        if not self.producer_node.strip() or len(self.producer_node) > 200:
            raise ApprovalStoreError("artifact_producer_node_invalid")

    @classmethod
    def _from_verified_orchestrator(
        cls,
        *,
        environment: Environment,
        producer_node: str,
        execution_id: UUID,
    ) -> "ArtifactProducerContext":
        return cls(environment, producer_node, execution_id, _PRODUCER_CONTEXT_SEAL)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: UUID
    approval_run_id: UUID
    artifact_type: str
    content_hash: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class ValidationBundleRecord:
    validation_bundle_id: UUID
    approval_run_id: UUID
    scope_key: str
    content_hash: str
    eligible: bool
    replayed: bool


def _revalidate_contract(value: ContractT, contract_type: type[ContractT]) -> ContractT:
    """Re-run all Pydantic invariants over the current in-memory envelope.

    The contracts deliberately are not frozen.  Reconstructing from the dumped
    value prevents post-construction mutation from bypassing canonical hashes
    or cross-field invariants at the persistence boundary.
    """

    try:
        return contract_type.model_validate(value.model_dump(mode="python"))
    except ValueError as error:
        raise ApprovalStoreError("artifact_contract_invalid") from error


def _assert_producer(
    provenance: ApprovalArtifactProvenance,
    producer: ArtifactProducerContext,
) -> None:
    if (
        provenance.producer_node != producer.producer_node
        or provenance.execution_id != producer.execution_id
    ):
        raise ApprovalStoreError("artifact_producer_provenance_mismatch")
    if provenance.provenance_hash != canonical_hash(
        {
            "producer_node": provenance.producer_node,
            "execution_id": provenance.execution_id,
            "source_kind": provenance.source_kind,
            "source_artifact_ids": provenance.source_artifact_ids,
            "source_hashes": provenance.source_hashes,
            "produced_at": provenance.produced_at,
        }
    ):
        raise ApprovalStoreError("artifact_provenance_hash_mismatch")


def _assert_provenance_pair(
    provenance: ApprovalArtifactProvenance,
    artifact_id: UUID,
    artifact_hash: str,
    code: str,
) -> None:
    pairs = dict(zip(provenance.source_artifact_ids, provenance.source_hashes))
    if pairs.get(artifact_id) != artifact_hash:
        raise ApprovalStoreError(code)


def _lock_active_run(
    session: Session,
    *,
    approval_run_id: UUID,
    ticket_id: UUID,
    producer: ArtifactProducerContext,
) -> dict[str, Any]:
    row = session.execute(
        select(rfp_approval_runs)
        .where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
            rfp_approval_runs.c.environment == producer.environment,
        )
        .with_for_update(key_share=True)
    ).mappings().one_or_none()
    if row is None:
        raise ApprovalStoreError("approval_run_not_found_or_environment_mismatch")
    result = dict(row)
    if (
        result["approval_run_id"] != approval_run_id
        or result["ticket_id"] != ticket_id
        or result["environment"] != producer.environment
    ):
        raise ApprovalStoreError("approval_run_not_found_or_environment_mismatch")
    if result["status"] != "active":
        raise ApprovalStoreError("approval_run_not_active")
    return result


def _assert_scope_active(run: dict[str, Any], scope_key: str) -> None:
    if scope_key == "proposal":
        return
    if scope_key not in set(run["active_departments"]):
        raise ApprovalStoreError("artifact_scope_not_active")


def _assert_exact_row(row: dict[str, Any], expected: dict[str, Any], code: str) -> None:
    if any(row.get(key) != value for key, value in expected.items()):
        raise ApprovalStoreError(code)


def _audit(
    session: Session,
    *,
    event_key: str,
    run_id: UUID,
    ticket_id: UUID,
    thread_id: str,
    producer: ArtifactProducerContext,
    node_name: str,
    event_type: str,
    safe_input: dict[str, Any],
    safe_output: dict[str, Any],
) -> None:
    deterministic = {
        "event_key": event_key,
        "approval_run_id": run_id,
        "ticket_id": ticket_id,
        "thread_id": thread_id,
        "execution_id": producer.execution_id,
        "sequence_no": 1,
        "node_name": node_name,
        "agent_name": "rfp_approval_artifact_store",
        "event_type": event_type,
        "safe_input_ref": canonical_value(safe_input),
        "safe_output_ref": canonical_value(safe_output),
        "result": "persisted",
        "from_state": None,
        "to_state": None,
        "department_revision_id": None,
        "proposal_revision_id": None,
        "decision_id": None,
        "conflict_id": None,
        "actor_principal_id": None,
        "actor_binding_id": None,
        "authorization_id": None,
        "authorization_version": None,
        "environment": None,
    }
    inserted = session.execute(
        pg_insert(rfp_workflow_events)
        .values(
            event_id=uuid4(),
            **deterministic,
            started_at=func.clock_timestamp(),
            finished_at=func.clock_timestamp(),
        )
        .on_conflict_do_nothing(index_elements=[rfp_workflow_events.c.event_key])
        .returning(rfp_workflow_events.c.event_id)
    ).mappings().one_or_none()
    if inserted is not None:
        return
    existing = session.execute(
        select(rfp_workflow_events)
        .where(rfp_workflow_events.c.event_key == event_key)
        .with_for_update(key_share=True)
    ).mappings().one_or_none()
    if existing is None:
        raise ApprovalStoreError("artifact_audit_event_insert_failed")
    if any(existing.get(key) != value for key, value in deterministic.items()):
        raise ApprovalStoreError("artifact_audit_event_collision")


def _insert_or_replay(
    session: Session,
    *,
    table,
    primary_key,
    primary_value: UUID,
    values: dict[str, Any],
    collision_code: str,
) -> tuple[dict[str, Any], bool]:
    inserted = session.execute(
        pg_insert(table)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[primary_key])
        .returning(table)
    ).mappings().one_or_none()
    if inserted is not None:
        return dict(inserted), False
    concurrent = session.execute(
        select(table)
        .where(primary_key == primary_value)
        .with_for_update(key_share=True)
    ).mappings().one_or_none()
    if concurrent is None:
        raise ApprovalStoreError("artifact_concurrent_insert_missing")
    row = dict(concurrent)
    _assert_exact_row(row, values, collision_code)
    return row, True


def persist_corrected_draft(
    session: Session,
    *,
    draft: CorrectedDraftArtifact,
    producer: ArtifactProducerContext,
) -> ArtifactRecord:
    """Persist or exactly replay one corrected-content envelope."""

    draft = _revalidate_contract(draft, CorrectedDraftArtifact)
    _assert_producer(draft.provenance, producer)
    if draft.provenance.source_kind not in {"part2_import", "human_correction"}:
        raise ApprovalStoreError("corrected_draft_provenance_kind_invalid")
    if draft.payload_hash != canonical_hash(draft.payload):
        raise ApprovalStoreError("corrected_draft_payload_hash_mismatch")
    run = _lock_active_run(
        session,
        approval_run_id=draft.approval_run_id,
        ticket_id=draft.ticket_id,
        producer=producer,
    )
    _assert_scope_active(run, draft.scope_key)
    values = {
        "draft_artifact_id": draft.draft_artifact_id,
        "approval_run_id": draft.approval_run_id,
        "ticket_id": draft.ticket_id,
        "scope_key": draft.scope_key,
        "contract_version": draft.contract_version,
        "content_payload": canonical_value(draft.payload),
        "content_hash": draft.content_hash,
        "payload_hash": draft.payload_hash,
        "provenance": canonical_value(draft.provenance),
        "provenance_hash": draft.provenance.provenance_hash,
        "producer_node": producer.producer_node,
        "execution_id": producer.execution_id,
        "produced_at": draft.provenance.produced_at,
        "created_at": draft.created_at,
    }
    _row, replayed = _insert_or_replay(
        session,
        table=rfp_approval_corrected_drafts,
        primary_key=rfp_approval_corrected_drafts.c.draft_artifact_id,
        primary_value=draft.draft_artifact_id,
        values=values,
        collision_code="corrected_draft_id_collision",
    )
    _audit(
        session,
        event_key=f"artifact:draft:{draft.draft_artifact_id}",
        run_id=draft.approval_run_id,
        ticket_id=draft.ticket_id,
        thread_id=f"artifact:draft:{draft.draft_artifact_id}",
        producer=producer,
        node_name="persist_corrected_draft",
        event_type="corrected_draft_persisted",
        safe_input={
            "draft_artifact_id": draft.draft_artifact_id,
            "scope_key": draft.scope_key,
            "contract_version": draft.contract_version,
            "content_hash": draft.content_hash,
            "payload_hash": draft.payload_hash,
            "provenance_hash": draft.provenance.provenance_hash,
        },
        safe_output={"draft_artifact_id": draft.draft_artifact_id},
    )
    return ArtifactRecord(
        draft.draft_artifact_id,
        draft.approval_run_id,
        "corrected_draft",
        draft.content_hash,
        replayed,
    )


def persist_validation_artifact(
    session: Session,
    *,
    artifact: ApprovalValidationArtifact,
    producer: ArtifactProducerContext,
) -> ArtifactRecord:
    """Persist an evaluation or safe preview bound to one corrected draft."""

    artifact = _revalidate_contract(artifact, ApprovalValidationArtifact)
    _assert_producer(artifact.provenance, producer)
    if artifact.provenance.source_kind not in {
        "part2_import",
        "deterministic_evaluator",
        "compliance_evaluator",
    }:
        raise ApprovalStoreError("validation_artifact_provenance_kind_invalid")
    if artifact.payload_hash != canonical_hash(artifact.payload):
        raise ApprovalStoreError("validation_artifact_payload_hash_mismatch")
    run = _lock_active_run(
        session,
        approval_run_id=artifact.approval_run_id,
        ticket_id=artifact.ticket_id,
        producer=producer,
    )
    _assert_scope_active(run, artifact.scope_key)
    draft = session.execute(
        select(rfp_approval_corrected_drafts)
        .where(
            rfp_approval_corrected_drafts.c.draft_artifact_id
            == artifact.corrected_draft_id,
            rfp_approval_corrected_drafts.c.approval_run_id
            == artifact.approval_run_id,
            rfp_approval_corrected_drafts.c.ticket_id == artifact.ticket_id,
            rfp_approval_corrected_drafts.c.scope_key == artifact.scope_key,
            rfp_approval_corrected_drafts.c.content_hash
            == artifact.evaluated_content_hash,
        )
        .with_for_update(key_share=True)
    ).mappings().one_or_none()
    if draft is None:
        raise ApprovalStoreError("validation_artifact_draft_mismatch")
    _assert_provenance_pair(
        artifact.provenance,
        artifact.corrected_draft_id,
        artifact.evaluated_content_hash,
        "validation_artifact_provenance_draft_mismatch",
    )
    values = {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "approval_run_id": artifact.approval_run_id,
        "ticket_id": artifact.ticket_id,
        "scope_key": artifact.scope_key,
        "corrected_draft_id": artifact.corrected_draft_id,
        "evaluated_content_hash": artifact.evaluated_content_hash,
        "contract_version": artifact.contract_version,
        "payload": canonical_value(artifact.payload),
        "payload_hash": artifact.payload_hash,
        "provenance": canonical_value(artifact.provenance),
        "provenance_hash": artifact.provenance.provenance_hash,
        "producer_node": producer.producer_node,
        "execution_id": producer.execution_id,
        "produced_at": artifact.provenance.produced_at,
        "created_at": artifact.created_at,
    }
    _row, replayed = _insert_or_replay(
        session,
        table=rfp_approval_validation_artifacts,
        primary_key=rfp_approval_validation_artifacts.c.artifact_id,
        primary_value=artifact.artifact_id,
        values=values,
        collision_code="validation_artifact_id_collision",
    )
    _audit(
        session,
        event_key=f"artifact:validation:{artifact.artifact_id}",
        run_id=artifact.approval_run_id,
        ticket_id=artifact.ticket_id,
        thread_id=f"artifact:validation:{artifact.artifact_id}",
        producer=producer,
        node_name="persist_validation_artifact",
        event_type="validation_artifact_persisted",
        safe_input={
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type,
            "contract_version": artifact.contract_version,
            "corrected_draft_id": artifact.corrected_draft_id,
            "content_hash": artifact.evaluated_content_hash,
            "payload_hash": artifact.payload_hash,
            "provenance_hash": artifact.provenance.provenance_hash,
        },
        safe_output={"artifact_id": artifact.artifact_id},
    )
    return ArtifactRecord(
        artifact.artifact_id,
        artifact.approval_run_id,
        artifact.artifact_type,
        artifact.evaluated_content_hash,
        replayed,
    )


def _lock_department_approval_state(
    session: Session,
    run: dict[str, Any],
    source_revision_ids: list[UUID],
) -> bool:
    expected = set(run["active_departments"])
    if (
        len(source_revision_ids) != len(expected)
        or len(set(source_revision_ids)) != len(source_revision_ids)
    ):
        raise ApprovalStoreError("global_draft_source_revision_scope_mismatch")
    rows = session.execute(
        select(
            rfp_approval_department_revisions.c.revision_id,
            rfp_approval_department_revisions.c.approval_run_id,
            rfp_approval_department_revisions.c.ticket_id,
            rfp_approval_department_revisions.c.department_id,
            rfp_approval_department_revisions.c.status,
            rfp_approval_department_revisions.c.is_current,
        )
        .where(
            rfp_approval_department_revisions.c.approval_run_id
            == run["approval_run_id"],
            rfp_approval_department_revisions.c.ticket_id == run["ticket_id"],
            rfp_approval_department_revisions.c.department_id.in_(expected),
            rfp_approval_department_revisions.c.is_current.is_(True),
        )
        .order_by(
            rfp_approval_department_revisions.c.department_id,
            rfp_approval_department_revisions.c.revision_id,
        )
        .with_for_update(key_share=True)
    ).mappings().all()
    normalized = [dict(row) for row in rows]
    departments = {row["department_id"] for row in normalized}
    revision_ids = {row["revision_id"] for row in normalized}
    if (
        len(normalized) != len(expected)
        or departments != expected
        or revision_ids != set(source_revision_ids)
        or any(
            row["approval_run_id"] != run["approval_run_id"]
            or row["ticket_id"] != run["ticket_id"]
            or not row["is_current"]
            for row in normalized
        )
    ):
        raise ApprovalStoreError("global_draft_source_revision_mismatch")
    return all(row["status"] == "approved" for row in normalized)


def _proposal_draft_sources(draft: dict[str, Any]) -> list[UUID]:
    try:
        payload = ProposalCorrectedDraftPayload.model_validate_json(
            json.dumps(draft["content_payload"])
        )
    except (KeyError, ValueError) as error:
        raise ApprovalStoreError("persisted_global_draft_payload_invalid") from error
    if (
        canonical_hash(payload) != draft["payload_hash"]
        or proposal_revision_content_hash(
            currency=payload.currency,
            sla=payload.sla,
            volume_discounts=payload.volume_discounts,
            source_department_revision_ids=payload.source_department_revision_ids,
            authorized_input_ids=payload.authorized_input_ids,
        )
        != draft["content_hash"]
    ):
        raise ApprovalStoreError("persisted_global_draft_hash_mismatch")
    return list(payload.source_department_revision_ids)


def _artifact_rows_by_id(
    session: Session, evaluation_id: UUID, preview_id: UUID
) -> dict[UUID, dict[str, Any]]:
    rows = session.execute(
        select(rfp_approval_validation_artifacts)
        .where(
            rfp_approval_validation_artifacts.c.artifact_id.in_(
                {evaluation_id, preview_id}
            )
        )
        .order_by(rfp_approval_validation_artifacts.c.artifact_id)
        .with_for_update(key_share=True)
    ).mappings().all()
    return {row["artifact_id"]: dict(row) for row in rows}


def _assert_bundle_artifact_identity(
    bundle: ApprovalValidationBundle,
    row: dict[str, Any],
    *,
    expected_id: UUID,
    expected_type: str,
    expected_contract_version: str,
    expected_payload_hash: str,
) -> None:
    expected = {
        "artifact_id": expected_id,
        "artifact_type": expected_type,
        "approval_run_id": bundle.approval_run_id,
        "ticket_id": bundle.ticket_id,
        "scope_key": bundle.scope_key,
        "corrected_draft_id": bundle.corrected_draft_id,
        "evaluated_content_hash": bundle.content_hash,
        "contract_version": expected_contract_version,
        "payload_hash": expected_payload_hash,
    }
    _assert_exact_row(row, expected, "validation_bundle_artifact_mismatch")
    if canonical_hash(row["payload"]) != row["payload_hash"]:
        raise ApprovalStoreError("persisted_validation_artifact_hash_mismatch")


def _assert_bundle_summary(
    bundle: ApprovalValidationBundle,
    evaluation: ContentEvaluationArtifactPayload,
    preview: SafePreviewArtifactPayload,
    departments_approved: bool,
) -> None:
    summary = (
        bundle.mandatory_compliance_passed,
        bundle.blocks_commercial_completeness,
        canonical_value(bundle.blocking_rule_ids),
        canonical_value(bundle.unresolved_input_codes),
        canonical_value(bundle.open_conflict_ids),
    )
    evaluation_summary = (
        evaluation.mandatory_compliance_passed,
        evaluation.blocks_commercial_completeness,
        canonical_value(evaluation.blocking_rule_ids),
        canonical_value(evaluation.unresolved_input_codes),
        canonical_value(evaluation.open_conflict_ids),
    )
    if summary != evaluation_summary:
        raise ApprovalStoreError("validation_bundle_evaluation_summary_mismatch")
    if bundle.scope_key == "proposal":
        if bundle.source_departments_approved != departments_approved:
            raise ApprovalStoreError("validation_bundle_department_state_mismatch")
    elif bundle.source_departments_approved:
        raise ApprovalStoreError("department_bundle_claims_global_approval")
    actually_eligible = (
        evaluation.eligible
        and preview.gate_status == "passed"
        and (bundle.scope_key != "proposal" or departments_approved)
    )
    if bundle.eligible != actually_eligible:
        if bundle.eligible and preview.gate_status != "passed":
            raise ApprovalStoreError("eligible_bundle_preview_not_passed")
        raise ApprovalStoreError("validation_bundle_eligibility_mismatch")


def persist_validation_bundle(
    session: Session,
    *,
    bundle: ApprovalValidationBundle,
    producer: ArtifactProducerContext,
) -> ValidationBundleRecord:
    """Persist a semantically resolved evaluation/preview bundle."""

    bundle = _revalidate_contract(bundle, ApprovalValidationBundle)
    _assert_producer(bundle.provenance, producer)
    if bundle.provenance.source_kind not in {
        "part2_import",
        "deterministic_evaluator",
        "compliance_evaluator",
    }:
        raise ApprovalStoreError("validation_bundle_provenance_kind_invalid")
    run = _lock_active_run(
        session,
        approval_run_id=bundle.approval_run_id,
        ticket_id=bundle.ticket_id,
        producer=producer,
    )
    _assert_scope_active(run, bundle.scope_key)
    departments_approved = False
    draft = session.execute(
        select(rfp_approval_corrected_drafts)
        .where(
            rfp_approval_corrected_drafts.c.draft_artifact_id
            == bundle.corrected_draft_id,
            rfp_approval_corrected_drafts.c.approval_run_id
            == bundle.approval_run_id,
            rfp_approval_corrected_drafts.c.ticket_id == bundle.ticket_id,
            rfp_approval_corrected_drafts.c.scope_key == bundle.scope_key,
            rfp_approval_corrected_drafts.c.content_hash == bundle.content_hash,
        )
        .with_for_update(key_share=True)
    ).mappings().one_or_none()
    if draft is None:
        raise ApprovalStoreError("validation_bundle_draft_mismatch")
    draft = dict(draft)
    if bundle.scope_key == "proposal":
        departments_approved = _lock_department_approval_state(
            session,
            run,
            _proposal_draft_sources(draft),
        )
    rows = _artifact_rows_by_id(
        session, bundle.evaluation_artifact_id, bundle.safe_preview_artifact_id
    )
    evaluation_row = rows.get(bundle.evaluation_artifact_id)
    preview_row = rows.get(bundle.safe_preview_artifact_id)
    if evaluation_row is None or preview_row is None:
        raise ApprovalStoreError("validation_bundle_artifact_missing")
    _assert_bundle_artifact_identity(
        bundle,
        evaluation_row,
        expected_id=bundle.evaluation_artifact_id,
        expected_type=bundle.evaluation_artifact_type,
        expected_contract_version=bundle.evaluation_contract_version,
        expected_payload_hash=bundle.evaluation_payload_hash,
    )
    _assert_bundle_artifact_identity(
        bundle,
        preview_row,
        expected_id=bundle.safe_preview_artifact_id,
        expected_type="safe_preview",
        expected_contract_version=bundle.safe_preview_contract_version,
        expected_payload_hash=bundle.safe_preview_payload_hash,
    )
    try:
        evaluation = ContentEvaluationArtifactPayload.model_validate_json(
            json.dumps(evaluation_row["payload"])
        )
        preview = SafePreviewArtifactPayload.model_validate_json(
            json.dumps(preview_row["payload"])
        )
    except (TypeError, ValueError) as error:
        raise ApprovalStoreError("persisted_validation_artifact_invalid") from error
    _assert_bundle_summary(bundle, evaluation, preview, departments_approved)
    _assert_provenance_pair(
        bundle.provenance,
        bundle.corrected_draft_id,
        bundle.content_hash,
        "validation_bundle_provenance_draft_mismatch",
    )
    _assert_provenance_pair(
        bundle.provenance,
        bundle.evaluation_artifact_id,
        bundle.evaluation_payload_hash,
        "validation_bundle_provenance_evaluation_mismatch",
    )
    _assert_provenance_pair(
        bundle.provenance,
        bundle.safe_preview_artifact_id,
        bundle.safe_preview_payload_hash,
        "validation_bundle_provenance_preview_mismatch",
    )
    values = {
        "validation_bundle_id": bundle.validation_bundle_id,
        "contract_version": bundle.contract_version,
        "approval_run_id": bundle.approval_run_id,
        "ticket_id": bundle.ticket_id,
        "scope_key": bundle.scope_key,
        "corrected_draft_id": bundle.corrected_draft_id,
        "content_hash": bundle.content_hash,
        "evaluation_artifact_id": bundle.evaluation_artifact_id,
        "evaluation_artifact_type": bundle.evaluation_artifact_type,
        "evaluation_contract_version": bundle.evaluation_contract_version,
        "evaluation_payload_hash": bundle.evaluation_payload_hash,
        "safe_preview_artifact_id": bundle.safe_preview_artifact_id,
        "safe_preview_artifact_type": "safe_preview",
        "safe_preview_contract_version": bundle.safe_preview_contract_version,
        "safe_preview_payload_hash": bundle.safe_preview_payload_hash,
        "revision_validation_hash": bundle.revision_validation_hash,
        "mandatory_compliance_passed": bundle.mandatory_compliance_passed,
        "blocks_commercial_completeness": bundle.blocks_commercial_completeness,
        "blocking_rule_ids": canonical_value(bundle.blocking_rule_ids),
        "unresolved_input_codes": canonical_value(bundle.unresolved_input_codes),
        "open_conflict_ids": canonical_value(bundle.open_conflict_ids),
        "source_departments_approved": bundle.source_departments_approved,
        "eligible": bundle.eligible,
        "provenance": canonical_value(bundle.provenance),
        "provenance_hash": bundle.provenance.provenance_hash,
        "bundle_hash": bundle.bundle_hash,
        "validated_at": bundle.validated_at,
    }
    _row, replayed = _insert_or_replay(
        session,
        table=rfp_approval_validation_bundles,
        primary_key=rfp_approval_validation_bundles.c.validation_bundle_id,
        primary_value=bundle.validation_bundle_id,
        values=values,
        collision_code="validation_bundle_id_collision",
    )
    _audit(
        session,
        event_key=f"artifact:bundle:{bundle.validation_bundle_id}",
        run_id=bundle.approval_run_id,
        ticket_id=bundle.ticket_id,
        thread_id=f"artifact:bundle:{bundle.validation_bundle_id}",
        producer=producer,
        node_name="persist_validation_bundle",
        event_type="validation_bundle_persisted",
        safe_input={
            "validation_bundle_id": bundle.validation_bundle_id,
            "scope_key": bundle.scope_key,
            "contract_version": bundle.contract_version,
            "corrected_draft_id": bundle.corrected_draft_id,
            "content_hash": bundle.content_hash,
            "evaluation_artifact_id": bundle.evaluation_artifact_id,
            "evaluation_payload_hash": bundle.evaluation_payload_hash,
            "safe_preview_artifact_id": bundle.safe_preview_artifact_id,
            "safe_preview_payload_hash": bundle.safe_preview_payload_hash,
            "revision_validation_hash": bundle.revision_validation_hash,
            "bundle_hash": bundle.bundle_hash,
        },
        safe_output={
            "validation_bundle_id": bundle.validation_bundle_id,
            "eligible": bundle.eligible,
        },
    )
    return ValidationBundleRecord(
        bundle.validation_bundle_id,
        bundle.approval_run_id,
        bundle.scope_key,
        bundle.content_hash,
        bundle.eligible,
        replayed,
    )
