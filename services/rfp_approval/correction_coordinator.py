"""Authenticated orchestration of one corrected department revision.

The public caller selects persisted commercial-input ids and supplies CAS data.
Content, artifacts, hashes, actor and status are derived server-side.  This
module never commits; the HTTP boundary owns the transaction and performs the
official conflict assessment before exposing an eligible revision to HITL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlmodel import Session

from data.pipelines.rfp_response.compliance import build_safe_preview
from data.pipelines.rfp_response.contracts import CommercialTerm, DepartmentDraft
from data.pipelines.rfp_response.evaluators import (
    AuthorizedCommercialCorrection,
    evaluate_department,
    inspect_department_safety,
)
from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from pipelines.rfp_approval.contracts import (
    ApprovalArtifactProvenance,
    ApprovalRevisionValidation,
    ApprovalValidationArtifact,
    ApprovalValidationBundle,
    AuthorizedHumanActor,
    ClientPriceQuoteInput,
    ContentEvaluationArtifactPayload,
    CorrectedDraftArtifact,
    DepartmentCorrectedDraftPayload,
    DepartmentRevision,
    ImmutableArtifactReference,
    RevisionValidationReferences,
    SafePreviewArtifactPayload,
    department_revision_content_hash,
    indicative_warnings_are_reviewable,
)
from services.rfp_approval.artifact_store import (
    ArtifactProducerContext,
    persist_corrected_draft,
    persist_validation_artifact,
    persist_validation_bundle,
)
from services.rfp_approval.authorization import AuthorizationReference
from services.rfp_approval.commercial_store import (
    DepartmentRevisionRecord,
    create_corrected_department_revision,
)
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.models import (
    rfp_approval_department_revisions,
    rfp_approval_runs,
    rfp_authorized_business_inputs,
)
from services.rfp_approval.store import ApprovalStoreError
from services.rfp_response.models import rfp_response_drafts
from services.rfp_response.store import load_response_start


_PRICE_TERM_BY_DEPARTMENT = {
    "lastmile": ("last_mile_rate", "LAST_MILE_RATE_REQUIRED"),
    "reverse": ("return_processing_rate", "RETURN_PROCESSING_RATE_REQUIRED"),
}


@dataclass(frozen=True, slots=True)
class CorrectedDepartmentResult:
    record: DepartmentRevisionRecord
    revision: DepartmentRevision
    remaining_input_codes: tuple[str, ...]


def _stable_id(approval_run_id: UUID, idempotency_key: UUID, label: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"trackflow:{approval_run_id}:department-correction:{idempotency_key}:{label}:v1",
    )


def _provenance(
    *,
    node: str,
    execution_id: UUID,
    source_kind: str,
    source_ids: list[UUID],
    source_hashes: list[str],
    produced_at: datetime,
) -> ApprovalArtifactProvenance:
    values = {
        "producer_node": node,
        "execution_id": execution_id,
        "source_kind": source_kind,
        "source_artifact_ids": source_ids,
        "source_hashes": source_hashes,
        "produced_at": produced_at,
    }
    return ApprovalArtifactProvenance(
        **values, provenance_hash=canonical_hash(values)
    )


def _price_text(amount: Decimal, currency: str, unit: str) -> str:
    normalized = format(amount, "f").rstrip("0").rstrip(".")
    return f"{normalized} {currency} per {unit}"


def _rehydrate_client_price_quote(payload: object) -> ClientPriceQuoteInput:
    """Restore one canonical JSONB payload without relaxing its domain model."""

    if type(payload) is not dict:
        raise ValueError("persisted client quote must be a JSON object")
    expected_fields = {
        "input_kind",
        "department",
        "service_code",
        "amount",
        "currency",
        "unit",
        "effective_from",
        "effective_until",
    }
    if set(payload) != expected_fields:
        raise ValueError("persisted client quote fields are not canonical")
    text_fields = {
        "input_kind",
        "department",
        "service_code",
        "amount",
        "currency",
        "unit",
        "effective_from",
    }
    if any(type(payload[field]) is not str for field in text_fields):
        raise ValueError("persisted client quote has a non-text canonical field")
    if payload["effective_until"] is not None and type(
        payload["effective_until"]
    ) is not str:
        raise ValueError("persisted quote end must be an ISO date string or null")
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    restored = ClientPriceQuoteInput.model_validate_json(serialized)
    if not restored.amount.is_finite() or canonical_value(restored) != payload:
        raise ValueError("persisted client quote is not canonical")
    return restored


def _load_selected_inputs(
    session: Session,
    *,
    approval_run_id: UUID,
    ticket_id: UUID,
    department: str,
    input_ids: tuple[UUID, ...],
    identity: ResolvedIdentity,
    requested_at: datetime,
) -> list[tuple[UUID, str, ClientPriceQuoteInput]]:
    if not input_ids or len(input_ids) != len(set(input_ids)):
        raise ApprovalStoreError("authorized_input_selection_invalid")
    rows = session.execute(
        select(rfp_authorized_business_inputs).where(
            rfp_authorized_business_inputs.c.input_id.in_(input_ids),
            rfp_authorized_business_inputs.c.approval_run_id == approval_run_id,
            rfp_authorized_business_inputs.c.ticket_id == ticket_id,
            rfp_authorized_business_inputs.c.environment == identity.environment,
        )
    ).mappings().all()
    by_id = {row["input_id"]: row for row in rows}
    if set(by_id) != set(input_ids):
        raise ApprovalStoreError("authorized_input_not_found_or_scope_mismatch")
    selected: list[tuple[UUID, str, ClientPriceQuoteInput]] = []
    for input_id in input_ids:
        row = by_id[input_id]
        if (
            row["department_scope"] != department
            or row["actor_principal_id"] != identity.principal_id
            or row["input_kind"] != "client_price_quote"
        ):
            raise ApprovalStoreError("authorized_input_not_applicable_to_correction")
        try:
            payload = _rehydrate_client_price_quote(row["payload"])
        except (TypeError, ValueError) as error:
            raise ApprovalStoreError("authorized_input_payload_invalid") from error
        if payload.department.value != department:
            raise ApprovalStoreError("authorized_input_not_applicable_to_correction")
        if requested_at.date() < payload.effective_from or (
            payload.effective_until is not None
            and requested_at.date() > payload.effective_until
        ):
            raise ApprovalStoreError("authorized_input_not_effective")
        selected.append((input_id, row["payload_hash"], payload))
    return selected


def _apply_price_inputs(
    *,
    predecessor: dict[str, object],
    selected: list[tuple[UUID, str, ClientPriceQuoteInput]],
) -> tuple[str, list[CommercialTerm], list[object], list[object]]:
    department = str(predecessor["department_id"])
    mapping = _PRICE_TERM_BY_DEPARTMENT.get(department)
    if mapping is None:
        raise ApprovalStoreError("department_correction_kind_not_supported")
    term_id, pending_code = mapping
    payload = predecessor["content_payload"]
    try:
        terms = [CommercialTerm.model_validate(item) for item in payload["commercial_terms"]]
        pending = list(payload["pending_commercial_items"])
        evidence = list(payload["evidence"])
    except (KeyError, TypeError, ValueError) as error:
        raise ApprovalStoreError("predecessor_content_payload_invalid") from error
    if len(selected) != 1 or selected[0][2].service_code != term_id:
        raise ApprovalStoreError("authorized_input_service_code_mismatch")
    input_id, input_hash, quote = selected[0]
    reference = {
        "document_id": "authorized-business-input-v1",
        "section_id": str(input_id),
        "evidence_hash": input_hash,
    }
    found = False
    corrected_terms: list[CommercialTerm] = []
    for term in terms:
        if term.term_id != term_id:
            corrected_terms.append(term)
            continue
        found = True
        corrected_terms.append(
            CommercialTerm(
                term_id=term_id,
                status="documented",
                value=_price_text(quote.amount, quote.currency, quote.unit),
                currency=quote.currency,
                evidence=[reference],
            )
        )
    if not found:
        raise ApprovalStoreError("correction_target_term_not_found")
    remaining = [item for item in pending if item.get("code") != pending_code]
    if len(remaining) == len(pending) and predecessor["status"] == "blocked_input":
        raise ApprovalStoreError("correction_pending_code_not_found")
    evidence_keys = {
        (item["document_id"], item["section_id"], item["evidence_hash"])
        for item in evidence
    }
    key = (reference["document_id"], reference["section_id"], reference["evidence_hash"])
    if key not in evidence_keys:
        evidence.append(reference)
    statement = (
        f"Authorized commercial correction: {term_id} is "
        f"{_price_text(quote.amount, quote.currency, quote.unit)}."
    )
    resolved_markers = {
        f"Pending commercial input: {pending_code}.",
        f"Dato comercial pendiente: {pending_code}.",
    }
    retained_lines = [
        line for line in str(payload["content"]).splitlines()
        if line.strip() not in resolved_markers
    ]
    base_content = "\n".join(retained_lines).rstrip()
    content = f"{base_content}\n\n{statement}" if base_content else statement
    return content, corrected_terms, remaining, evidence


def _authorized_correction_context(
    selected: list[tuple[UUID, str, ClientPriceQuoteInput]],
) -> tuple[AuthorizedCommercialCorrection, ...]:
    return tuple(
        AuthorizedCommercialCorrection(
            input_id=input_id,
            payload_hash=input_hash,
            department=quote.department,
            term_id=quote.service_code,
            pending_code=_PRICE_TERM_BY_DEPARTMENT[quote.department.value][1],
            value=_price_text(quote.amount, quote.currency, quote.unit),
            currency=quote.currency,
            statement=(
                f"Authorized commercial correction: {quote.service_code} is "
                f"{_price_text(quote.amount, quote.currency, quote.unit)}."
            ),
        )
        for input_id, input_hash, quote in selected
    )


def create_corrected_department_from_inputs(
    session: Session,
    *,
    approval_run_id: UUID,
    ticket_id: UUID,
    department: str,
    predecessor_revision_id: UUID,
    expected_predecessor_status: str,
    expected_predecessor_content_hash: str,
    expected_predecessor_state_version: int,
    authorized_input_ids: tuple[UUID, ...],
    identity: ResolvedIdentity,
    authorization_reference: AuthorizationReference,
    actor: AuthorizedHumanActor,
    idempotency_key: UUID,
    requested_at: datetime,
) -> CorrectedDepartmentResult:
    """Build real artifacts and supersede one exact predecessor atomically."""

    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ApprovalStoreError("requested_at_timezone_required")
    predecessor_row = session.execute(
        select(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id == predecessor_revision_id,
            rfp_approval_department_revisions.c.approval_run_id == approval_run_id,
            rfp_approval_department_revisions.c.ticket_id == ticket_id,
            rfp_approval_department_revisions.c.department_id == department,
        )
    ).mappings().one_or_none()
    if predecessor_row is None:
        raise ApprovalStoreError("predecessor_revision_not_found")
    predecessor = dict(predecessor_row)
    if predecessor["review_revision_no"] >= 3:
        raise ApprovalStoreError("department_revision_limit_reached")
    if expected_predecessor_status not in {"blocked_input", "changes_requested"}:
        raise ApprovalStoreError("predecessor_status_not_correctable")
    run = session.execute(
        select(rfp_approval_runs).where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
            rfp_approval_runs.c.environment == identity.environment,
        )
    ).mappings().one_or_none()
    if run is None or run["status"] != "active":
        raise ApprovalStoreError("approval_run_not_active")
    selected = _load_selected_inputs(
        session,
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        department=department,
        input_ids=authorized_input_ids,
        identity=identity,
        requested_at=requested_at,
    )
    content, terms, pending, evidence = _apply_price_inputs(
        predecessor=predecessor, selected=selected
    )
    authorized_corrections = _authorized_correction_context(selected)
    source_row = session.execute(
        select(rfp_response_drafts.c.payload).where(
            rfp_response_drafts.c.draft_id == predecessor["source_part2_draft_id"],
            rfp_response_drafts.c.run_id == run["response_run_id"],
        )
    ).mappings().one_or_none()
    if source_row is None:
        raise ApprovalStoreError("part2_department_draft_not_found")
    try:
        source = DepartmentDraft.model_validate_json(json.dumps(source_row["payload"]))
        draft_id = _stable_id(approval_run_id, idempotency_key, "draft")
        evaluated_draft = DepartmentDraft.model_validate(
            {
                **source.model_dump(mode="python"),
                "draft_id": draft_id,
                "generated_content": content,
                "commercial_terms": terms,
                "pending_commercial_items": pending,
                "draft_status": (
                    "provisional_missing_inputs" if pending else "complete_candidate"
                ),
                "generated_at": requested_at,
            }
        )
        request = load_response_start(
            session, run_id=run["response_run_id"], language=source.language
        )
        evaluation, _feedback = evaluate_department(
            request,
            evaluated_draft,
            evaluated_at=requested_at,
            authorized_corrections=authorized_corrections,
        )
        inspection = inspect_department_safety(
            request,
            evaluated_draft,
            inspected_at=requested_at,
            authorized_corrections=authorized_corrections,
        )
        preview = build_safe_preview(evaluated_draft, inspection)
    except (TypeError, ValueError) as error:
        raise ApprovalStoreError("corrected_department_evaluation_failed") from error

    input_ids = [item[0] for item in selected]
    input_hashes = [item[1] for item in selected]
    execution_id = _stable_id(approval_run_id, idempotency_key, "execution")
    node = "create_corrected_department_revision"
    content_hash = department_revision_content_hash(
        content=content,
        commercial_terms=terms,
        pending_commercial_items=pending,
        evidence=evidence,
    )
    corrected_payload = DepartmentCorrectedDraftPayload(
        department=source.department,
        content=content,
        commercial_terms=terms,
        pending_commercial_items=pending,
        evidence=evidence,
    )
    corrected = CorrectedDraftArtifact(
        draft_artifact_id=draft_id,
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        scope_key=department,
        payload=corrected_payload,
        content_hash=content_hash,
        payload_hash=canonical_hash(corrected_payload),
        provenance=_provenance(
            node=node,
            execution_id=execution_id,
            source_kind="human_correction",
            source_ids=input_ids,
            source_hashes=input_hashes,
            produced_at=requested_at,
        ),
        created_at=requested_at,
    )
    pending_codes = [item["code"] for item in pending]
    reviewable_warnings = indicative_warnings_are_reviewable(
        blocking_rule_ids=evaluation.blocking_rule_ids,
        unresolved_input_codes=pending_codes,
    )
    mandatory_compliance_passed = (
        evaluation.compliance.passed
        and evaluation.readability.passed is not False
        and evaluation.relevance.passed
        and preview.gate_status == "passed"
    )
    evaluation_payload = ContentEvaluationArtifactPayload(
        mandatory_compliance_passed=mandatory_compliance_passed,
        blocks_commercial_completeness=evaluation.blocks_commercial_completeness,
        blocking_rule_ids=evaluation.blocking_rule_ids,
        unresolved_input_codes=pending_codes,
        open_conflict_ids=[],
        eligible=mandatory_compliance_passed and reviewable_warnings,
    )
    preview_payload = SafePreviewArtifactPayload(
        gate_status=preview.gate_status,
        rendered_content=preview.content,
        rendered_content_hash=(
            canonical_hash({"rendered_content": preview.content})
            if preview.content is not None
            else None
        ),
        reason_codes=preview.reason_codes,
    )
    evaluation_id = _stable_id(approval_run_id, idempotency_key, "evaluation")
    preview_id = _stable_id(approval_run_id, idempotency_key, "preview")
    evaluation_provenance = _provenance(
        node=node,
        execution_id=execution_id,
        source_kind="deterministic_evaluator",
        source_ids=[draft_id],
        source_hashes=[content_hash],
        produced_at=requested_at,
    )
    preview_provenance = _provenance(
        node=node,
        execution_id=execution_id,
        source_kind="compliance_evaluator",
        source_ids=[draft_id],
        source_hashes=[content_hash],
        produced_at=requested_at,
    )
    evaluation_artifact = ApprovalValidationArtifact(
        artifact_id=evaluation_id,
        artifact_type="evaluation",
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        scope_key=department,
        corrected_draft_id=draft_id,
        evaluated_content_hash=content_hash,
        payload=evaluation_payload,
        payload_hash=canonical_hash(evaluation_payload),
        provenance=evaluation_provenance,
        created_at=requested_at,
    )
    preview_artifact = ApprovalValidationArtifact(
        artifact_id=preview_id,
        artifact_type="safe_preview",
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        scope_key=department,
        corrected_draft_id=draft_id,
        evaluated_content_hash=content_hash,
        payload=preview_payload,
        payload_hash=canonical_hash(preview_payload),
        provenance=preview_provenance,
        created_at=requested_at,
    )
    evaluation_ref = ImmutableArtifactReference(
        artifact_type="evaluation",
        artifact_id=evaluation_id,
        contract_version=evaluation_artifact.contract_version,
        payload_hash=evaluation_artifact.payload_hash,
        evaluated_content_hash=content_hash,
    )
    preview_ref = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=preview_id,
        contract_version=preview_artifact.contract_version,
        payload_hash=preview_artifact.payload_hash,
        evaluated_content_hash=content_hash,
    )
    reference_values = {
        "revision_content_hash": content_hash,
        "evaluation": evaluation_ref,
        "safe_preview": preview_ref,
        "validated_at": requested_at,
    }
    validation_hash = canonical_hash(reference_values)
    validation = ApprovalRevisionValidation(
        references=RevisionValidationReferences(
            **reference_values, validation_hash=validation_hash
        ),
        mandatory_compliance_passed=evaluation_payload.mandatory_compliance_passed,
        blocks_commercial_completeness=evaluation_payload.blocks_commercial_completeness,
        blocking_rule_ids=evaluation_payload.blocking_rule_ids,
        unresolved_input_codes=evaluation_payload.unresolved_input_codes,
        open_conflict_ids=[],
        global_revalidation_required=True,
        eligible_for_approval=evaluation_payload.eligible,
    )
    bundle_provenance = _provenance(
        node=node,
        execution_id=execution_id,
        source_kind="deterministic_evaluator",
        source_ids=[draft_id, evaluation_id, preview_id],
        source_hashes=[
            content_hash,
            evaluation_artifact.payload_hash,
            preview_artifact.payload_hash,
        ],
        produced_at=requested_at,
    )
    bundle_values = {
        "approval_run_id": approval_run_id,
        "ticket_id": ticket_id,
        "scope_key": department,
        "corrected_draft_id": draft_id,
        "content_hash": content_hash,
        "evaluation_artifact_id": evaluation_id,
        "evaluation_artifact_type": "evaluation",
        "evaluation_contract_version": evaluation_artifact.contract_version,
        "evaluation_payload_hash": evaluation_artifact.payload_hash,
        "safe_preview_artifact_id": preview_id,
        "safe_preview_contract_version": preview_artifact.contract_version,
        "safe_preview_payload_hash": preview_artifact.payload_hash,
        "revision_validation_hash": validation_hash,
        "mandatory_compliance_passed": evaluation_payload.mandatory_compliance_passed,
        "blocks_commercial_completeness": evaluation_payload.blocks_commercial_completeness,
        "blocking_rule_ids": evaluation_payload.blocking_rule_ids,
        "unresolved_input_codes": evaluation_payload.unresolved_input_codes,
        "open_conflict_ids": [],
        "source_departments_approved": False,
        "eligible": evaluation_payload.eligible,
        "provenance": bundle_provenance,
        "validated_at": requested_at,
    }
    bundle = ApprovalValidationBundle(
        validation_bundle_id=_stable_id(approval_run_id, idempotency_key, "bundle"),
        **bundle_values,
        bundle_hash=canonical_hash(bundle_values),
    )
    if not evaluation_payload.eligible:
        revision_status = "blocked_compliance"
    else:
        revision_status = "pending_approval"
    revision = DepartmentRevision(
        revision_id=_stable_id(approval_run_id, idempotency_key, "revision"),
        draft_id=draft_id,
        ticket_id=ticket_id,
        approval_run_id=approval_run_id,
        department=source.department,
        review_revision_no=predecessor["review_revision_no"] + 1,
        source_part2_draft_id=predecessor["source_part2_draft_id"],
        source_part2_draft_hash=predecessor["source_part2_draft_hash"],
        source_part2_evaluation_id=predecessor["source_part2_evaluation_id"],
        source_part2_evaluation_hash=predecessor["source_part2_evaluation_hash"],
        source_part2_iteration=predecessor["source_part2_iteration"],
        supersedes_revision_id=predecessor_revision_id,
        content=content,
        content_hash=content_hash,
        commercial_terms=terms,
        pending_commercial_items=pending,
        evidence=evidence,
        validation=validation,
        validation_bundle_id=bundle.validation_bundle_id,
        status=revision_status,
        created_by=actor,
        creation_reason=(
            "human_request_changes"
            if expected_predecessor_status == "changes_requested"
            else "authorized_input"
        ),
        created_at=requested_at,
    )
    producer = ArtifactProducerContext._from_verified_orchestrator(
        environment=identity.environment,
        producer_node=node,
        execution_id=execution_id,
    )
    persist_corrected_draft(session, draft=corrected, producer=producer)
    persist_validation_artifact(
        session, artifact=evaluation_artifact, producer=producer
    )
    persist_validation_artifact(session, artifact=preview_artifact, producer=producer)
    persist_validation_bundle(session, bundle=bundle, producer=producer)
    record = create_corrected_department_revision(
        session,
        revision=revision,
        expected_predecessor_status=expected_predecessor_status,
        expected_predecessor_content_hash=expected_predecessor_content_hash,
        expected_predecessor_state_version=expected_predecessor_state_version,
        identity=identity,
        authorization_reference=authorization_reference,
        idempotency_key=idempotency_key,
        execution_id=execution_id,
    )
    return CorrectedDepartmentResult(
        record=record,
        revision=revision,
        remaining_input_codes=tuple(pending_codes),
    )
