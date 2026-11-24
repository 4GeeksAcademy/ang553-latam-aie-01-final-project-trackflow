"""Derive and persist the three official pre-approval conflict assessments.

Facts come only from persisted Parte 1 metadata, hash-verified Parte 2 artifacts
and immutable authorized business inputs.  The caller owns the transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, func, or_, select
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.compliance import extract_return_processing_hours
from data.pipelines.rfp_response.contracts import (
    DepartmentDraft,
    GlobalCommercialTermsDraft,
)
from data.pipelines.rfp_response.hashing import canonical_hash
from pipelines.rfp_approval.contracts import (
    ArbitrationConflict,
    ArbitrationTarget,
    BusinessInputProvenance,
    ConflictAssessment,
    CurrencyMismatchFacts,
    MonetaryTermFact,
    MonthlyStorageVolumeInput,
    QuantityFact,
    QuantityScope,
    ReturnSLAViolation,
    ReturnsSLAFacts,
    VolumeCapacityFacts,
    WarehouseCapacityInput,
    arbitration_conflict_fingerprint,
    conflict_assessment_hash,
)
from services.rfp_approval.arbitration import persist_conflict_assessment
from services.rfp_approval.arbitration import _deserialize_assessed_content_hashes
from services.rfp_approval.authorization import (
    AuthorizationReference,
    AuthorizationTarget,
    EffectiveAuthorization,
    resolve_effective_authorization,
)
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.models import (
    rfp_approval_department_revisions,
    rfp_approval_runs,
    rfp_approval_validation_bundles,
    rfp_authorizations,
    rfp_authorized_business_inputs,
    rfp_conflict_assessments,
    rfp_identity_bindings,
)
from services.rfp_approval.proposal_coordinator import _load_part2_sources
from services.rfp_approval.run_coordinator import _load_department_source
from services.rfp_approval.store import (
    ApprovalStoreError,
    _persisted_revision_creation_reason,
    link_initial_validation_bundle,
)
from services.rfp_intake.models import rfp_metadata


@dataclass(frozen=True, slots=True)
class InitialAssessmentResult:
    approval_run_id: UUID
    outcomes: tuple[tuple[str, str], ...]
    ready_revisions: tuple[tuple[str, UUID, UUID], ...]
    missing_input_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InitialAssessmentSources:
    active_departments: tuple[DepartmentId, ...]
    metadata: dict[str, object]
    revisions: tuple[dict[str, object], ...]
    department_drafts: dict[DepartmentId, DepartmentDraft]
    global_draft: GlobalCommercialTermsDraft
    capacity_input_id: UUID | None
    capacity: WarehouseCapacityInput | None
    volume_input_id: UUID | None = None
    volume: MonthlyStorageVolumeInput | None = None


def _requires_historical_department_rollup(
    revisions: list[dict[str, object]] | tuple[dict[str, object], ...],
) -> bool:
    """Keep initial imports strict; corrected current content replaces its rollup."""

    return all(
        _persisted_revision_creation_reason(row) == "part2_import"
        for row in revisions
    )


def _department_draft_from_revision(
    row: dict[str, object], source_draft: DepartmentDraft
) -> DepartmentDraft:
    """Rehydrate the exact current draft from the persisted revision envelope."""

    creation_reason = _persisted_revision_creation_reason(row)
    payload = row.get("content_payload")
    if not isinstance(payload, dict):
        raise ApprovalStoreError("revision_content_payload_invalid")
    if creation_reason == "part2_import":
        if (
            row.get("review_revision_no") != 1
            or payload.get("content") != source_draft.generated_content
        ):
            raise ApprovalStoreError("initial_revision_source_mismatch")
        return source_draft
    try:
        return DepartmentDraft.model_validate(
            {
                **source_draft.model_dump(mode="python"),
                "draft_id": UUID(str(payload["draft_id"])),
                "generated_content": payload["content"],
                "commercial_terms": payload["commercial_terms"],
                "pending_commercial_items": payload["pending_commercial_items"],
                "draft_status": (
                    "provisional_missing_inputs"
                    if payload["pending_commercial_items"]
                    else "complete_candidate"
                ),
                "generated_at": row["created_at"],
            }
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ApprovalStoreError("corrected_revision_payload_invalid") from error


def assert_current_conflict_clearance(
    session: Session, *, approval_run_id: UUID, ticket_id: UUID
) -> None:
    """Require all official no-conflict assessments for the exact current content."""

    run = session.execute(
        select(rfp_approval_runs).where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
        )
    ).mappings().one_or_none()
    if run is None:
        raise ApprovalStoreError("approval_run_not_found")
    revisions = session.execute(
        select(
            rfp_approval_department_revisions.c.department_id,
            rfp_approval_department_revisions.c.content_hash,
            rfp_approval_department_revisions.c.review_revision_no,
            rfp_approval_department_revisions.c.content_payload,
        ).where(
            rfp_approval_department_revisions.c.approval_run_id == approval_run_id,
            rfp_approval_department_revisions.c.ticket_id == ticket_id,
            rfp_approval_department_revisions.c.is_current.is_(True),
        )
    ).mappings().all()
    revision_rows = [dict(row) for row in revisions]
    _handoff, global_draft, _evaluation, _draft, _evaluation_row, _preview = (
        _load_part2_sources(
            session,
            dict(run),
            require_department_rollup_pass=(
                _requires_historical_department_rollup(revision_rows)
            ),
        )
    )
    expected = {
        row["department_id"]: row["content_hash"] for row in revision_rows
    }
    expected["proposal"] = canonical_hash(global_draft)
    rows = session.execute(
        select(
            rfp_conflict_assessments.c.trigger_id,
            rfp_conflict_assessments.c.assessed_content_hashes,
        ).where(
            rfp_conflict_assessments.c.approval_run_id == approval_run_id,
            rfp_conflict_assessments.c.ticket_id == ticket_id,
            rfp_conflict_assessments.c.outcome == "no_conflict",
        )
    ).mappings().all()
    cleared = {
        row["trigger_id"]
        for row in rows
        if _deserialize_assessed_content_hashes(row["assessed_content_hashes"])
        == expected
    }
    if cleared != {
        "volume-vs-capacity",
        "returns-sla-breach",
        "currency-mismatch",
    }:
        raise ApprovalStoreError("preapproval_conflict_clearance_required")


_UNIT_ALIASES: dict[str, tuple[str, str]] = {
    "m3": ("volume", "m3"),
    "m³": ("volume", "m3"),
    "cubic meter": ("volume", "m3"),
    "cubic meters": ("volume", "m3"),
    "kg": ("mass", "kg"),
    "kilogram": ("mass", "kg"),
    "kilograms": ("mass", "kg"),
    "order": ("item_count", "order"),
    "orders": ("item_count", "order"),
    "shipment": ("item_count", "shipment"),
    "shipments": ("item_count", "shipment"),
    "item": ("item_count", "item"),
    "items": ("item_count", "item"),
    "sku": ("item_count", "sku"),
    "skus": ("item_count", "sku"),
    "pallet": ("pallet_count", "pallet"),
    "pallets": ("pallet_count", "pallet"),
}


def _quantity_unit(value: str) -> tuple[str, str] | None:
    normalized = value.strip().lower().replace(" per month", "")
    normalized = normalized.replace("/month", "").replace("/mo", "")
    return _UNIT_ALIASES.get(normalized.strip())


def _period(value: str) -> str:
    normalized = value.strip().lower()
    return "month" if normalized in {"month", "monthly", "mes", "mensual"} else normalized


def _assessment(
    facts,
    *,
    missing: list[str],
    assessed_at: datetime,
    assessed_content_hashes: dict[str, str],
) -> ConflictAssessment:
    if isinstance(facts, VolumeCapacityFacts):
        if facts.comparison_status in {"missing_required_volume", "missing_capacity", "incompatible_units"}:
            outcome = "insufficient_information"
        elif facts.exceeds_capacity:
            outcome = "demonstrated"
        else:
            outcome = "no_conflict"
    else:
        outcome = "demonstrated" if facts.conflict_demonstrated else "no_conflict"
    digest = conflict_assessment_hash(
        trigger=facts.trigger,
        facts=facts,
        outcome=outcome,
        missing_input_codes=missing,
        assessed_content_hashes=assessed_content_hashes,
    )
    return ConflictAssessment(
        assessment_id=uuid5(NAMESPACE_URL, f"trackflow:assessment:{digest}"),
        trigger=facts.trigger,
        facts=facts,
        outcome=outcome,
        missing_input_codes=missing,
        assessed_content_hashes=assessed_content_hashes,
        assessment_hash=digest,
        assessed_at=assessed_at,
    )


def build_initial_assessments(
    sources: InitialAssessmentSources, *, assessed_at: datetime
) -> tuple[ConflictAssessment, ConflictAssessment, ConflictAssessment]:
    """Build deterministic assessments without accepting request-supplied facts."""

    active = set(sources.active_departments)
    by_department = {
        DepartmentId(row["department_id"]): row for row in sources.revisions
    }
    assessed_content_hashes = {
        row["department_id"]: row["content_hash"] for row in sources.revisions
    }
    assessed_content_hashes["proposal"] = canonical_hash(sources.global_draft)
    volume_missing: list[str] = []
    if not {DepartmentId.warehouse, DepartmentId.lastmile} <= active:
        volume_facts = VolumeCapacityFacts(
            applicable=False,
            comparison_status="not_applicable",
            exceeds_capacity=None,
        )
    else:
        raw_value = sources.metadata.get("monthly_volume_value")
        raw_unit = sources.metadata.get("monthly_volume_unit")
        source_ref = sources.metadata.get("monthly_volume_source_ref")
        authorized_volume = sources.volume
        authorized_unit = (
            _quantity_unit(authorized_volume.unit)
            if authorized_volume is not None
            else None
        )
        authorized_scope = (
            authorized_volume.operational_scope
            if authorized_volume is not None
            else None
        )
        if authorized_volume is not None and (
            authorized_unit != ("volume", "m3")
            or _period(authorized_volume.period) != "month"
            or not (
                authorized_scope
                and (
                    authorized_scope.scope_code == "monthly-storage-volume"
                    or authorized_scope.service_code
                    in {"warehouse-storage", "warehouse-lastmile-throughput"}
                )
            )
        ):
            authorized_volume = None
            volume_missing.append("MONTHLY_VOLUME_INPUT_INCOMPATIBLE")
        unit = _quantity_unit(str(raw_unit)) if raw_unit else None
        if (
            authorized_volume is not None
            and raw_value is not None
            and source_ref is not None
            and (
                unit != authorized_unit
                or Decimal(str(raw_value)) != authorized_volume.value
            )
        ):
            authorized_volume = None
            volume_missing.append("MONTHLY_VOLUME_SOURCE_CONTRADICTION")
        if authorized_volume is not None:
            required = QuantityFact(
                value=authorized_volume.value,
                dimension="volume",
                unit="m3",
                period="month",
                operational_scope=authorized_volume.operational_scope,
            )
        elif raw_value is not None and source_ref is not None and unit is not None:
            scope = QuantityScope(
                scope_code="approval-run-volume-capacity",
                service_code="warehouse-lastmile-throughput",
            )
            required = QuantityFact(
                value=Decimal(str(raw_value)),
                dimension=unit[0],
                unit=unit[1],
                period="month",
                operational_scope=scope,
            )
        else:
            volume_facts = VolumeCapacityFacts(
                applicable=True,
                comparison_status="missing_required_volume",
                exceeds_capacity=None,
            )
            if not volume_missing:
                volume_missing.append("MONTHLY_VOLUME_REQUIRED")
            required = None
        if required is not None:
            capacity = sources.capacity
            capacity_unit = (
                _quantity_unit(capacity.capacity_unit) if capacity is not None else None
            )
            if capacity is None or sources.capacity_input_id is None:
                volume_facts = VolumeCapacityFacts(
                    required_volume=required,
                    required_volume_input_id=sources.volume_input_id,
                    comparison_status="missing_capacity",
                    exceeds_capacity=None,
                )
                volume_missing.append("WAREHOUSE_CAPACITY_REQUIRED")
            elif capacity_unit is None:
                volume_facts = VolumeCapacityFacts(
                    required_volume=required,
                    required_volume_input_id=sources.volume_input_id,
                    comparison_status="missing_capacity",
                    exceeds_capacity=None,
                )
                volume_missing.append("CAPACITY_UNIT_UNSUPPORTED")
            else:
                available = QuantityFact(
                    value=capacity.capacity_value,
                    dimension=capacity_unit[0],
                    unit=capacity_unit[1],
                    period=_period(capacity.period),
                    operational_scope=required.operational_scope,
                )
                comparable = (
                    required.dimension == available.dimension
                    and required.unit == available.unit
                    and required.period == available.period
                )
                volume_facts = VolumeCapacityFacts(
                    required_volume=required,
                    required_volume_input_id=sources.volume_input_id,
                    available_capacity=available,
                    capacity_input_id=sources.capacity_input_id,
                    comparison_status=("comparable" if comparable else "incompatible_units"),
                    exceeds_capacity=(required.value > available.value if comparable else None),
                )
                if not comparable:
                    volume_missing.append("CAPACITY_NORMALIZATION_REQUIRED")

    observed: list[ReturnSLAViolation] = []
    for department in sources.active_departments:
        revision_id = by_department[department]["revision_id"]
        hours = extract_return_processing_hours(
            sources.department_drafts[department].generated_content
        )
        if hours:
            observed.append(
                ReturnSLAViolation(
                    department_revision_id=revision_id,
                    sla_hours=Decimal(str(min(hours))),
                )
            )
    violating = [item.department_revision_id for item in observed if item.sla_hours < 48]
    returns_facts = ReturnsSLAFacts(
        observed=observed,
        violating_revision_ids=violating,
        conflict_demonstrated=bool(violating),
    )

    country = sources.metadata.get("client_country_code")
    if country not in {"US", "ES"}:
        raise ApprovalStoreError("client_country_not_documented")
    if (
        sources.global_draft.currency.status != "documented"
        or sources.global_draft.currency.value is None
    ):
        raise ApprovalStoreError("part2_global_currency_not_documented")
    monetary_terms: list[MonetaryTermFact] = []
    for department in sources.active_departments:
        revision_id = by_department[department]["revision_id"]
        for term in sources.department_drafts[department].commercial_terms:
            if term.status == "documented" and term.currency is not None:
                monetary_terms.append(
                    MonetaryTermFact(
                        term_code=f"{department.value}:{term.term_id}",
                        department_revision_id=revision_id,
                        currency=term.currency,
                    )
                )
    expected_currency = "USD" if country == "US" else "EUR"
    proposal_id = sources.global_draft.draft_id
    offenders: list[UUID] = []
    if sources.global_draft.currency.value != expected_currency:
        offenders.append(proposal_id)
    offenders.extend(
        term.department_revision_id
        for term in monetary_terms
        if term.currency != expected_currency
    )
    offenders = list(dict.fromkeys(offenders))
    currency_facts = CurrencyMismatchFacts(
        client_country=country,
        expected_currency=expected_currency,
        proposal_revision_id=proposal_id,
        proposal_currency=sources.global_draft.currency.value,
        monetary_terms=monetary_terms,
        mismatched_revision_ids=offenders,
        conflict_demonstrated=bool(offenders),
    )
    return (
        _assessment(
            volume_facts,
            missing=volume_missing,
            assessed_at=assessed_at,
            assessed_content_hashes=assessed_content_hashes,
        ),
        _assessment(
            returns_facts,
            missing=[],
            assessed_at=assessed_at,
            assessed_content_hashes=assessed_content_hashes,
        ),
        _assessment(
            currency_facts,
            missing=[],
            assessed_at=assessed_at,
            assessed_content_hashes=assessed_content_hashes,
        ),
    )


def _latest_capacity(
    session: Session, *, approval_run_id: UUID, ticket_id: UUID, today: date
) -> tuple[UUID | None, WarehouseCapacityInput | None]:
    rows = session.execute(
        select(rfp_authorized_business_inputs)
        .select_from(
            rfp_authorized_business_inputs.join(
                rfp_authorizations,
                and_(
                    rfp_authorizations.c.authorization_id
                    == rfp_authorized_business_inputs.c.authorization_id,
                    rfp_authorizations.c.principal_id
                    == rfp_authorized_business_inputs.c.actor_principal_id,
                    rfp_authorizations.c.environment
                    == rfp_authorized_business_inputs.c.environment,
                    rfp_authorizations.c.authorization_version
                    == rfp_authorized_business_inputs.c.authorization_version,
                ),
            ).join(
                rfp_identity_bindings,
                and_(
                    rfp_identity_bindings.c.binding_id
                    == rfp_authorized_business_inputs.c.actor_binding_id,
                    rfp_identity_bindings.c.principal_id
                    == rfp_authorized_business_inputs.c.actor_principal_id,
                    rfp_identity_bindings.c.environment
                    == rfp_authorized_business_inputs.c.environment,
                ),
            )
        )
        .where(
            rfp_authorized_business_inputs.c.approval_run_id == approval_run_id,
            rfp_authorized_business_inputs.c.ticket_id == ticket_id,
            rfp_authorized_business_inputs.c.input_kind == "warehouse_capacity",
            rfp_authorized_business_inputs.c.department_scope == "warehouse",
            rfp_authorizations.c.permission == "provide_business_input",
            rfp_authorizations.c.department_scope == "warehouse",
            rfp_authorizations.c.target_policy == "department_exact",
            rfp_authorizations.c.active_from <= func.now(),
            rfp_authorizations.c.revoked_at.is_(None),
            or_(
                rfp_authorizations.c.expires_at.is_(None),
                rfp_authorizations.c.expires_at > func.now(),
            ),
            rfp_identity_bindings.c.active.is_(True),
        )
        .order_by(rfp_authorized_business_inputs.c.recorded_at.desc())
    ).mappings().all()
    for row in rows:
        try:
            value = WarehouseCapacityInput.model_validate(row["payload"])
        except ValueError as error:
            raise ApprovalStoreError("authorized_capacity_payload_invalid") from error
        if value.available_from <= today and (
            value.available_until is None or today <= value.available_until
        ):
            return row["input_id"], value
    return None, None


def _latest_monthly_volume(
    session: Session, *, approval_run_id: UUID, ticket_id: UUID, today: date
) -> tuple[UUID | None, MonthlyStorageVolumeInput | None]:
    rows = session.execute(
        select(rfp_authorized_business_inputs)
        .select_from(
            rfp_authorized_business_inputs.join(
                rfp_authorizations,
                and_(
                    rfp_authorizations.c.authorization_id
                    == rfp_authorized_business_inputs.c.authorization_id,
                    rfp_authorizations.c.principal_id
                    == rfp_authorized_business_inputs.c.actor_principal_id,
                    rfp_authorizations.c.environment
                    == rfp_authorized_business_inputs.c.environment,
                    rfp_authorizations.c.authorization_version
                    == rfp_authorized_business_inputs.c.authorization_version,
                ),
            ).join(
                rfp_identity_bindings,
                and_(
                    rfp_identity_bindings.c.binding_id
                    == rfp_authorized_business_inputs.c.actor_binding_id,
                    rfp_identity_bindings.c.principal_id
                    == rfp_authorized_business_inputs.c.actor_principal_id,
                    rfp_identity_bindings.c.environment
                    == rfp_authorized_business_inputs.c.environment,
                ),
            )
        )
        .where(
            rfp_authorized_business_inputs.c.approval_run_id == approval_run_id,
            rfp_authorized_business_inputs.c.ticket_id == ticket_id,
            rfp_authorized_business_inputs.c.input_kind == "monthly_storage_volume",
            rfp_authorized_business_inputs.c.department_scope.is_(None),
            rfp_authorizations.c.permission == "provide_business_input",
            rfp_authorizations.c.department_scope.is_(None),
            rfp_authorizations.c.target_policy == "proposal_wide",
            rfp_authorizations.c.active_from <= func.now(),
            rfp_authorizations.c.revoked_at.is_(None),
            or_(
                rfp_authorizations.c.expires_at.is_(None),
                rfp_authorizations.c.expires_at > func.now(),
            ),
            rfp_identity_bindings.c.active.is_(True),
        )
        .order_by(rfp_authorized_business_inputs.c.recorded_at.desc())
    ).mappings().all()
    for row in rows:
        try:
            value = MonthlyStorageVolumeInput.model_validate_json(
                json.dumps(row["payload"])
            )
            provenance = BusinessInputProvenance.model_validate_json(
                json.dumps(row["provenance"])
            )
        except (TypeError, ValueError) as error:
            raise ApprovalStoreError("authorized_volume_payload_invalid") from error
        if (
            canonical_hash(provenance) != row["provenance_hash"]
            or provenance.source_payload_hash != row["evidence_hash"]
        ):
            raise ApprovalStoreError("authorized_volume_provenance_invalid")
        if provenance.valid_until is not None and today > provenance.valid_until:
            continue
        if value.effective_from <= today and (
            value.effective_until is None or today <= value.effective_until
        ):
            return row["input_id"], value
    return None, None


def assess_and_release_initial_revisions(
    session: Session,
    *,
    approval_run_id: UUID,
    ticket_id: UUID,
    identity: ResolvedIdentity,
    authorization: EffectiveAuthorization,
    execution_id: UUID,
) -> InitialAssessmentResult:
    """Persist exact assessments and release only a fully cleared initial run."""

    if authorization.permission not in {"start_approval_run", "manage_approval_run"}:
        raise ApprovalStoreError("run_management_permission_required")
    run_row = session.execute(
        select(rfp_approval_runs)
        .where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
            rfp_approval_runs.c.environment == identity.environment,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if run_row is None or run_row["status"] != "active":
        raise ApprovalStoreError("approval_run_not_active")
    run = dict(run_row)
    reference = AuthorizationReference(
        authorization.authorization_id, authorization.authorization_version
    )
    authorization = resolve_effective_authorization(
        session,
        identity=identity,
        target=AuthorizationTarget(
            permission=authorization.permission,
            target_policy="run",
        ),
        reference=reference,
        lock_rows=True,
    )
    revision_rows = session.execute(
        select(rfp_approval_department_revisions)
        .where(
            rfp_approval_department_revisions.c.approval_run_id == approval_run_id,
            rfp_approval_department_revisions.c.ticket_id == ticket_id,
            rfp_approval_department_revisions.c.is_current.is_(True),
        )
        .order_by(rfp_approval_department_revisions.c.department_id)
        .with_for_update()
    ).mappings().all()
    active = tuple(DepartmentId(value) for value in run["active_departments"])
    if {row["department_id"] for row in revision_rows} != {
        item.value for item in active
    }:
        raise ApprovalStoreError("initial_revision_scope_mismatch")
    if any(
        row["status"] not in {"pending_assessment", "pending_approval"}
        or row["applied_decision_id"] is not None
        for row in revision_rows
    ):
        raise ApprovalStoreError("assessment_requires_reviewable_current_revisions")

    current_revisions = [dict(row) for row in revision_rows]
    handoff, global_draft, _global_evaluation, _draft_row, _evaluation_row, _preview = (
        _load_part2_sources(
            session,
            run,
            require_department_rollup_pass=(
                _requires_historical_department_rollup(current_revisions)
            ),
        )
    )
    handoff_by_department = {item.department: item for item in handoff.department_results}
    department_drafts: dict[DepartmentId, DepartmentDraft] = {}
    for row in revision_rows:
        department = DepartmentId(row["department_id"])
        result = handoff_by_department.get(department)
        if result is None:
            raise ApprovalStoreError("part2_department_artifacts_incomplete")
        source = _load_department_source(
            session, handoff=handoff, department_result=result
        )
        draft, _evaluation, _preview, draft_hash, evaluation_hash, _preview_hash, _at = source
        if (
            row["source_part2_draft_id"] != draft.draft_id
            or row["source_part2_draft_hash"] != draft_hash
            or row["source_part2_evaluation_id"] != _evaluation.evaluation_id
            or row["source_part2_evaluation_hash"] != evaluation_hash
        ):
            raise ApprovalStoreError("revision_part2_heritage_mismatch")
        department_drafts[department] = _department_draft_from_revision(
            dict(row), draft
        )

    metadata = session.execute(
        select(rfp_metadata).where(rfp_metadata.c.ticket_id == ticket_id)
    ).mappings().one_or_none()
    if metadata is None:
        raise ApprovalStoreError("rfp_metadata_not_found")
    assessed_at = session.execute(select(func.statement_timestamp())).scalar_one()
    today = session.execute(select(func.current_date())).scalar_one()
    capacity_id, capacity = _latest_capacity(
        session,
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        today=today,
    )
    volume_id, volume = _latest_monthly_volume(
        session,
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        today=today,
    )
    sources = InitialAssessmentSources(
        active_departments=active,
        metadata=dict(metadata),
        revisions=tuple(dict(row) for row in revision_rows),
        department_drafts=department_drafts,
        global_draft=global_draft,
        volume_input_id=volume_id,
        volume=volume,
        capacity_input_id=capacity_id,
        capacity=capacity,
    )
    assessments = build_initial_assessments(sources, assessed_at=assessed_at)
    assessed_hashes = {
        row["department_id"]: row["content_hash"] for row in revision_rows
    }
    assessed_hashes["proposal"] = canonical_hash(global_draft)
    by_revision = {row["revision_id"]: row for row in revision_rows}
    for assessment in assessments:
        conflict = None
        if assessment.outcome == "demonstrated":
            if assessment.trigger == "volume-vs-capacity":
                lastmile = next(
                    row for row in revision_rows if row["department_id"] == "lastmile"
                )
                targets = [
                    ArbitrationTarget(
                        department=DepartmentId.lastmile,
                        department_revision_id=lastmile["revision_id"],
                    )
                ]
            elif assessment.trigger == "returns-sla-breach":
                facts = assessment.facts
                targets = [
                    ArbitrationTarget(
                        department=DepartmentId(by_revision[item]["department_id"]),
                        department_revision_id=item,
                    )
                    for item in facts.violating_revision_ids
                ]
            else:
                facts = assessment.facts
                if facts.proposal_revision_id in facts.mismatched_revision_ids:
                    raise ApprovalStoreError(
                        "part2_global_currency_conflict_requires_correction"
                    )
                targets = [
                    ArbitrationTarget(
                        department=DepartmentId(by_revision[item]["department_id"]),
                        department_revision_id=item,
                    )
                    for item in facts.mismatched_revision_ids
                ]
            fingerprint = arbitration_conflict_fingerprint(
                trigger=assessment.trigger,
                facts=assessment.facts,
                affected_targets=targets,
            )
            conflict = ArbitrationConflict(
                conflict_id=uuid5(NAMESPACE_URL, f"trackflow:conflict:{fingerprint}"),
                approval_run_id=approval_run_id,
                trigger=assessment.trigger,
                facts=assessment.facts,
                affected_targets=targets,
                status="demonstrated",
                missing_input_codes=[],
                fingerprint=fingerprint,
                detected_at=assessed_at,
            )
        persist_conflict_assessment(
            session,
            approval_run_id=approval_run_id,
            ticket_id=ticket_id,
            assessment=assessment,
            assessed_content_hashes=assessed_hashes,
            conflict=conflict,
            execution_id=execution_id,
        )

    missing = tuple(
        code for item in assessments for code in item.missing_input_codes
    )
    if any(item.outcome == "demonstrated" for item in assessments):
        return InitialAssessmentResult(
            approval_run_id=approval_run_id,
            outcomes=tuple((item.trigger, item.outcome) for item in assessments),
            ready_revisions=(),
            missing_input_codes=missing,
        )

    ready: list[tuple[str, UUID, UUID]] = []
    for row in revision_rows:
        bundle_id = session.execute(
            select(rfp_approval_validation_bundles.c.validation_bundle_id).where(
                rfp_approval_validation_bundles.c.approval_run_id == approval_run_id,
                rfp_approval_validation_bundles.c.ticket_id == ticket_id,
                rfp_approval_validation_bundles.c.scope_key == row["department_id"],
                rfp_approval_validation_bundles.c.content_hash == row["content_hash"],
                rfp_approval_validation_bundles.c.eligible.is_(True),
            )
        ).scalar_one_or_none()
        if bundle_id is None:
            raise ApprovalStoreError("initial_validation_bundle_not_found")
        if row["status"] == "pending_assessment":
            link_initial_validation_bundle(
                session,
                approval_run_id=approval_run_id,
                ticket_id=ticket_id,
                revision_id=row["revision_id"],
                validation_bundle_id=bundle_id,
                identity=identity,
                authorization_reference=reference,
                permission=authorization.permission,
                execution_id=execution_id,
            )
        ready.append((row["department_id"], row["revision_id"], bundle_id))
    return InitialAssessmentResult(
        approval_run_id=approval_run_id,
        outcomes=tuple((item.trigger, item.outcome) for item in assessments),
        ready_revisions=tuple(ready),
        missing_input_codes=missing,
    )
