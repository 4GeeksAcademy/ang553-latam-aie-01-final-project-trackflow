from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    EvidenceReference,
    GlobalCurrency,
    GlobalSLA,
    MissingCommercialInput,
    VolumeDiscountRow,
)
from data.pipelines.rfp_response.hashing import canonical_hash
from pipelines.rfp_approval.contracts import (
    ApprovalArtifactProvenance,
    ApprovalRevisionValidation,
    ApprovalRunStart,
    ApprovalValidationArtifact,
    ApprovalValidationBundle,
    ArbitrationConflict,
    ArbitrationResolution,
    ArbitrationTarget,
    AuthenticatedPrincipal,
    AuthorizedBusinessInput,
    AuthorizedHumanActor,
    BusinessInputProvenance,
    ClientPriceQuoteInput,
    CurrencyConversionInput,
    CurrencyMismatchFacts,
    CurrencyQuoteInput,
    ConflictAssessment,
    ContentEvaluationArtifactPayload,
    CorrectedDraftArtifact,
    CorrectedRevisionEvidence,
    DecisionApplicationCommand,
    DepartmentRevision,
    DepartmentCorrectedDraftPayload,
    DeterministicRuleExecution,
    FinalDepartmentSection,
    FinalDocumentManifest,
    FinalDocumentPayload,
    FinalGlobalTerms,
    GlobalCommercialTermInput,
    HumanArbitrationExecution,
    HumanDecision,
    ImmutableArtifactReference,
    InitialProposalRevisionCommand,
    InitialRevisionReference,
    MonetaryTermFact,
    MonthlyStorageVolumeInput,
    OperationalScopeInput,
    PersistedFinalDocument,
    ProposalRevision,
    ProposalRevisionValidation,
    ProposalCorrectedDraftPayload,
    QuantityFact,
    QuantityScope,
    ReturnSLAViolation,
    ReturnsSLAFacts,
    ReviewInterruptPayload,
    ReviewPreparationReceipt,
    RevisionValidationReferences,
    RunInitiator,
    SafePreviewArtifactPayload,
    VolumeCapacityFacts,
    VolumeDiscountInput,
    VolumeDiscountTierInput,
    WarehouseCapacityInput,
    arbitration_conflict_fingerprint,
    arbitration_resolution_hash,
    authorized_business_input_hash,
    canonical_contract_payload,
    conflict_assessment_hash,
    decision_application_key,
    department_revision_content_hash,
    deterministic_execution_hash,
    interrupt_payload_hash,
    proposal_revision_content_hash,
    public_final_document,
    review_audit_event_key,
)


NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 20)
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64


def uid(number: int) -> UUID:
    return UUID(int=number)


def principal(*, role: str = "user", synthetic: bool = False) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        issuer="urn:trackflow:qa:local" if synthetic else "trackflow-local",
        subject=f"subject-{role}",
        trackflow_user_id=f"user-{role}",
        auth_source="local",
        general_role=role,
        synthetic=synthetic,
    )


def actor(
    responsible: str,
    permission: str,
    *,
    department: DepartmentId | None = None,
    trigger: str | None = None,
) -> AuthorizedHumanActor:
    return AuthorizedHumanActor(
        principal=principal(),
        authorization_id=uid(20),
        official_responsible=responsible,
        permission=permission,
        department_scope=department,
        trigger_scope=trigger,
    )


def ana(permission: str = "provide_business_input") -> AuthorizedHumanActor:
    return actor("Ana Whitfield", permission, department=DepartmentId.warehouse)


def carlos(permission: str = "provide_business_input") -> AuthorizedHumanActor:
    return actor("Carlos Vega", permission, department=DepartmentId.lastmile)


def sofia(permission: str = "provide_business_input") -> AuthorizedHumanActor:
    return actor("Sofía Ramos", permission, department=DepartmentId.reverse)


def miguel(permission: str = "provide_business_input", trigger: str | None = None) -> AuthorizedHumanActor:
    return actor("Miguel Torres", permission, trigger=trigger)


def evidence() -> EvidenceReference:
    return EvidenceReference(document_id="source-doc", section_id="section-1", evidence_hash=H1)


def artifact(kind: str, content_hash: str, number: int) -> ImmutableArtifactReference:
    return ImmutableArtifactReference(
        artifact_type=kind,
        artifact_id=uid(number),
        contract_version="test-v1",
        payload_hash=H2,
        evaluated_content_hash=content_hash,
    )


def revision_references(content_hash: str) -> RevisionValidationReferences:
    evaluation = artifact("evaluation", content_hash, 100)
    preview = artifact("safe_preview", content_hash, 101)
    validation_hash = canonical_hash(
        {
            "revision_content_hash": content_hash,
            "evaluation": evaluation,
            "safe_preview": preview,
            "validated_at": NOW,
        }
    )
    return RevisionValidationReferences(
        revision_content_hash=content_hash,
        evaluation=evaluation,
        safe_preview=preview,
        validation_hash=validation_hash,
        validated_at=NOW,
    )


def proposal_validation(
    content_hash: str,
    *,
    source_departments_approved: bool = True,
    open_conflict_ids: list[UUID] | None = None,
) -> ProposalRevisionValidation:
    global_evaluation = artifact("global_evaluation", content_hash, 102)
    preview = artifact("safe_preview", content_hash, 103)
    conflicts = open_conflict_ids or []
    data = {
        "revision_content_hash": content_hash,
        "global_evaluation": global_evaluation,
        "safe_preview": preview,
        "mandatory_compliance_passed": True,
        "unresolved_input_codes": [],
        "blocking_rule_ids": [],
        "open_conflict_ids": conflicts,
        "source_departments_approved": source_departments_approved,
        "eligible_for_synthesis": source_departments_approved and not conflicts,
        "validated_at": NOW,
    }
    return ProposalRevisionValidation(
        contract_version="rfp-proposal-revision-validation-v2",
        **data,
        validation_hash=canonical_hash(data),
    )


def documented_globals() -> tuple[GlobalCurrency, GlobalSLA, list[VolumeDiscountRow]]:
    ref = evidence()
    return (
        GlobalCurrency(value="USD", status="documented", evidence=[ref]),
        GlobalSLA(
            applicability="applicable",
            percentage=99.0,
            measurement_period="monthly",
            scope=["warehouse", "lastmile", "reverse"],
            exclusions=[],
            evidence=[ref],
        ),
        [
            VolumeDiscountRow(
                volume_from=100.0,
                volume_to=200.0,
                discount_percentage=5.0,
                condition="monthly volume",
                status="documented",
                evidence=[ref],
            )
        ],
    )


def provenance(kind: str) -> BusinessInputProvenance:
    return BusinessInputProvenance(
        evidence_type=kind,
        evidence_id=uid(300),
        source_name="Authorized source",
        issued_at=NOW,
        valid_until=date(2027, 1, 1),
        source_payload_hash=H3,
    )


def artifact_provenance() -> ApprovalArtifactProvenance:
    data = {
        "producer_node": "evaluate_corrected_content",
        "execution_id": uid(350),
        "source_kind": "deterministic_evaluator",
        "source_artifact_ids": [uid(351)],
        "source_hashes": [H1],
        "produced_at": NOW,
    }
    return ApprovalArtifactProvenance(
        **data,
        provenance_hash=canonical_hash(data),
    )


def quantity(
    value: str,
    *,
    unit: str = "m3",
    period: str = "month",
    location: str = "WH-MAD",
    service: str = "WAREHOUSE",
) -> QuantityFact:
    return QuantityFact(
        value=Decimal(value),
        dimension="volume",
        unit=unit,
        period=period,
        operational_scope=QuantityScope(
            location_code=location,
            service_code=service,
        ),
    )


def conflict_assessment(
    facts, outcome: str, missing_input_codes: list[str] | None = None
) -> ConflictAssessment:
    missing = missing_input_codes or []
    return ConflictAssessment(
        assessment_id=uid(390),
        trigger=facts.trigger,
        facts=facts,
        outcome=outcome,
        missing_input_codes=missing,
        assessment_hash=conflict_assessment_hash(
            trigger=facts.trigger,
            facts=facts,
            outcome=outcome,
            missing_input_codes=missing,
        ),
        assessed_at=NOW,
    )


def authorized_input(payload, owner: AuthorizedHumanActor, evidence_type: str) -> AuthorizedBusinessInput:
    source = provenance(evidence_type)
    payload_hash = authorized_business_input_hash(
        input_kind=payload.input_kind,
        payload=payload,
        provenance=source,
        actor=owner,
    )
    return AuthorizedBusinessInput(
        input_id=uid(301),
        ticket_id=uid(1),
        approval_run_id=uid(2),
        input_kind=payload.input_kind,
        payload=payload,
        provenance=source,
        actor=owner,
        payload_hash=payload_hash,
        recorded_at=NOW,
    )


def test_strict_contracts_reject_extra_fields_and_naive_datetimes() -> None:
    with pytest.raises(ValidationError):
        AuthenticatedPrincipal(
            issuer="trackflow-local",
            subject="x",
            trackflow_user_id="u",
            auth_source="local",
            general_role="user",
            hidden="no",
        )
    with pytest.raises(ValidationError):
        BusinessInputProvenance(
            evidence_type="approved_quote",
            evidence_id=uid(1),
            source_name="q",
            issued_at=datetime(2026, 1, 1),
            source_payload_hash=H1,
        )


def test_synthetic_principals_require_isolated_qa_issuer() -> None:
    assert principal(synthetic=True).synthetic is True
    with pytest.raises(ValidationError):
        AuthenticatedPrincipal(
            issuer="trackflow-local",
            subject="synthetic",
            trackflow_user_id="qa-user",
            auth_source="local",
            general_role="user",
            synthetic=True,
        )


def test_admin_can_start_run_but_has_no_implicit_department_approval() -> None:
    initiator = RunInitiator(
        principal=principal(role="admin"),
        authorization_id=uid(1),
        permission="start_approval_run",
    )
    assert initiator.permission == "start_approval_run"
    with pytest.raises(ValidationError):
        AuthorizedHumanActor(
            principal=principal(role="admin"),
            authorization_id=uid(2),
            official_responsible="Miguel Torres",
            permission="approve_department",
            department_scope=DepartmentId.warehouse,
        )


def test_sofia_arbitration_is_limited_to_return_sla_reverse_scope() -> None:
    valid = actor(
        "Sofía Ramos",
        "arbitrate",
        department=DepartmentId.reverse,
        trigger="returns-sla-breach",
    )
    assert valid.trigger_scope == "returns-sla-breach"
    with pytest.raises(ValidationError):
        actor(
            "Sofía Ramos",
            "arbitrate",
            department=DepartmentId.reverse,
            trigger="currency-mismatch",
        )


def test_run_start_requires_exact_ordered_department_references() -> None:
    initial = InitialRevisionReference(
        department=DepartmentId.warehouse,
        source_draft_id=uid(10),
        source_draft_hash=H1,
        source_evaluation_id=uid(11),
        source_evaluation_hash=H2,
        source_part2_iteration=3,
        terminal_status="iteration_limit_reached",
    )
    start = ApprovalRunStart(
        approval_run_id=uid(2),
        ticket_id=uid(1),
        response_run_id=uid(3),
        response_handoff_hash=H3,
        active_departments=[DepartmentId.warehouse],
        initial_revisions=[initial],
        started_by=RunInitiator(
            principal=principal(role="admin"),
            authorization_id=uid(4),
            permission="start_approval_run",
        ),
        started_at=NOW,
    )
    assert start.max_review_revisions == 3
    with pytest.raises(ValidationError):
        start.model_copy(update={"active_departments": [DepartmentId.lastmile]}).model_validate(
            start.model_copy(update={"active_departments": [DepartmentId.lastmile]}).model_dump()
        )


def test_proposal_automation_requires_explicit_run_management_delegation() -> None:
    initial = InitialRevisionReference(
        department=DepartmentId.warehouse,
        source_draft_id=uid(10),
        source_draft_hash=H1,
        source_evaluation_id=uid(11),
        source_evaluation_hash=H2,
        source_part2_iteration=1,
        terminal_status="passed",
    )
    values = {
        "approval_run_id": uid(2),
        "ticket_id": uid(1),
        "response_run_id": uid(3),
        "response_handoff_hash": H3,
        "active_departments": [DepartmentId.warehouse],
        "initial_revisions": [initial],
        "automate_proposal_materialization": True,
        "started_at": NOW,
    }
    with pytest.raises(ValidationError):
        ApprovalRunStart(
            **values,
            started_by=RunInitiator(
                principal=principal(role="admin"),
                authorization_id=uid(4),
                permission="start_approval_run",
            ),
        )

    delegated = ApprovalRunStart(
        **values,
        started_by=RunInitiator(
            principal=principal(role="admin"),
            authorization_id=uid(5),
            permission="manage_approval_run",
        ),
    )
    assert delegated.automate_proposal_materialization is True


def test_review_preparation_interrupt_and_application_keys_are_stable() -> None:
    content_hash = H1
    event_key = review_audit_event_key(
        approval_run_id=uid(2), revision_id=uid(5), content_hash=content_hash
    )
    receipt = ReviewPreparationReceipt(
        preparation_id=uid(6),
        approval_run_id=uid(2),
        revision_id=uid(5),
        draft_id=uid(7),
        content_hash=content_hash,
        validation_hash=H2,
        audit_event_key=event_key,
        prepared_at=NOW,
    )
    preview = artifact("safe_preview", content_hash, 8)
    payload_hash = interrupt_payload_hash(
        preparation_id=receipt.preparation_id,
        revision_id=receipt.revision_id,
        draft_id=receipt.draft_id,
        expected_content_hash=content_hash,
        allowed_actions=["approve", "reject", "request_changes"],
        safe_preview_ref=preview,
    )
    interrupt = ReviewInterruptPayload(
        preparation_id=receipt.preparation_id,
        revision_id=receipt.revision_id,
        draft_id=receipt.draft_id,
        expected_content_hash=content_hash,
        allowed_actions=["approve", "reject", "request_changes"],
        safe_preview_ref=preview,
        interrupt_payload_hash=payload_hash,
    )
    decision = HumanDecision(
        decision_id=uid(9),
        idempotency_key=uid(10),
        ticket_id=uid(1),
        approval_run_id=uid(2),
        department=DepartmentId.warehouse,
        revision_id=interrupt.revision_id,
        draft_id=interrupt.draft_id,
        expected_content_hash=content_hash,
        interrupt_id="interrupt-1",
        action="approve",
        actor=ana("approve_department"),
        decided_at=NOW,
    )
    key = decision_application_key(decision, receipt.preparation_id)
    command = DecisionApplicationCommand(
        decision=decision,
        preparation_id=receipt.preparation_id,
        application_key=key,
    )
    assert command.application_key == decision_application_key(decision, receipt.preparation_id)
    with pytest.raises(ValidationError):
        DecisionApplicationCommand(
            decision=decision,
            preparation_id=receipt.preparation_id,
            application_key="decision-apply:" + H3,
        )


def test_interrupt_rejects_duplicate_actions_and_stale_preview() -> None:
    preview = artifact("safe_preview", H1, 1)
    with pytest.raises(ValidationError):
        ReviewInterruptPayload(
            preparation_id=uid(1),
            revision_id=uid(2),
            draft_id=uid(3),
            expected_content_hash=H1,
            allowed_actions=["approve", "approve"],
            safe_preview_ref=preview,
            interrupt_payload_hash=H2,
        )
    with pytest.raises(ValidationError):
        ReviewInterruptPayload(
            preparation_id=uid(1),
            revision_id=uid(2),
            draft_id=uid(3),
            expected_content_hash=H2,
            allowed_actions=["approve"],
            safe_preview_ref=preview,
            interrupt_payload_hash=H3,
        )


def test_negative_decisions_require_reason_and_correct_owner() -> None:
    base = dict(
        decision_id=uid(1),
        idempotency_key=uid(2),
        ticket_id=uid(3),
        approval_run_id=uid(4),
        department=DepartmentId.warehouse,
        revision_id=uid(5),
        draft_id=uid(6),
        expected_content_hash=H1,
        interrupt_id="i",
        decided_at=NOW,
    )
    with pytest.raises(ValidationError):
        HumanDecision(**base, action="reject", actor=ana("approve_department"))
    with pytest.raises(ValidationError):
        HumanDecision(**base, action="approve", actor=carlos("approve_department"))


@pytest.mark.parametrize(
    ("payload", "owner", "evidence_type"),
    [
        (
            ClientPriceQuoteInput(
                department=DepartmentId.lastmile,
                service_code="LAST_MILE_RATE_REQUIRED",
                amount=Decimal("12.50"),
                currency="USD",
                unit="delivery",
                effective_from=TODAY,
            ),
            carlos(),
            "approved_quote",
        ),
        (
            WarehouseCapacityInput(
                site_code="WH-MAD",
                capacity_value=Decimal("900"),
                capacity_unit="m3",
                period="month",
                available_from=TODAY,
            ),
            ana(),
            "responsible_attestation",
        ),
        (
            OperationalScopeInput(
                department=DepartmentId.reverse,
                scope_code="RETURNS",
                description="Returns processing",
                included=True,
                effective_from=TODAY,
            ),
            sofia(),
            "approved_policy",
        ),
        (
            CurrencyQuoteInput(
                term_code="GLOBAL_FEE",
                amount=Decimal("100"),
                currency="USD",
                effective_from=TODAY,
            ),
            miguel(),
            "approved_quote",
        ),
        (
            CurrencyConversionInput(
                source_currency="EUR",
                target_currency="USD",
                conversion_rate=Decimal("1.10"),
                effective_at=NOW,
                source_term_codes=["GLOBAL_FEE"],
            ),
            miguel(),
            "authorized_conversion",
        ),
        (
            GlobalCommercialTermInput(
                term_code="PAYMENT_DAYS",
                value="30",
                effective_from=TODAY,
            ),
            miguel(),
            "approved_policy",
        ),
        (
            VolumeDiscountInput(
                volume_unit="m3",
                period="month",
                tiers=[
                    VolumeDiscountTierInput(
                        minimum_volume=Decimal("100"),
                        maximum_volume=Decimal("200"),
                        discount_percentage=Decimal("5"),
                    )
                ],
                effective_from=TODAY,
            ),
            miguel(),
            "approved_policy",
        ),
        (
            MonthlyStorageVolumeInput(
                value=Decimal("42.1250"),
                unit="m3",
                period="month",
                operational_scope=QuantityScope(
                    scope_code="monthly-storage-volume",
                    service_code="warehouse-storage",
                ),
                effective_from=TODAY,
            ),
            miguel(),
            "responsible_attestation",
        ),
    ],
)
def test_all_authorized_business_input_variants_are_discriminated(
    payload, owner, evidence_type
) -> None:
    value = authorized_input(payload, owner, evidence_type)
    assert value.input_kind == payload.input_kind
    assert value.evidence_reference().document_id == "authorized-business-input-v1"
    assert value.evidence_reference().section_id == str(value.input_id)


def test_authorized_input_rejects_kind_mismatch_and_unproven_monetary_values() -> None:
    payload = ClientPriceQuoteInput(
        department=DepartmentId.lastmile,
        service_code="LAST_MILE_RATE_REQUIRED",
        amount=Decimal("10"),
        currency="USD",
        unit="delivery",
        effective_from=TODAY,
    )
    source = provenance("responsible_attestation")
    with pytest.raises(ValidationError):
        AuthorizedBusinessInput(
            input_id=uid(1),
            ticket_id=uid(2),
            approval_run_id=uid(3),
            input_kind="warehouse_capacity",
            payload=payload,
            provenance=source,
            actor=carlos(),
            payload_hash=H1,
            recorded_at=NOW,
        )
    with pytest.raises(ValidationError):
        authorized_input(payload, carlos(), "responsible_attestation")


def test_conversion_requires_distinct_currencies_and_authorized_conversion_evidence() -> None:
    with pytest.raises(ValidationError):
        CurrencyConversionInput(
            source_currency="USD",
            target_currency="USD",
            conversion_rate=Decimal("1"),
            effective_at=NOW,
            source_term_codes=["RATE"],
        )
    payload = CurrencyConversionInput(
        source_currency="EUR",
        target_currency="USD",
        conversion_rate=Decimal("1.1"),
        effective_at=NOW,
        source_term_codes=["RATE"],
    )
    with pytest.raises(ValidationError):
        authorized_input(payload, miguel(), "approved_quote")


def test_volume_discount_open_tier_must_be_last() -> None:
    open_tier = VolumeDiscountTierInput(
        minimum_volume=Decimal("100"),
        maximum_volume=None,
        discount_percentage=Decimal("5"),
    )
    closed_tier = VolumeDiscountTierInput(
        minimum_volume=Decimal("200"),
        maximum_volume=Decimal("300"),
        discount_percentage=Decimal("7"),
    )
    with pytest.raises(ValidationError):
        VolumeDiscountInput(
            volume_unit="m3",
            period="month",
            tiers=[open_tier, closed_tier],
            effective_from=TODAY,
        )
    valid = VolumeDiscountInput(
        volume_unit="m3",
        period="month",
        tiers=[closed_tier, open_tier.model_copy(update={"minimum_volume": Decimal("300")})],
        effective_from=TODAY,
    )
    assert valid.tiers[-1].maximum_volume is None


def test_volume_discount_rejects_overlap_and_out_of_order_tiers() -> None:
    with pytest.raises(ValidationError):
        VolumeDiscountInput(
            volume_unit="m3",
            period="month",
            tiers=[
                VolumeDiscountTierInput(
                    minimum_volume=Decimal("100"),
                    maximum_volume=Decimal("250"),
                    discount_percentage=Decimal("5"),
                ),
                VolumeDiscountTierInput(
                    minimum_volume=Decimal("200"),
                    maximum_volume=Decimal("300"),
                    discount_percentage=Decimal("7"),
                ),
            ],
            effective_from=TODAY,
        )


def test_monthly_storage_volume_is_exact_and_rejects_invalid_contract_values() -> None:
    value = MonthlyStorageVolumeInput(
        value=Decimal("42.1250"),
        unit="m3",
        period="month",
        operational_scope=QuantityScope(scope_code="monthly-storage-volume"),
        effective_from=TODAY,
    )
    assert value.value == Decimal("42.1250")
    assert value.dimension == "volume"
    with pytest.raises(ValidationError):
        MonthlyStorageVolumeInput(
            value=Decimal("-0.0001"),
            unit="m3",
            period="month",
            operational_scope=QuantityScope(scope_code="monthly-storage-volume"),
            effective_from=TODAY,
        )
    with pytest.raises(ValidationError):
        MonthlyStorageVolumeInput(
            value=Decimal("1"),
            unit="ft3",
            period="month",
            operational_scope=QuantityScope(scope_code="monthly-storage-volume"),
            effective_from=TODAY,
        )
    with pytest.raises(ValidationError):
        MonthlyStorageVolumeInput(
            value=Decimal("1"),
            unit="m3",
            period="week",
            operational_scope=QuantityScope(scope_code="monthly-storage-volume"),
            effective_from=TODAY,
        )
    with pytest.raises(ValidationError):
        MonthlyStorageVolumeInput(
            value=Decimal("1"),
            unit="m3",
            period="month",
            operational_scope=QuantityScope(scope_code="monthly-storage-volume"),
            effective_from=TODAY,
            effective_until=TODAY.replace(day=TODAY.day - 1),
        )
    with pytest.raises(ValidationError):
        authorized_input(value, ana(), "responsible_attestation")


def test_department_revision_requires_fresh_evaluation_and_preserves_part2_counter() -> None:
    content_hash = department_revision_content_hash(
        content="Approved section",
        commercial_terms=[],
        pending_commercial_items=[],
        evidence=[],
    )
    validation = ApprovalRevisionValidation(
        references=revision_references(content_hash),
        mandatory_compliance_passed=True,
        blocks_commercial_completeness=False,
        blocking_rule_ids=[],
        unresolved_input_codes=[],
        open_conflict_ids=[],
        global_revalidation_required=True,
        eligible_for_approval=True,
    )
    revision = DepartmentRevision(
        revision_id=uid(1),
        draft_id=uid(2),
        ticket_id=uid(3),
        approval_run_id=uid(4),
        department=DepartmentId.warehouse,
        review_revision_no=1,
        source_part2_draft_id=uid(5),
        source_part2_draft_hash=H1,
        source_part2_evaluation_id=uid(6),
        source_part2_evaluation_hash=H2,
        source_part2_iteration=3,
        content="Approved section",
        content_hash=content_hash,
        commercial_terms=[],
        pending_commercial_items=[],
        evidence=[],
        validation=validation,
        status="pending_approval",
        creation_reason="part2_import",
        created_at=NOW,
    )
    assert revision.source_part2_iteration == 3
    with pytest.raises(ValidationError):
        revision_references(H3).model_copy(update={"revision_content_hash": H1}).model_validate(
            revision_references(H3).model_copy(update={"revision_content_hash": H1}).model_dump()
        )


def test_department_revision_supports_compliance_only_blocking() -> None:
    content_hash = department_revision_content_hash(
        content="Unsupported compliance promise",
        commercial_terms=[],
        pending_commercial_items=[],
        evidence=[],
    )
    validation = ApprovalRevisionValidation(
        references=revision_references(content_hash),
        mandatory_compliance_passed=False,
        blocks_commercial_completeness=False,
        blocking_rule_ids=["RETURNS_UNDER_48_HOURS"],
        unresolved_input_codes=[],
        open_conflict_ids=[],
        global_revalidation_required=True,
        eligible_for_approval=False,
    )
    revision = DepartmentRevision(
        revision_id=uid(401),
        draft_id=uid(402),
        ticket_id=uid(403),
        approval_run_id=uid(404),
        department=DepartmentId.reverse,
        review_revision_no=1,
        source_part2_draft_id=uid(405),
        source_part2_draft_hash=H1,
        source_part2_evaluation_id=uid(406),
        source_part2_evaluation_hash=H2,
        source_part2_iteration=3,
        content="Unsupported compliance promise",
        content_hash=content_hash,
        commercial_terms=[],
        pending_commercial_items=[],
        evidence=[],
        validation=validation,
        status="blocked_compliance",
        creation_reason="part2_import",
        created_at=NOW,
    )
    assert revision.status == "blocked_compliance"
    assert revision.pending_commercial_items == []


def test_department_revision_supports_conflict_only_blocking() -> None:
    content_hash = department_revision_content_hash(
        content="Capacity conflict requires arbitration",
        commercial_terms=[],
        pending_commercial_items=[],
        evidence=[],
    )
    validation = ApprovalRevisionValidation(
        references=revision_references(content_hash),
        mandatory_compliance_passed=True,
        blocks_commercial_completeness=False,
        blocking_rule_ids=[],
        unresolved_input_codes=[],
        open_conflict_ids=[uid(430)],
        global_revalidation_required=True,
        eligible_for_approval=False,
    )
    revision = DepartmentRevision(
        revision_id=uid(431),
        draft_id=uid(432),
        ticket_id=uid(433),
        approval_run_id=uid(434),
        department=DepartmentId.warehouse,
        review_revision_no=1,
        source_part2_draft_id=uid(435),
        source_part2_draft_hash=H1,
        source_part2_evaluation_id=uid(436),
        source_part2_evaluation_hash=H2,
        source_part2_iteration=2,
        content="Capacity conflict requires arbitration",
        content_hash=content_hash,
        commercial_terms=[],
        pending_commercial_items=[],
        evidence=[],
        validation=validation,
        status="blocked_conflict",
        creation_reason="part2_import",
        created_at=NOW,
    )
    assert revision.status == "blocked_conflict"
    assert revision.validation.open_conflict_ids == [uid(430)]
    with pytest.raises(ValidationError, match="conflict-only"):
        DepartmentRevision(
            **{
                **revision.model_dump(),
                "validation": validation.model_copy(
                    update={"mandatory_compliance_passed": False}
                ),
            }
        )


def test_blocker_precedence_preserves_conflicts_under_input_and_compliance() -> None:
    pending = MissingCommercialInput(
        code="WAREHOUSE_CAPACITY_REQUIRED",
        description="Capacity evidence is required",
        owner="Ana Whitfield",
        blocks_commercial_completeness=True,
    )
    content_hash = department_revision_content_hash(
        content="Blocked section",
        commercial_terms=[],
        pending_commercial_items=[pending],
        evidence=[],
    )
    input_validation = ApprovalRevisionValidation(
        references=revision_references(content_hash),
        mandatory_compliance_passed=False,
        blocks_commercial_completeness=True,
        blocking_rule_ids=["RETURNS_UNDER_48_HOURS"],
        unresolved_input_codes=["WAREHOUSE_CAPACITY_REQUIRED"],
        open_conflict_ids=[uid(440)],
        global_revalidation_required=True,
        eligible_for_approval=False,
    )
    base = dict(
        revision_id=uid(441), draft_id=uid(442), ticket_id=uid(443),
        approval_run_id=uid(444), department=DepartmentId.warehouse,
        review_revision_no=1, source_part2_draft_id=uid(445),
        source_part2_draft_hash=H1, source_part2_evaluation_id=uid(446),
        source_part2_evaluation_hash=H2, source_part2_iteration=2,
        content="Blocked section", content_hash=content_hash,
        commercial_terms=[], pending_commercial_items=[pending], evidence=[],
        validation=input_validation, creation_reason="part2_import", created_at=NOW,
    )
    input_blocked = DepartmentRevision(**base, status="blocked_input")
    assert input_blocked.validation.open_conflict_ids == [uid(440)]

    compliance_hash = department_revision_content_hash(
        content="Compliance and conflict blockers",
        commercial_terms=[], pending_commercial_items=[], evidence=[],
    )
    compliance_validation = ApprovalRevisionValidation(
        references=revision_references(compliance_hash),
        mandatory_compliance_passed=False,
        blocks_commercial_completeness=False,
        blocking_rule_ids=["RETURNS_UNDER_48_HOURS"],
        unresolved_input_codes=[], open_conflict_ids=[uid(447)],
        global_revalidation_required=True, eligible_for_approval=False,
    )
    compliance_blocked = DepartmentRevision(
        **{
            **base,
            "content": "Compliance and conflict blockers",
            "content_hash": compliance_hash,
            "pending_commercial_items": [],
            "validation": compliance_validation,
            "status": "blocked_compliance",
        }
    )
    assert compliance_blocked.validation.open_conflict_ids == [uid(447)]


def test_revision_limit_and_superseded_states_require_valid_lineage() -> None:
    content_hash = department_revision_content_hash(
        content="Needs correction",
        commercial_terms=[],
        pending_commercial_items=[],
        evidence=[],
    )
    validation = ApprovalRevisionValidation(
        references=revision_references(content_hash),
        mandatory_compliance_passed=False,
        blocks_commercial_completeness=False,
        blocking_rule_ids=["RULE"],
        unresolved_input_codes=[],
        open_conflict_ids=[],
        global_revalidation_required=True,
        eligible_for_approval=False,
    )
    base = dict(
        revision_id=uid(410),
        draft_id=uid(411),
        ticket_id=uid(412),
        approval_run_id=uid(413),
        department=DepartmentId.warehouse,
        source_part2_draft_id=uid(414),
        source_part2_draft_hash=H1,
        source_part2_evaluation_id=uid(415),
        source_part2_evaluation_hash=H2,
        source_part2_iteration=3,
        content="Needs correction",
        content_hash=content_hash,
        commercial_terms=[],
        pending_commercial_items=[],
        evidence=[],
        validation=validation,
        created_at=NOW,
    )
    with pytest.raises(ValidationError):
        DepartmentRevision(
            **base,
            review_revision_no=1,
            status="limit_reached",
            creation_reason="part2_import",
        )
    limit = DepartmentRevision(
        **base,
        review_revision_no=3,
        supersedes_revision_id=uid(409),
        status="limit_reached",
        created_by=ana(),
        creation_reason="human_request_changes",
    )
    assert limit.review_revision_no == 3
    with pytest.raises(ValidationError):
        DepartmentRevision(
            **base,
            review_revision_no=1,
            status="superseded",
            creation_reason="part2_import",
        )
    superseded = DepartmentRevision(
        **base,
        review_revision_no=1,
        superseded_by_revision_id=uid(416),
        status="superseded",
        creation_reason="part2_import",
    )
    assert superseded.superseded_by_revision_id == uid(416)


def test_proposal_revision_revalidates_its_actual_global_content() -> None:
    currency, sla, discounts = documented_globals()
    content_hash = proposal_revision_content_hash(
        currency=currency,
        sla=sla,
        volume_discounts=discounts,
        source_department_revision_ids=[uid(1)],
        authorized_input_ids=[uid(2)],
    )
    revision = ProposalRevision(
        revision_id=uid(10),
        approval_run_id=uid(11),
        ticket_id=uid(12),
        revision_no=1,
        source_global_draft_id=uid(13),
        source_global_draft_hash=H1,
        source_global_evaluation_id=uid(14),
        source_global_evaluation_hash=H2,
        currency=currency,
        sla=sla,
        volume_discounts=discounts,
        source_department_revision_ids=[uid(1)],
        authorized_input_ids=[uid(2)],
        content_hash=content_hash,
        validation=proposal_validation(content_hash),
        created_at=NOW,
    )
    assert revision.validation.eligible_for_synthesis
    with pytest.raises(ValidationError):
        ProposalRevision(**{**revision.model_dump(), "content_hash": H3})


def test_part3_artifacts_bind_draft_evaluation_preview_and_bundle() -> None:
    payload = DepartmentCorrectedDraftPayload(
        department=DepartmentId.warehouse,
        content="Corrected and re-evaluated warehouse section",
        commercial_terms=[],
        pending_commercial_items=[],
        evidence=[evidence()],
    )
    content_hash = department_revision_content_hash(
        content=payload.content,
        commercial_terms=payload.commercial_terms,
        pending_commercial_items=payload.pending_commercial_items,
        evidence=payload.evidence,
    )
    provenance_value = artifact_provenance()
    draft = CorrectedDraftArtifact(
        draft_artifact_id=uid(500), approval_run_id=uid(501), ticket_id=uid(502),
        scope_key="warehouse", payload=payload, content_hash=content_hash,
        payload_hash=canonical_hash(payload), provenance=provenance_value,
        created_at=NOW,
    )
    evaluation_payload = ContentEvaluationArtifactPayload(
        mandatory_compliance_passed=True,
        blocks_commercial_completeness=False,
        blocking_rule_ids=[], unresolved_input_codes=[], open_conflict_ids=[],
        eligible=True,
    )
    evaluation = ApprovalValidationArtifact(
        artifact_id=uid(503), artifact_type="evaluation",
        approval_run_id=draft.approval_run_id, ticket_id=draft.ticket_id,
        scope_key=draft.scope_key, corrected_draft_id=draft.draft_artifact_id,
        evaluated_content_hash=content_hash, payload=evaluation_payload,
        payload_hash=canonical_hash(evaluation_payload), provenance=provenance_value,
        created_at=NOW,
    )
    preview_payload = SafePreviewArtifactPayload(
        gate_status="passed",
        rendered_content="Corrected and re-evaluated warehouse section",
        rendered_content_hash=canonical_hash(
            {"rendered_content": "Corrected and re-evaluated warehouse section"}
        ),
        reason_codes=[],
    )
    preview = ApprovalValidationArtifact(
        artifact_id=uid(504), artifact_type="safe_preview",
        approval_run_id=draft.approval_run_id, ticket_id=draft.ticket_id,
        scope_key=draft.scope_key, corrected_draft_id=draft.draft_artifact_id,
        evaluated_content_hash=content_hash, payload=preview_payload,
        payload_hash=canonical_hash(preview_payload), provenance=provenance_value,
        created_at=NOW,
    )
    evaluation_reference = ImmutableArtifactReference(
        artifact_type=evaluation.artifact_type,
        artifact_id=evaluation.artifact_id,
        contract_version=evaluation.contract_version,
        payload_hash=evaluation.payload_hash,
        evaluated_content_hash=content_hash,
    )
    preview_reference = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=preview.artifact_id,
        contract_version=preview.contract_version,
        payload_hash=preview.payload_hash,
        evaluated_content_hash=content_hash,
    )
    revision_validation_hash = canonical_hash(
        {
            "revision_content_hash": content_hash,
            "evaluation": evaluation_reference,
            "safe_preview": preview_reference,
            "validated_at": NOW,
        }
    )
    bundle_data = {
        "approval_run_id": draft.approval_run_id,
        "ticket_id": draft.ticket_id,
        "scope_key": draft.scope_key,
        "corrected_draft_id": draft.draft_artifact_id,
        "content_hash": content_hash,
        "evaluation_artifact_id": evaluation.artifact_id,
        "evaluation_artifact_type": evaluation.artifact_type,
        "evaluation_contract_version": evaluation.contract_version,
        "evaluation_payload_hash": evaluation.payload_hash,
        "safe_preview_artifact_id": preview.artifact_id,
        "safe_preview_contract_version": preview.contract_version,
        "safe_preview_payload_hash": preview.payload_hash,
        "revision_validation_hash": revision_validation_hash,
        "mandatory_compliance_passed": True,
        "blocks_commercial_completeness": False,
        "blocking_rule_ids": [],
        "unresolved_input_codes": [],
        "open_conflict_ids": [],
        "source_departments_approved": False,
        "eligible": True,
        "provenance": provenance_value,
        "validated_at": NOW,
    }
    bundle = ApprovalValidationBundle(
        validation_bundle_id=uid(505),
        **bundle_data,
        bundle_hash=canonical_hash(bundle_data),
    )
    assert bundle.content_hash == draft.content_hash
    assert bundle.evaluation_payload_hash == evaluation.payload_hash
    assert bundle.safe_preview_payload_hash == preview.payload_hash

    with pytest.raises(ValidationError, match="type and scope"):
        ApprovalValidationArtifact(
            **{
                **evaluation.model_dump(),
                "scope_key": "proposal",
            }
        )
    with pytest.raises(ValidationError, match="content hash"):
        CorrectedDraftArtifact(**{**draft.model_dump(), "content_hash": H3})


def test_global_bundle_needs_approved_departments_and_no_open_conflicts() -> None:
    currency, sla, discounts = documented_globals()
    content_hash = proposal_revision_content_hash(
        currency=currency, sla=sla, volume_discounts=discounts,
        source_department_revision_ids=[uid(510)], authorized_input_ids=[],
    )
    validation = proposal_validation(
        content_hash,
        source_departments_approved=False,
        open_conflict_ids=[uid(511)],
    )
    assert validation.eligible_for_synthesis is False
    with pytest.raises(ValidationError):
        ProposalRevisionValidation(
            **{
                **validation.model_dump(),
                "eligible_for_synthesis": True,
            }
        )


def test_v1_global_validation_hash_remains_backward_compatible() -> None:
    data = {
        "revision_content_hash": H1,
        "global_evaluation": artifact("global_evaluation", H1, 512),
        "safe_preview": artifact("safe_preview", H1, 513),
        "mandatory_compliance_passed": True,
        "unresolved_input_codes": [],
        "blocking_rule_ids": [],
        "open_conflict_ids": [],
        "eligible_for_synthesis": True,
        "validated_at": NOW,
    }
    legacy = ProposalRevisionValidation(
        **data,
        validation_hash=canonical_hash(data),
    )
    assert legacy.contract_version == "rfp-proposal-revision-validation-v1"
    assert legacy.source_departments_approved is None


def test_initial_proposal_distinguishes_part2_import_from_human_change() -> None:
    currency, sla, discounts = documented_globals()
    content_hash = proposal_revision_content_hash(
        currency=currency, sla=sla, volume_discounts=discounts,
        source_department_revision_ids=[uid(520)], authorized_input_ids=[],
    )
    proposal = ProposalRevision(
        revision_id=uid(521), approval_run_id=uid(522), ticket_id=uid(523),
        revision_no=1, source_global_draft_id=uid(524),
        source_global_draft_hash=H1, source_global_evaluation_id=uid(525),
        source_global_evaluation_hash=H2, currency=currency, sla=sla,
        volume_discounts=discounts, source_department_revision_ids=[uid(520)],
        authorized_input_ids=[], content_hash=content_hash,
        validation=proposal_validation(content_hash),
        validation_bundle_id=uid(526), created_at=NOW,
    )
    manager = RunInitiator(
        principal=principal(role="manager"), authorization_id=uid(527),
        permission="manage_approval_run",
    )
    imported = InitialProposalRevisionCommand(
        creation_mode="part2_import", response_run_id=uid(528),
        response_handoff_hash=H3, proposal=proposal,
        validation_bundle_id=uid(526), imported_by=manager,
        idempotency_key=uid(529), requested_at=NOW,
    )
    assert imported.created_by is None

    changed_hash = proposal_revision_content_hash(
        currency=currency, sla=sla, volume_discounts=discounts,
        source_department_revision_ids=[uid(520)], authorized_input_ids=[uid(530)],
    )
    changed = ProposalRevision(
        **{
            **proposal.model_dump(),
            "authorized_input_ids": [uid(530)],
            "content_hash": changed_hash,
            "validation": proposal_validation(changed_hash),
        }
    )
    with pytest.raises(ValidationError, match="cannot claim human changes"):
        InitialProposalRevisionCommand(
            **{
                **imported.model_dump(),
                "proposal": changed,
            }
        )

    human = InitialProposalRevisionCommand(
        creation_mode="authorized_commercial_change", response_run_id=uid(528),
        response_handoff_hash=H3, proposal=changed,
        validation_bundle_id=uid(526), created_by=miguel(),
        idempotency_key=uid(531), requested_at=NOW,
    )
    assert human.created_by.official_responsible == "Miguel Torres"


def test_volume_capacity_distinguishes_conflict_from_missing_or_incomparable_data() -> None:
    required = quantity("1000")
    capacity = quantity("900")
    demonstrated = VolumeCapacityFacts(
        required_volume=required,
        available_capacity=capacity,
        capacity_input_id=uid(1),
        comparison_status="comparable",
        exceeds_capacity=True,
    )
    assert demonstrated.exceeds_capacity is True
    missing = VolumeCapacityFacts(
        required_volume=required,
        comparison_status="missing_capacity",
        exceeds_capacity=None,
    )
    assert missing.exceeds_capacity is None
    with pytest.raises(ValidationError):
        VolumeCapacityFacts(
            required_volume=required,
            available_capacity=quantity("2", unit="pallet", period="day"),
            capacity_input_id=uid(2),
            comparison_status="comparable",
            exceeds_capacity=True,
        )
    normalized = VolumeCapacityFacts(
        required_volume=required,
        available_capacity=quantity("2", unit="pallet", period="day"),
        capacity_input_id=uid(2),
        normalization_input_id=uid(3),
        normalized_required_volume=quantity("1000"),
        normalized_available_capacity=quantity("900"),
        comparison_status="comparable",
        exceeds_capacity=True,
    )
    assert normalized.exceeds_capacity is True


def test_volume_capacity_requires_matching_dimension_and_operational_scope() -> None:
    with pytest.raises(ValidationError):
        VolumeCapacityFacts(
            required_volume=quantity("100", location="WH-MAD"),
            available_capacity=quantity("150", location="WH-LAX"),
            capacity_input_id=uid(420),
            comparison_status="comparable",
            exceeds_capacity=False,
        )
    incompatible = VolumeCapacityFacts(
        required_volume=quantity("100", location="WH-MAD"),
        available_capacity=quantity("150", location="WH-LAX"),
        capacity_input_id=uid(420),
        comparison_status="incompatible_units",
        exceeds_capacity=None,
    )
    assert incompatible.exceeds_capacity is None
    with pytest.raises(ValidationError):
        VolumeCapacityFacts(
            required_volume=quantity("100", location="WH-MAD"),
            available_capacity=quantity("150", location="WH-LAX"),
            capacity_input_id=uid(420),
            normalization_input_id=uid(421),
            normalized_required_volume=quantity("100", location="WH-MAD"),
            normalized_available_capacity=quantity("150", location="WH-LAX"),
            comparison_status="comparable",
            exceeds_capacity=False,
        )


def test_sufficient_capacity_is_no_conflict_not_missing_information() -> None:
    facts = VolumeCapacityFacts(
        required_volume=quantity("100"),
        available_capacity=quantity("150"),
        capacity_input_id=uid(430),
        comparison_status="comparable",
        exceeds_capacity=False,
    )
    assessment = conflict_assessment(facts, "no_conflict")
    assert assessment.outcome == "no_conflict"


def test_volume_capacity_is_not_applicable_without_both_departments() -> None:
    facts = VolumeCapacityFacts(
        applicable=False,
        comparison_status="not_applicable",
        exceeds_capacity=None,
    )
    assessment = ConflictAssessment(
        assessment_id=uid(431),
        trigger=facts.trigger,
        facts=facts,
        outcome="no_conflict",
        missing_input_codes=[],
        assessment_hash=conflict_assessment_hash(
            trigger=facts.trigger,
            facts=facts,
            outcome="no_conflict",
            missing_input_codes=[],
        ),
        assessed_at=NOW,
    )

    assert assessment.outcome == "no_conflict"


def test_returns_sla_can_prove_no_conflict_when_no_promise_is_present() -> None:
    facts = ReturnsSLAFacts(
        observed=[],
        violating_revision_ids=[],
        conflict_demonstrated=False,
    )

    assert facts.conflict_demonstrated is False
    targets = [
        ArbitrationTarget(
            department=DepartmentId.warehouse,
            department_revision_id=uid(431),
        )
    ]
    with pytest.raises(ValidationError):
        ArbitrationConflict(
            conflict_id=uid(432),
            approval_run_id=uid(433),
            trigger="volume-vs-capacity",
            facts=facts,
            affected_targets=targets,
            status="insufficient_information",
            missing_input_codes=["WAREHOUSE_CAPACITY_REQUIRED"],
            fingerprint=arbitration_conflict_fingerprint(
                trigger="volume-vs-capacity",
                facts=facts,
                affected_targets=targets,
            ),
            detected_at=NOW,
        )


def test_returns_sla_violation_is_strictly_less_than_48_hours() -> None:
    facts = ReturnsSLAFacts(
        observed=[
            ReturnSLAViolation(department_revision_id=uid(1), sla_hours=Decimal("48")),
            ReturnSLAViolation(department_revision_id=uid(2), sla_hours=Decimal("47")),
        ],
        violating_revision_ids=[uid(2)],
        conflict_demonstrated=True,
    )
    assert facts.violating_revision_ids == [uid(2)]
    with pytest.raises(ValidationError):
        ReturnsSLAFacts(
            observed=[ReturnSLAViolation(department_revision_id=uid(1), sla_hours=Decimal("48"))],
            violating_revision_ids=[uid(1)],
            conflict_demonstrated=True,
        )


def test_currency_mismatch_derives_country_currency_and_all_offending_revisions() -> None:
    facts = CurrencyMismatchFacts(
        client_country="US",
        expected_currency="USD",
        proposal_revision_id=uid(1),
        proposal_currency="EUR",
        monetary_terms=[
            MonetaryTermFact(
                term_code="RATE",
                department_revision_id=uid(2),
                currency="EUR",
                amount=Decimal("10"),
            )
        ],
        mismatched_revision_ids=[uid(1), uid(2)],
        conflict_demonstrated=True,
    )
    assert facts.expected_currency == "USD"
    with pytest.raises(ValidationError):
        CurrencyMismatchFacts(
            **{**facts.model_dump(), "expected_currency": "EUR"}
        )


def test_arbitration_conflict_has_real_discriminator_and_fact_fingerprint() -> None:
    facts = VolumeCapacityFacts(
        required_volume=quantity("1000"),
        available_capacity=quantity("900"),
        capacity_input_id=uid(1),
        comparison_status="comparable",
        exceeds_capacity=True,
    )
    targets = [
        ArbitrationTarget(
            department=DepartmentId.warehouse,
            department_revision_id=uid(2),
        )
    ]
    fingerprint = arbitration_conflict_fingerprint(
        trigger="volume-vs-capacity", facts=facts, affected_targets=targets
    )
    conflict = ArbitrationConflict(
        conflict_id=uid(3),
        approval_run_id=uid(4),
        trigger="volume-vs-capacity",
        facts=facts,
        affected_targets=targets,
        status="demonstrated",
        missing_input_codes=[],
        fingerprint=fingerprint,
        detected_at=NOW,
    )
    assert conflict.fingerprint == fingerprint
    with pytest.raises(ValidationError):
        ArbitrationConflict(**{**conflict.model_dump(), "trigger": "currency-mismatch"})


def test_incomplete_arbitration_facts_request_inputs_instead_of_inventing_values() -> None:
    facts = VolumeCapacityFacts(
        required_volume=quantity("1000"),
        comparison_status="missing_capacity",
        exceeds_capacity=None,
    )
    targets = [ArbitrationTarget(proposal_revision_id=uid(2))]
    conflict = ArbitrationConflict(
        conflict_id=uid(1),
        approval_run_id=uid(3),
        trigger="volume-vs-capacity",
        facts=facts,
        affected_targets=targets,
        status="insufficient_information",
        missing_input_codes=["WAREHOUSE_CAPACITY_REQUIRED"],
        fingerprint=arbitration_conflict_fingerprint(
            trigger="volume-vs-capacity", facts=facts, affected_targets=targets
        ),
        detected_at=NOW,
    )
    assert conflict.status == "insufficient_information"


def test_deterministic_rule_execution_is_not_attributed_to_a_human() -> None:
    execution_hash = deterministic_execution_hash(
        conflict_id=uid(1),
        rule_id="currency-country-v1",
        outcome="route_to_arbitration",
        resulting_status="pending_arbitration",
    )
    execution = DeterministicRuleExecution(
        execution_id=uid(2),
        conflict_id=uid(1),
        rule_id="currency-country-v1",
        outcome="route_to_arbitration",
        resulting_status="pending_arbitration",
        execution_hash=execution_hash,
        executed_at=NOW,
    )
    assert "actor" not in DeterministicRuleExecution.model_fields


def test_sofia_resolution_can_only_target_reverse_revision() -> None:
    sofia_arbitrator = actor(
        "Sofía Ramos",
        "arbitrate",
        department=DepartmentId.reverse,
        trigger="returns-sla-breach",
    )
    execution = HumanArbitrationExecution(
        execution_id=uid(1), conflict_id=uid(2), actor=sofia_arbitrator, decided_at=NOW
    )
    target = ArbitrationTarget(
        department=DepartmentId.reverse, department_revision_id=uid(3)
    )
    resolution_hash = arbitration_resolution_hash(
        conflict_id=uid(2),
        conflict_fingerprint=H1,
        action="request_changes",
        targets=[target],
        authorized_input_ids=[],
        reason="Unsupported return promise below 48 hours",
    )
    resolution = ArbitrationResolution(
        resolution_id=uid(4),
        conflict_id=uid(2),
        conflict_fingerprint=H1,
        trigger="returns-sla-breach",
        action="request_changes",
        targets=[target],
        authorized_input_ids=[],
        reason="Unsupported return promise below 48 hours",
        execution=execution,
        resolution_hash=resolution_hash,
    )
    assert resolution.action == "request_changes"
    wrong_target = ArbitrationTarget(
        department=DepartmentId.warehouse, department_revision_id=uid(5)
    )
    with pytest.raises(ValidationError):
        ArbitrationResolution(
            **{
                **resolution.model_dump(),
                "targets": [wrong_target],
                "resolution_hash": arbitration_resolution_hash(
                    conflict_id=uid(2),
                    conflict_fingerprint=H1,
                    action="request_changes",
                    targets=[wrong_target],
                    authorized_input_ids=[],
                    reason="Unsupported return promise below 48 hours",
                ),
            }
        )


def test_miguel_cannot_resolve_reverse_only_return_sla_breach() -> None:
    target = ArbitrationTarget(
        department=DepartmentId.reverse,
        department_revision_id=uid(440),
    )
    execution = HumanArbitrationExecution(
        execution_id=uid(441),
        conflict_id=uid(442),
        actor=miguel("arbitrate", "returns-sla-breach"),
        decided_at=NOW,
    )
    reason = "Reverse-only breach belongs to Sofía"
    with pytest.raises(ValidationError):
        ArbitrationResolution(
            resolution_id=uid(443),
            conflict_id=uid(442),
            conflict_fingerprint=H1,
            trigger="returns-sla-breach",
            action="request_changes",
            targets=[target],
            authorized_input_ids=[],
            reason=reason,
            execution=execution,
            resolution_hash=arbitration_resolution_hash(
                conflict_id=uid(442),
                conflict_fingerprint=H1,
                action="request_changes",
                targets=[target],
                authorized_input_ids=[],
                reason=reason,
            ),
        )

    other_section = ArbitrationTarget(
        department=DepartmentId.warehouse,
        department_revision_id=uid(444),
    )
    allowed_reason = "Return-SLA infringement appears outside Reverse"
    allowed = ArbitrationResolution(
        resolution_id=uid(445),
        conflict_id=uid(442),
        conflict_fingerprint=H1,
        trigger="returns-sla-breach",
        action="request_changes",
        targets=[other_section],
        authorized_input_ids=[],
        reason=allowed_reason,
        execution=execution,
        resolution_hash=arbitration_resolution_hash(
            conflict_id=uid(442),
            conflict_fingerprint=H1,
            action="request_changes",
            targets=[other_section],
            authorized_input_ids=[],
            reason=allowed_reason,
        ),
    )
    assert allowed.targets == [other_section]


def test_accepting_return_sla_resolution_requires_corrected_recheck() -> None:
    target = ArbitrationTarget(
        department=DepartmentId.reverse,
        department_revision_id=uid(450),
    )
    execution = HumanArbitrationExecution(
        execution_id=uid(451),
        conflict_id=uid(452),
        actor=actor(
            "Sofía Ramos",
            "arbitrate",
            department=DepartmentId.reverse,
            trigger="returns-sla-breach",
        ),
        decided_at=NOW,
    )
    reason = "Corrected promise was re-evaluated"
    with pytest.raises(ValidationError):
        ArbitrationResolution(
            resolution_id=uid(453),
            conflict_id=uid(452),
            conflict_fingerprint=H1,
            trigger="returns-sla-breach",
            action="accept_resolution",
            targets=[target],
            authorized_input_ids=[],
            reason=reason,
            execution=execution,
            resolution_hash=arbitration_resolution_hash(
                conflict_id=uid(452),
                conflict_fingerprint=H1,
                action="accept_resolution",
                targets=[target],
                authorized_input_ids=[],
                reason=reason,
            ),
        )
    corrected_facts = ReturnsSLAFacts(
        observed=[
            ReturnSLAViolation(
                department_revision_id=uid(454),
                sla_hours=Decimal("48"),
            )
        ],
        violating_revision_ids=[],
        conflict_demonstrated=False,
    )
    assessment = conflict_assessment(corrected_facts, "no_conflict")
    corrected = CorrectedRevisionEvidence(
        original_target=target,
        corrected_revision_id=uid(454),
        corrected_content_hash=H3,
        revision_validation=artifact("revision_validation", H3, 455),
        conflict_recheck=ImmutableArtifactReference(
            artifact_type="conflict_recheck",
            artifact_id=uid(456),
            contract_version="rfp-conflict-assessment-v1",
            payload_hash=assessment.assessment_hash,
            evaluated_content_hash=H3,
        ),
    )
    resolution_hash = arbitration_resolution_hash(
        conflict_id=uid(452),
        conflict_fingerprint=H1,
        action="accept_resolution",
        targets=[target],
        authorized_input_ids=[],
        reason=reason,
        corrected_evidence=[corrected],
        post_correction_assessment=assessment,
    )
    resolution = ArbitrationResolution(
        resolution_id=uid(453),
        conflict_id=uid(452),
        conflict_fingerprint=H1,
        trigger="returns-sla-breach",
        action="accept_resolution",
        targets=[target],
        authorized_input_ids=[],
        reason=reason,
        execution=execution,
        corrected_evidence=[corrected],
        post_correction_assessment=assessment,
        resolution_hash=resolution_hash,
    )
    assert resolution.post_correction_assessment is not None
    assert resolution.post_correction_assessment.outcome == "no_conflict"

    unrelated_facts = ReturnsSLAFacts(
        observed=[
            ReturnSLAViolation(
                department_revision_id=uid(457),
                sla_hours=Decimal("48"),
            )
        ],
        violating_revision_ids=[],
        conflict_demonstrated=False,
    )
    unrelated_assessment = conflict_assessment(unrelated_facts, "no_conflict")
    unrelated_evidence = corrected.model_copy(
        update={
            "conflict_recheck": corrected.conflict_recheck.model_copy(
                update={"payload_hash": unrelated_assessment.assessment_hash}
            )
        }
    )
    with pytest.raises(ValidationError):
        ArbitrationResolution(
            resolution_id=uid(458),
            conflict_id=uid(452),
            conflict_fingerprint=H1,
            trigger="returns-sla-breach",
            action="accept_resolution",
            targets=[target],
            authorized_input_ids=[],
            reason=reason,
            execution=execution,
            corrected_evidence=[unrelated_evidence],
            post_correction_assessment=unrelated_assessment,
            resolution_hash=arbitration_resolution_hash(
                conflict_id=uid(452),
                conflict_fingerprint=H1,
                action="accept_resolution",
                targets=[target],
                authorized_input_ids=[],
                reason=reason,
                corrected_evidence=[unrelated_evidence],
                post_correction_assessment=unrelated_assessment,
            ),
        )


def test_return_sla_breach_cannot_be_accepted_as_resolved() -> None:
    target = ArbitrationTarget(
        department=DepartmentId.reverse,
        department_revision_id=uid(460),
    )
    breach = ReturnsSLAFacts(
        observed=[
            ReturnSLAViolation(
                department_revision_id=uid(461),
                sla_hours=Decimal("24"),
            )
        ],
        violating_revision_ids=[uid(461)],
        conflict_demonstrated=True,
    )
    breach_assessment = conflict_assessment(breach, "demonstrated")
    corrected = CorrectedRevisionEvidence(
        original_target=target,
        corrected_revision_id=uid(461),
        corrected_content_hash=H3,
        revision_validation=artifact("revision_validation", H3, 462),
        conflict_recheck=ImmutableArtifactReference(
            artifact_type="conflict_recheck",
            artifact_id=uid(463),
            contract_version="rfp-conflict-assessment-v1",
            payload_hash=breach_assessment.assessment_hash,
            evaluated_content_hash=H3,
        ),
    )
    execution = HumanArbitrationExecution(
        execution_id=uid(464),
        conflict_id=uid(465),
        actor=actor(
            "Sofía Ramos",
            "arbitrate",
            department=DepartmentId.reverse,
            trigger="returns-sla-breach",
        ),
        decided_at=NOW,
    )
    reason = "Attempted acceptance without resolving the breach"
    with pytest.raises(ValidationError):
        ArbitrationResolution(
            resolution_id=uid(466),
            conflict_id=uid(465),
            conflict_fingerprint=H1,
            trigger="returns-sla-breach",
            action="accept_resolution",
            targets=[target],
            authorized_input_ids=[],
            reason=reason,
            execution=execution,
            corrected_evidence=[corrected],
            post_correction_assessment=breach_assessment,
            resolution_hash=arbitration_resolution_hash(
                conflict_id=uid(465),
                conflict_fingerprint=H1,
                action="accept_resolution",
                targets=[target],
                authorized_input_ids=[],
                reason=reason,
                corrected_evidence=[corrected],
                post_correction_assessment=breach_assessment,
            ),
        )


def persisted_document() -> PersistedFinalDocument:
    currency, sla, discounts = documented_globals()
    section = FinalDepartmentSection(
        department=DepartmentId.warehouse,
        title="Warehouse",
        markdown="## Warehouse\nApproved.",
        approved_revision_id=uid(1),
        approved_content_hash=H1,
        approval_decision_id=uid(2),
    )
    manifest = FinalDocumentManifest(
        ticket_id=uid(3),
        approval_run_id=uid(4),
        response_run_id=uid(5),
        department_revision_ids=[uid(1)],
        approval_decision_ids=[uid(2)],
        proposal_revision_id=uid(6),
        arbitration_resolution_ids=[uid(7)],
        authorized_input_ids=[uid(8)],
    )
    payload = FinalDocumentPayload(
        title="TrackFlow proposal",
        executive_summary_markdown="# Executive summary",
        sections=[section],
        global_terms=FinalGlobalTerms(
            currency=currency,
            sla=sla,
            volume_discounts=discounts,
            proposal_revision_id=uid(6),
            proposal_content_hash=H2,
        ),
        manifest=manifest,
    )
    markdown = "# TrackFlow proposal\n\nApproved content."
    import hashlib

    return PersistedFinalDocument(
        final_document_id=uid(9),
        payload=payload,
        payload_hash=canonical_hash(payload),
        markdown=markdown,
        markdown_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        storage_reference="postgres://logical/final/9",
        generated_at=NOW,
    )


def test_final_document_manifest_is_tied_to_sections_and_proposal() -> None:
    document = persisted_document()
    assert document.payload.manifest.department_revision_ids == [uid(1)]
    bad_payload = document.payload.model_dump()
    bad_payload["manifest"]["department_revision_ids"] = [uid(99)]
    with pytest.raises(ValidationError):
        FinalDocumentPayload(**bad_payload)


def test_final_document_rejects_pending_global_currency() -> None:
    _, sla, discounts = documented_globals()
    with pytest.raises(ValidationError):
        FinalGlobalTerms(
            currency=GlobalCurrency(value=None, status="pending", evidence=[]),
            sla=sla,
            volume_discounts=discounts,
            proposal_revision_id=uid(470),
            proposal_content_hash=H1,
        )


def test_markdown_hash_proves_bytes_not_payload_equivalence() -> None:
    import hashlib

    document = persisted_document()
    unrelated_markdown = "# Independently rendered bytes"
    rebuilt = PersistedFinalDocument(
        **{
            **document.model_dump(),
            "markdown": unrelated_markdown,
            "markdown_hash": hashlib.sha256(
                unrelated_markdown.encode("utf-8")
            ).hexdigest(),
        }
    )
    assert rebuilt.markdown != document.markdown
    assert rebuilt.payload_hash == document.payload_hash


def test_public_projection_excludes_identity_audit_and_storage_metadata() -> None:
    public = public_final_document(persisted_document(), download_filename="trackflow-proposal.md")
    serialized = public.model_dump(mode="json")
    forbidden = {
        "issuer",
        "subject",
        "trackflow_user_id",
        "authorization_id",
        "approval_decision_id",
        "approval_decision_ids",
        "storage_reference",
        "evidence",
        "manifest",
        "authorized_input_ids",
    }

    def all_keys(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                yield key
                yield from all_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from all_keys(nested)

    assert forbidden.isdisjoint(set(all_keys(serialized)))
    assert public.markdown_hash == persisted_document().markdown_hash
    with pytest.raises(ValidationError):
        public_final_document(persisted_document(), download_filename="../secret.md")


def test_canonical_contract_payload_is_reproducible_and_json_safe() -> None:
    document = persisted_document()
    assert canonical_contract_payload(document.payload) == canonical_contract_payload(
        document.payload.model_copy(deep=True)
    )
