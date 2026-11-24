from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.hashing import canonical_hash
from data.pipelines.rfp_response.hashing import canonical_value
from data.pipelines.rfp_response.contracts import (
    DepartmentHandoffResult,
    EvidenceReference,
    GlobalCurrency,
    GlobalSLA,
    RFPResponseHandoff,
)
from pipelines.rfp_approval.contracts import (
    ApprovalRevisionValidation,
    ApprovalArtifactProvenance,
    AuthenticatedPrincipal,
    AuthorizedBusinessInput,
    AuthorizedHumanActor,
    BusinessInputProvenance,
    ClientPriceQuoteInput,
    CurrencyConversionInput,
    CurrencyQuoteInput,
    DepartmentRevision,
    GlobalCommercialTermInput,
    InitialProposalRevisionCommand,
    ImmutableArtifactReference,
    OperationalScopeInput,
    ProposalRevision,
    ProposalRevisionValidation,
    RevisionValidationReferences,
    RunInitiator,
    VolumeDiscountInput,
    VolumeDiscountTierInput,
    WarehouseCapacityInput,
    authorized_business_input_hash,
    proposal_revision_content_hash,
)
from services.rfp_approval.authorization import AuthorizationReference
from services.rfp_approval.commercial_store import (
    _assert_revision_replay_actor,
    _derived_department_status,
    _derived_proposal_eligibility,
    _lock_active_run,
    _lock_validation_bundle,
    _target,
    _verify_part2_global_source,
    _validated_handoff,
    create_corrected_department_revision,
    create_corrected_proposal_revision,
    create_initial_proposal_revision,
    preview_artifact_payload_hash,
    record_authorized_business_input,
)
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.store import ApprovalStoreError


NOW = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
RUN_ID = UUID("20000000-0000-0000-0000-000000000001")
TICKET_ID = UUID("20000000-0000-0000-0000-000000000002")
PRINCIPAL_ID = UUID("20000000-0000-0000-0000-000000000003")
BINDING_ID = UUID("20000000-0000-0000-0000-000000000004")
AUTHORIZATION_ID = UUID("20000000-0000-0000-0000-000000000005")
INPUT_ID = UUID("20000000-0000-0000-0000-000000000006")
KEY = UUID("20000000-0000-0000-0000-000000000007")
EXECUTION_ID = UUID("20000000-0000-0000-0000-000000000008")
H64 = "a" * 64
H64_B = "b" * 64


class _Result:
    def __init__(self, value=None, *, rowcount=1):
        self.value = value
        self.rowcount = rowcount

    def mappings(self):
        return self

    def one_or_none(self):
        return self.value

    def one(self):
        if self.value is None:
            raise AssertionError("expected row")
        return self.value

    def all(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self


class _Session:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(statement)
        if not self.results:
            raise AssertionError(f"unexpected statement: {statement}")
        return self.results.pop(0)


def _principal(global_actor=False):
    suffix = "miguel" if global_actor else "ana"
    return AuthenticatedPrincipal(
        issuer="https://identity.trackflow.example/realms/trackflow",
        subject=f"subject-{suffix}",
        trackflow_user_id=f"user-{suffix}",
        auth_source="oidc",
        general_role="user",
    )


def _actor(global_actor=False):
    return AuthorizedHumanActor(
        principal=_principal(global_actor),
        authorization_id=AUTHORIZATION_ID,
        official_responsible="Miguel Torres" if global_actor else "Ana Whitfield",
        permission="provide_business_input",
        department_scope=None if global_actor else DepartmentId.warehouse,
    )


def _identity(global_actor=False):
    principal = _principal(global_actor)
    return ResolvedIdentity._from_persisted_binding(
        environment="production",
        auth_source="oidc",
        issuer=principal.issuer,
        subject=principal.subject,
        trackflow_user_id=principal.trackflow_user_id,
        general_role="user",
        principal_id=PRINCIPAL_ID,
        binding_id=BINDING_ID,
        responsible_code="miguel_torres" if global_actor else "ana_whitfield",
        synthetic=False,
    )


def _replay_event(identity=None, reference=None, **changes):
    identity = identity or _identity(False)
    reference = reference or AuthorizationReference(AUTHORIZATION_ID, 1)
    row = {
        "actor_principal_id": identity.principal_id,
        "actor_binding_id": identity.binding_id,
        "environment": identity.environment,
        "authorization_id": reference.authorization_id,
        "authorization_version": reference.authorization_version,
    }
    row.update(changes)
    return row


def _provenance(kind):
    return BusinessInputProvenance(
        evidence_type=kind,
        evidence_id=UUID("20000000-0000-0000-0000-000000000009"),
        source_name="Approved TrackFlow source",
        issued_at=NOW,
        source_payload_hash=H64,
    )


def _corrected_revision():
    evidence = [EvidenceReference(
        document_id="authorized-business-input-v1",
        section_id=str(INPUT_ID),
        evidence_hash=H64,
    )]
    content = "Corrected warehouse response"
    content_hash = canonical_hash({
        "content": content,
        "commercial_terms": [],
        "pending_commercial_items": [],
        "evidence": evidence,
    })
    evaluation = ImmutableArtifactReference(
        artifact_type="evaluation",
        artifact_id=UUID("20000000-0000-0000-0000-000000000010"),
        contract_version="section-evaluation-v1",
        payload_hash=H64_B,
        evaluated_content_hash=content_hash,
    )


    preview = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=UUID("20000000-0000-0000-0000-000000000011"),
        contract_version="rfp-safe-preview-v1",
        payload_hash=H64,
        evaluated_content_hash=content_hash,
    )
    validated_at = NOW
    validation_hash = canonical_hash({
        "revision_content_hash": content_hash,
        "evaluation": evaluation,
        "safe_preview": preview,
        "validated_at": validated_at,
    })
    references = RevisionValidationReferences(
        revision_content_hash=content_hash,
        evaluation=evaluation,
        safe_preview=preview,
        validation_hash=validation_hash,
        validated_at=validated_at,
    )
    return DepartmentRevision(
        revision_id=UUID("20000000-0000-0000-0000-000000000012"),
        draft_id=UUID("20000000-0000-0000-0000-000000000013"),
        ticket_id=TICKET_ID,
        approval_run_id=RUN_ID,
        department=DepartmentId.warehouse,
        review_revision_no=2,
        source_part2_draft_id=UUID("20000000-0000-0000-0000-000000000014"),
        source_part2_draft_hash=H64,
        source_part2_evaluation_id=UUID("20000000-0000-0000-0000-000000000015"),
        source_part2_evaluation_hash=H64_B,
        source_part2_iteration=3,
        supersedes_revision_id=UUID("20000000-0000-0000-0000-000000000016"),
        content=content,
        content_hash=content_hash,
        commercial_terms=[],
        pending_commercial_items=[],
        evidence=evidence,
        validation=ApprovalRevisionValidation(
            references=references,
            mandatory_compliance_passed=True,
            blocks_commercial_completeness=False,
            blocking_rule_ids=[],
            unresolved_input_codes=[],
            open_conflict_ids=[],
            global_revalidation_required=True,
            eligible_for_approval=True,
        ),
        validation_bundle_id=UUID(
            "20000000-0000-0000-0000-000000000018"
        ),
        status="pending_approval",
        created_by=_actor(False),
        creation_reason="authorized_input",
        created_at=NOW,
    )


def _persisted_bundle_fixture(*, preview_status="passed"):
    revision = _corrected_revision()
    evaluation_payload = {
        "payload_kind": "content_evaluation",
        "mandatory_compliance_passed": True,
        "blocks_commercial_completeness": False,
        "blocking_rule_ids": [],
        "unresolved_input_codes": [],
        "open_conflict_ids": [],
        "eligible": True,
    }
    preview_payload = (
        {
            "payload_kind": "safe_preview",
            "gate_status": "passed",
            "rendered_content": revision.content,
            "rendered_content_hash": canonical_hash(
                {"rendered_content": revision.content}
            ),
            "reason_codes": [],
        }
        if preview_status == "passed"
        else {
            "payload_kind": "safe_preview",
            "gate_status": "blocked",
            "rendered_content": None,
            "rendered_content_hash": None,
            "reason_codes": ["unsafe"],
        }
    )
    evaluation = revision.validation.references.evaluation.model_copy(
        update={"payload_hash": canonical_hash(evaluation_payload)}
    )
    preview = revision.validation.references.safe_preview.model_copy(
        update={"payload_hash": canonical_hash(preview_payload)}
    )
    validation_hash = canonical_hash(
        {
            "revision_content_hash": revision.content_hash,
            "evaluation": evaluation,
            "safe_preview": preview,
            "validated_at": NOW,
        }
    )
    validation = revision.validation.model_copy(
        update={
            "references": RevisionValidationReferences(
                revision_content_hash=revision.content_hash,
                evaluation=evaluation,
                safe_preview=preview,
                validation_hash=validation_hash,
                validated_at=NOW,
            )
        }
    )
    revision = revision.model_copy(update={"validation": validation})
    draft_id = UUID("20000000-0000-0000-0000-000000000019")
    draft_payload = canonical_value(
        {
            "payload_kind": "department_corrected_draft",
            "department": revision.department,
            "content": revision.content,
            "commercial_terms": revision.commercial_terms,
            "pending_commercial_items": revision.pending_commercial_items,
            "evidence": revision.evidence,
        }
    )
    bundle = {
        "validation_bundle_id": revision.validation_bundle_id,
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "scope_key": "warehouse",
        "corrected_draft_id": draft_id,
        "content_hash": revision.content_hash,
        "evaluation_artifact_id": evaluation.artifact_id,
        "evaluation_contract_version": evaluation.contract_version,
        "evaluation_payload_hash": evaluation.payload_hash,
        "safe_preview_artifact_id": preview.artifact_id,
        "safe_preview_contract_version": preview.contract_version,
        "safe_preview_payload_hash": preview.payload_hash,
        "revision_validation_hash": validation_hash,
        "mandatory_compliance_passed": True,
        "blocks_commercial_completeness": False,
        "blocking_rule_ids": [],
        "unresolved_input_codes": [],
        "open_conflict_ids": [],
        "source_departments_approved": False,
        "eligible": True,
        "validated_at": NOW,
    }
    provenance_data = {
        "producer_node": "s2a-corrected-draft",
        "execution_id": EXECUTION_ID,
        "source_kind": "human_correction",
        "source_artifact_ids": [INPUT_ID],
        "source_hashes": [H64],
        "produced_at": NOW,
    }
    provenance = ApprovalArtifactProvenance(
        **provenance_data,
        provenance_hash=canonical_hash(provenance_data),
    )
    draft = {
        "draft_artifact_id": draft_id,
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "scope_key": "warehouse",
        "content_hash": revision.content_hash,
        "content_payload": draft_payload,
        "payload_hash": canonical_hash(draft_payload),
        "provenance": canonical_value(provenance),
        "provenance_hash": provenance.provenance_hash,
    }
    artifacts = [
        {
            "artifact_id": evaluation.artifact_id,
            "artifact_type": "evaluation",
            "approval_run_id": RUN_ID,
            "ticket_id": TICKET_ID,
            "scope_key": "warehouse",
            "corrected_draft_id": draft_id,
            "evaluated_content_hash": revision.content_hash,
            "contract_version": evaluation.contract_version,
            "payload": evaluation_payload,
            "payload_hash": evaluation.payload_hash,
        },
        {
            "artifact_id": preview.artifact_id,
            "artifact_type": "safe_preview",
            "approval_run_id": RUN_ID,
            "ticket_id": TICKET_ID,
            "scope_key": "warehouse",
            "corrected_draft_id": draft_id,
            "evaluated_content_hash": revision.content_hash,
            "contract_version": preview.contract_version,
            "payload": preview_payload,
            "payload_hash": preview.payload_hash,
        },
    ]
    return revision, bundle, draft, artifacts


def _initial_import_command():
    evidence = [
        EvidenceReference(
            document_id="part2-source",
            section_id="global-currency",
            evidence_hash=H64,
        )
    ]
    currency = GlobalCurrency(value="USD", status="documented", evidence=evidence)
    sla = GlobalSLA(
        applicability="not_applicable",
        percentage=None,
        measurement_period=None,
        scope=[],
        exclusions=[],
        evidence=[],
    )
    source_revision_id = UUID("20000000-0000-0000-0000-000000000021")
    content_hash = proposal_revision_content_hash(
        currency=currency,
        sla=sla,
        volume_discounts=[],
        source_department_revision_ids=[source_revision_id],
        authorized_input_ids=[],
    )
    evaluation = ImmutableArtifactReference(
        artifact_type="global_evaluation",
        artifact_id=UUID("20000000-0000-0000-0000-000000000022"),
        contract_version="global-evaluation-v1",
        payload_hash=H64,
        evaluated_content_hash=content_hash,
    )
    preview = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=UUID("20000000-0000-0000-0000-000000000023"),
        contract_version="rfp-safe-preview-v1",
        payload_hash=H64_B,
        evaluated_content_hash=content_hash,
    )
    validation_hash = canonical_hash(
        {
            "revision_content_hash": content_hash,
            "global_evaluation": evaluation,
            "safe_preview": preview,
            "mandatory_compliance_passed": True,
            "unresolved_input_codes": [],
            "blocking_rule_ids": [],
            "open_conflict_ids": [],
            "eligible_for_synthesis": True,
            "validated_at": NOW,
            "source_departments_approved": True,
        }
    )
    proposal = ProposalRevision(
        revision_id=UUID("20000000-0000-0000-0000-000000000024"),
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        revision_no=1,
        source_global_draft_id=UUID(
            "20000000-0000-0000-0000-000000000025"
        ),
        source_global_draft_hash=H64,
        source_global_evaluation_id=UUID(
            "20000000-0000-0000-0000-000000000026"
        ),
        source_global_evaluation_hash=H64_B,
        currency=currency,
        sla=sla,
        volume_discounts=[],
        source_department_revision_ids=[source_revision_id],
        authorized_input_ids=[],
        content_hash=content_hash,
        validation=ProposalRevisionValidation(
            contract_version="rfp-proposal-revision-validation-v2",
            revision_content_hash=content_hash,
            global_evaluation=evaluation,
            safe_preview=preview,
            mandatory_compliance_passed=True,
            unresolved_input_codes=[],
            blocking_rule_ids=[],
            open_conflict_ids=[],
            source_departments_approved=True,
            eligible_for_synthesis=True,
            validation_hash=validation_hash,
            validated_at=NOW,
        ),
        validation_bundle_id=UUID(
            "20000000-0000-0000-0000-000000000027"
        ),
        created_at=NOW,
    )
    handoff = RFPResponseHandoff(
        ticket_id=TICKET_ID,
        run_id=RUN_ID,
        source_handoff_hash=H64,
        outcome="ready_for_part3",
        all_sections_passed=True,
        global_compliance_passed=True,
        selected_departments=[DepartmentId.warehouse],
        department_results=[
            DepartmentHandoffResult(
                department=DepartmentId.warehouse,
                terminal_status="passed",
                iteration_count=1,
                final_draft_id=UUID(
                    "20000000-0000-0000-0000-000000000028"
                ),
                evaluation_result_id=UUID(
                    "20000000-0000-0000-0000-000000000029"
                ),
                safe_preview_id=UUID(
                    "20000000-0000-0000-0000-000000000030"
                ),
                blocks_commercial_completeness=False,
                requires_human_input=False,
                blocking_rule_ids=[],
            )
        ],
        global_draft_id=proposal.source_global_draft_id,
        global_evaluation_result_id=proposal.source_global_evaluation_id,
        unresolved_items=[],
        created_at=NOW,
    )
    handoff_payload = canonical_value(handoff)
    return InitialProposalRevisionCommand(
        creation_mode="part2_import",
        response_run_id=RUN_ID,
        response_handoff_hash=canonical_hash(handoff_payload),
        proposal=proposal,
        validation_bundle_id=proposal.validation_bundle_id,
        imported_by=RunInitiator(
            principal=_principal(True),
            authorization_id=AUTHORIZATION_ID,
            permission="manage_approval_run",
        ),
        idempotency_key=KEY,
        requested_at=NOW,
    ), handoff_payload


def _persisted_proposal_bundle_fixture(*, authorized_inputs=True):
    command, _handoff = _initial_import_command()
    original = command.proposal
    input_ids = [INPUT_ID] if authorized_inputs else []
    input_hash = H64_B
    content_hash = proposal_revision_content_hash(
        currency=original.currency,
        sla=original.sla,
        volume_discounts=original.volume_discounts,
        source_department_revision_ids=original.source_department_revision_ids,
        authorized_input_ids=input_ids,
    )
    evaluation_payload = {
        "payload_kind": "content_evaluation",
        "mandatory_compliance_passed": True,
        "blocks_commercial_completeness": False,
        "blocking_rule_ids": [],
        "unresolved_input_codes": [],
        "open_conflict_ids": [],
        "eligible": True,
    }
    preview_payload = {
        "payload_kind": "safe_preview",
        "gate_status": "passed",
        "rendered_content": '{"currency":"USD"}',
        "rendered_content_hash": canonical_hash(
            {"rendered_content": '{"currency":"USD"}'}
        ),
        "reason_codes": [],
    }
    evaluation = original.validation.global_evaluation.model_copy(
        update={
            "payload_hash": canonical_hash(evaluation_payload),
            "evaluated_content_hash": content_hash,
        }
    )
    preview = original.validation.safe_preview.model_copy(
        update={
            "payload_hash": canonical_hash(preview_payload),
            "evaluated_content_hash": content_hash,
        }
    )
    validation_values = {
        "revision_content_hash": content_hash,
        "global_evaluation": evaluation,
        "safe_preview": preview,
        "mandatory_compliance_passed": True,
        "unresolved_input_codes": [],
        "blocking_rule_ids": [],
        "open_conflict_ids": [],
        "source_departments_approved": True,
        "eligible_for_synthesis": True,
        "validated_at": NOW,
    }
    validation = ProposalRevisionValidation(
        contract_version="rfp-proposal-revision-validation-v2",
        **validation_values,
        validation_hash=canonical_hash(validation_values),
    )
    proposal = ProposalRevision.model_validate(
        {
            **original.model_dump(),
            "authorized_input_ids": input_ids,
            "content_hash": content_hash,
            "validation": validation,
        }
    )
    draft_id = UUID("20000000-0000-0000-0000-000000000031")
    draft_payload = canonical_value(
        {
            "payload_kind": "proposal_corrected_draft",
            "currency": proposal.currency,
            "sla": proposal.sla,
            "volume_discounts": proposal.volume_discounts,
            "source_department_revision_ids": proposal.source_department_revision_ids,
            "authorized_input_ids": proposal.authorized_input_ids,
        }
    )
    source_pairs = [
        (proposal.source_global_draft_id, proposal.source_global_draft_hash),
        *([(INPUT_ID, input_hash)] if authorized_inputs else []),
    ]
    provenance_data = {
        "producer_node": "coordinate_global_proposal",
        "execution_id": EXECUTION_ID,
        "source_kind": "human_correction" if authorized_inputs else "part2_import",
        "source_artifact_ids": [item[0] for item in source_pairs],
        "source_hashes": [item[1] for item in source_pairs],
        "produced_at": NOW,
    }
    provenance = ApprovalArtifactProvenance(
        **provenance_data,
        provenance_hash=canonical_hash(provenance_data),
    )
    draft = {
        "draft_artifact_id": draft_id,
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "scope_key": "proposal",
        "content_hash": proposal.content_hash,
        "content_payload": draft_payload,
        "payload_hash": canonical_hash(draft_payload),
        "provenance": canonical_value(provenance),
        "provenance_hash": provenance.provenance_hash,
    }
    bundle = {
        "validation_bundle_id": proposal.validation_bundle_id,
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "scope_key": "proposal",
        "corrected_draft_id": draft_id,
        "content_hash": proposal.content_hash,
        "evaluation_artifact_id": evaluation.artifact_id,
        "evaluation_contract_version": evaluation.contract_version,
        "evaluation_payload_hash": evaluation.payload_hash,
        "safe_preview_artifact_id": preview.artifact_id,
        "safe_preview_contract_version": preview.contract_version,
        "safe_preview_payload_hash": preview.payload_hash,
        "revision_validation_hash": validation.validation_hash,
        "mandatory_compliance_passed": True,
        "blocks_commercial_completeness": False,
        "blocking_rule_ids": [],
        "unresolved_input_codes": [],
        "open_conflict_ids": [],
        "source_departments_approved": True,
        "eligible": True,
        "validated_at": NOW,
    }
    artifacts = [
        {
            "artifact_id": evaluation.artifact_id,
            "artifact_type": "global_evaluation",
            "approval_run_id": RUN_ID,
            "ticket_id": TICKET_ID,
            "scope_key": "proposal",
            "corrected_draft_id": draft_id,
            "evaluated_content_hash": proposal.content_hash,
            "contract_version": evaluation.contract_version,
            "payload": evaluation_payload,
            "payload_hash": evaluation.payload_hash,
        },
        {
            "artifact_id": preview.artifact_id,
            "artifact_type": "safe_preview",
            "approval_run_id": RUN_ID,
            "ticket_id": TICKET_ID,
            "scope_key": "proposal",
            "corrected_draft_id": draft_id,
            "evaluated_content_hash": proposal.content_hash,
            "contract_version": preview.contract_version,
            "payload": preview_payload,
            "payload_hash": preview.payload_hash,
        },
    ]
    input_rows = [{"input_id": INPUT_ID, "payload_hash": input_hash}]
    return proposal, bundle, draft, artifacts, input_rows


def _replace_draft_sources(draft, source_pairs):
    provenance_data = {
        "producer_node": "coordinate_global_proposal",
        "execution_id": EXECUTION_ID,
        "source_kind": "human_correction",
        "source_artifact_ids": [item[0] for item in source_pairs],
        "source_hashes": [item[1] for item in source_pairs],
        "produced_at": NOW,
    }
    return {
        **draft,
        "provenance": {
            "contract_version": "rfp-approval-artifact-provenance-v1",
            **canonical_value(provenance_data),
            "provenance_hash": canonical_hash(provenance_data),
        },
        "provenance_hash": canonical_hash(provenance_data),
    }


@pytest.fixture(params=[
    (ClientPriceQuoteInput(
        department=DepartmentId.warehouse, service_code="storage",
        amount=Decimal("10"), currency="USD", unit="m3/month",
        effective_from=date(2026, 11, 21)), "approved_quote", False),
    (WarehouseCapacityInput(
        site_code="WH-MX-01", capacity_value=Decimal("150"),
        capacity_unit="m3", period="month", available_from=date(2026, 11, 21)),
     "responsible_attestation", False),
    (OperationalScopeInput(
        department=DepartmentId.warehouse, scope_code="ambient-storage",
        description="Ambient storage at WH-MX-01", included=True,
        effective_from=date(2026, 11, 21)), "responsible_attestation", False),
    (CurrencyQuoteInput(
        term_code="global-base", amount=Decimal("20"), currency="USD",
        effective_from=date(2026, 11, 21)), "approved_quote", True),
    (CurrencyConversionInput(
        source_currency="EUR", target_currency="USD",
        conversion_rate=Decimal("1.08"), effective_at=NOW,
        source_term_codes=["global-base"]), "authorized_conversion", True),
    (GlobalCommercialTermInput(
        term_code="fuel-policy", value="Documented published policy",
        effective_from=date(2026, 11, 21)), "approved_policy", True),
    (VolumeDiscountInput(
        volume_unit="shipment", period="month",
        tiers=[VolumeDiscountTierInput(
            minimum_volume=Decimal("0"), maximum_volume=None,
            discount_percentage=Decimal("2"))],
        effective_from=date(2026, 11, 21)), "approved_policy", True),
])
def business_input(request):
    payload, evidence_type, global_actor = request.param
    provenance = _provenance(evidence_type)
    actor = _actor(global_actor)
    return AuthorizedBusinessInput(
        input_id=INPUT_ID,
        ticket_id=TICKET_ID,
        approval_run_id=RUN_ID,
        input_kind=payload.input_kind,
        payload=payload,
        provenance=provenance,
        actor=actor,
        payload_hash=authorized_business_input_hash(
            input_kind=payload.input_kind, payload=payload,
            provenance=provenance, actor=actor,
        ),
        recorded_at=NOW,
    ), global_actor


def test_all_seven_input_kinds_are_inserted_with_exact_envelope(
    monkeypatch, business_input
):
    value, global_actor = business_input
    identity = _identity(global_actor)
    row = {
        "input_id": value.input_id,
        "approval_run_id": value.approval_run_id,
        "payload_hash": value.payload_hash,
    }
    session = _Session(_Result(None), _Result(row))
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._lock_active_run",
        lambda *args, **kwargs: {"status": "active"},
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._authorize",
        lambda *args, **kwargs: SimpleNamespace(
            authorization_id=AUTHORIZATION_ID, authorization_version=1
        ),
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._insert_event",
        lambda *args, **kwargs: None,
    )
    result = record_authorized_business_input(
        session,
        business_input=value,
        identity=identity,
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
        idempotency_key=KEY,
        execution_id=EXECUTION_ID,
    )
    assert result.input_id == INPUT_ID
    assert result.replayed is False
    sql = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql
    assert "rfp_authorized_business_inputs" in sql


def test_department_and_global_targets_are_exact():
    department = _target("warehouse")
    assert department.department_scope == "warehouse"
    assert department.affected_departments == frozenset({"warehouse"})
    global_target = _target(None)
    assert global_target.target_policy == "proposal_wide"
    assert global_target.department_scope is None


@pytest.mark.parametrize(
    ("identity_environment", "stored_environment"),
    [("qa", "production"), ("production", "qa")],
)
def test_active_run_lookup_hides_cross_environment_runs(
    identity_environment, stored_environment
):
    # The stored row belongs to the opposite environment, therefore the SQL
    # predicate returns no row and does not disclose its status or payload.
    session = _Session(_Result(None))
    with pytest.raises(ApprovalStoreError) as exc:
        _lock_active_run(session, RUN_ID, TICKET_ID, identity_environment)
    assert exc.value.code == "approval_run_not_found_or_environment_mismatch"
    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "rfp_approval_runs.environment" in sql
    assert stored_environment != identity_environment
    assert len(session.statements) == 1


def test_active_run_lookup_accepts_same_environment():
    session = _Session(_Result({
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "status": "active",
        "environment": "production",
    }))
    row = _lock_active_run(session, RUN_ID, TICKET_ID, "production")
    assert row["environment"] == "production"
    assert row["status"] == "active"


def test_all_public_operations_forward_authenticated_environment(
    monkeypatch, business_input
):
    value, global_actor = business_input
    identity = _identity(global_actor)
    observed = []

    def reject_after_capture(session, run_id, ticket_id, environment):
        observed.append((run_id, ticket_id, environment))
        raise ApprovalStoreError("environment_guard_reached")

    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._lock_active_run",
        reject_after_capture,
    )
    with pytest.raises(ApprovalStoreError, match="persistence operation"):
        record_authorized_business_input(
            _Session(), business_input=value, identity=identity,
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
            idempotency_key=KEY, execution_id=EXECUTION_ID,
        )
    revision = _corrected_revision()
    with pytest.raises(ApprovalStoreError, match="persistence operation"):
        create_corrected_department_revision(
            _Session(), revision=revision,
            expected_predecessor_status="approved",
            expected_predecessor_content_hash=H64,
            expected_predecessor_state_version=1,
            identity=_identity(False),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
            idempotency_key=KEY, execution_id=EXECUTION_ID,
        )
    proposal = SimpleNamespace(
        revision_no=2,
        supersedes_revision_id=INPUT_ID,
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
    )
    with pytest.raises(ApprovalStoreError, match="persistence operation"):
        create_corrected_proposal_revision(
            _Session(), revision=proposal,
            expected_predecessor_content_hash=H64,
            expected_predecessor_state_version=1,
            identity=_identity(True),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
            idempotency_key=KEY, execution_id=EXECUTION_ID,
        )
    assert [item[2] for item in observed] == [
        "production", "production", "production"
    ]


@pytest.mark.parametrize(
    ("field", "replacement", "error_code"),
    [
        ("actor_principal_id", UUID("20000000-0000-0000-0000-000000000090"),
         "revision_replay_actor_mismatch"),
        ("actor_binding_id", UUID("20000000-0000-0000-0000-000000000091"),
         "revision_replay_actor_mismatch"),
        ("environment", "qa", "revision_replay_actor_mismatch"),
        ("authorization_id", UUID("20000000-0000-0000-0000-000000000092"),
         "revision_replay_authorization_mismatch"),
        ("authorization_version", 2, "revision_replay_authorization_mismatch"),
    ],
)
def test_revision_replay_requires_persisted_actor_and_grant(
    field, replacement, error_code
):
    event = _replay_event(**{field: replacement})
    with pytest.raises(ApprovalStoreError) as exc:
        _assert_revision_replay_actor(
            event,
            identity=_identity(False),
            reference=AuthorizationReference(AUTHORIZATION_ID, 1),
        )
    assert exc.value.code == error_code


def test_revision_replay_accepts_same_actor_binding_and_grant():
    _assert_revision_replay_actor(
        _replay_event(),
        identity=_identity(False),
        reference=AuthorizationReference(AUTHORIZATION_ID, 1),
    )


def test_preview_hash_excludes_no_row_fields_and_changes_with_content():
    row = {
        "preview_id": INPUT_ID,
        "draft_id": RUN_ID,
        "gate_status": "passed",
        "safe_content": "revision A",
        "reason_codes": [],
    }
    original = preview_artifact_payload_hash(row)
    assert original == canonical_hash(row)
    row["safe_content"] = "revision B"
    assert preview_artifact_payload_hash(row) != original


@pytest.mark.parametrize(
    ("validation", "pending", "number", "expected"),
    [
        (SimpleNamespace(mandatory_compliance_passed=True, blocking_rule_ids=[],
                         blocks_commercial_completeness=False,
                         unresolved_input_codes=[], open_conflict_ids=[],
                         eligible_for_approval=True), [], 2, "pending_approval"),
        (SimpleNamespace(mandatory_compliance_passed=False, blocking_rule_ids=["R1"],
                         blocks_commercial_completeness=False,
                         unresolved_input_codes=[], open_conflict_ids=[],
                         eligible_for_approval=False), [], 2, "blocked_compliance"),
        (SimpleNamespace(mandatory_compliance_passed=True, blocking_rule_ids=[],
                         blocks_commercial_completeness=True,
                         unresolved_input_codes=["RATE"], open_conflict_ids=[],
                         eligible_for_approval=False), [object()], 2, "blocked_input"),
        (SimpleNamespace(mandatory_compliance_passed=True, blocking_rule_ids=[],
                         blocks_commercial_completeness=False,
                         unresolved_input_codes=[], open_conflict_ids=[INPUT_ID],
                         eligible_for_approval=False), [], 3, "blocked_conflict"),
    ],
)
def test_department_status_is_derived(validation, pending, number, expected):
    revision = SimpleNamespace(
        validation=validation,
        pending_commercial_items=pending,
        review_revision_no=number,
    )
    assert _derived_department_status(revision) == expected


def test_revision_two_conflict_is_explicitly_blocked():
    revision = SimpleNamespace(
        validation=SimpleNamespace(
            mandatory_compliance_passed=True,
            blocking_rule_ids=[],
            blocks_commercial_completeness=False,
            unresolved_input_codes=[],
            open_conflict_ids=[INPUT_ID],
            eligible_for_approval=False,
        ),
        pending_commercial_items=[],
        review_revision_no=2,
    )
    assert _derived_department_status(revision) == "blocked_conflict"


def test_compliance_precedes_open_conflict_without_discarding_conflict():
    revision = SimpleNamespace(
        validation=SimpleNamespace(
            mandatory_compliance_passed=False,
            blocking_rule_ids=["TF-COMPLIANCE"],
            blocks_commercial_completeness=False,
            unresolved_input_codes=[],
            open_conflict_ids=[INPUT_ID],
            eligible_for_approval=False,
        ),
        pending_commercial_items=[],
        review_revision_no=3,
    )
    assert _derived_department_status(revision) == "blocked_compliance"
    assert revision.validation.open_conflict_ids == [INPUT_ID]


def test_pending_input_precedes_compliance_and_conflict():
    revision = SimpleNamespace(
        validation=SimpleNamespace(
            mandatory_compliance_passed=False,
            blocking_rule_ids=["TF-COMPLIANCE"],
            blocks_commercial_completeness=True,
            unresolved_input_codes=["RATE"],
            open_conflict_ids=[INPUT_ID],
            eligible_for_approval=False,
        ),
        pending_commercial_items=[object()],
        review_revision_no=3,
    )
    assert _derived_department_status(revision) == "blocked_input"


def test_unrepresented_input_blocker_without_pending_item_fails_closed():
    revision = SimpleNamespace(
        validation=SimpleNamespace(
            mandatory_compliance_passed=True,
            blocking_rule_ids=[],
            blocks_commercial_completeness=True,
            unresolved_input_codes=["RATE"],
            open_conflict_ids=[],
            eligible_for_approval=False,
        ),
        pending_commercial_items=[],
        review_revision_no=3,
    )
    with pytest.raises(ApprovalStoreError) as raised:
        _derived_department_status(revision)
    assert raised.value.code == "input_blocker_without_pending_item"


def test_global_blockers_can_disable_synthesis_with_approved_sources():
    revision = SimpleNamespace(
        validation=SimpleNamespace(eligible_for_synthesis=False)
    )
    assert _derived_proposal_eligibility(revision) is False


def test_persisted_bundle_binds_exact_draft_artifacts_and_summary():
    revision, bundle, draft, artifacts = _persisted_bundle_fixture()
    session = _Session(_Result(bundle), _Result(draft), _Result(artifacts))
    resolved = _lock_validation_bundle(
        session, revision=revision, scope="warehouse"
    )
    assert resolved["validation_bundle_id"] == revision.validation_bundle_id
    assert len(session.statements) == 3
    assert all("FOR" in str(statement) for statement in session.statements)


def test_persisted_bundle_validates_real_s2a_provenance_envelope():
    revision, bundle, draft, artifacts = _persisted_bundle_fixture()
    envelope = ApprovalArtifactProvenance.model_validate_json(
        json.dumps(draft["provenance"])
    )
    assert envelope.provenance_hash == draft["provenance_hash"]
    session = _Session(_Result(bundle), _Result(draft), _Result(artifacts))
    _lock_validation_bundle(session, revision=revision, scope="warehouse")


def test_persisted_bundle_rejects_tampered_provenance_envelope():
    revision, bundle, draft, _artifacts = _persisted_bundle_fixture()
    draft = dict(draft)
    draft["provenance"] = {
        **draft["provenance"],
        "source_hashes": [H64_B],
    }
    session = _Session(_Result(bundle), _Result(draft))
    with pytest.raises(ApprovalStoreError) as raised:
        _lock_validation_bundle(session, revision=revision, scope="warehouse")
    assert raised.value.code == "corrected_draft_provenance_invalid"


def test_persisted_bundle_rejects_valid_provenance_for_other_sources():
    revision, bundle, draft, _artifacts = _persisted_bundle_fixture()
    original = ApprovalArtifactProvenance.model_validate_json(
        json.dumps(draft["provenance"])
    )
    provenance_data = original.model_dump(
        exclude={"contract_version", "provenance_hash"}
    )
    provenance_data["source_artifact_ids"] = [uuid4()]
    provenance_data["source_hashes"] = [H64_B]
    replacement = ApprovalArtifactProvenance(
        **provenance_data,
        provenance_hash=canonical_hash(provenance_data),
    )
    draft = {
        **draft,
        "provenance": canonical_value(replacement),
        "provenance_hash": replacement.provenance_hash,
    }
    session = _Session(_Result(bundle), _Result(draft))
    with pytest.raises(ApprovalStoreError) as raised:
        _lock_validation_bundle(session, revision=revision, scope="warehouse")
    assert raised.value.code == "corrected_draft_provenance_sources_mismatch"


def test_global_human_correction_requires_original_draft_and_authorized_inputs():
    proposal, bundle, draft, artifacts, input_rows = (
        _persisted_proposal_bundle_fixture()
    )
    session = _Session(
        _Result(bundle),
        _Result(draft),
        _Result(input_rows),
        _Result(artifacts),
    )

    resolved = _lock_validation_bundle(
        session,
        revision=proposal,
        scope="proposal",
        required_draft_source_kind="human_correction",
    )

    assert resolved["validation_bundle_id"] == proposal.validation_bundle_id
    assert len(session.statements) == 4


@pytest.mark.parametrize(
    "source_pairs",
    [
        [(INPUT_ID, H64_B)],
        [(UUID("20000000-0000-0000-0000-000000000025"), H64)],
        [
            (UUID("20000000-0000-0000-0000-000000000025"), H64),
            (INPUT_ID, H64_B),
            (UUID("20000000-0000-0000-0000-000000000099"), "c" * 64),
        ],
        [
            (UUID("20000000-0000-0000-0000-000000000025"), H64),
            (INPUT_ID, "c" * 64),
        ],
    ],
    ids=["missing-original-draft", "missing-input", "extra-source", "altered-hash"],
)
def test_global_human_correction_rejects_inexact_source_sets(source_pairs):
    proposal, bundle, draft, _artifacts, input_rows = (
        _persisted_proposal_bundle_fixture()
    )
    changed = _replace_draft_sources(draft, source_pairs)
    session = _Session(_Result(bundle), _Result(changed), _Result(input_rows))

    with pytest.raises(ApprovalStoreError) as raised:
        _lock_validation_bundle(
            session,
            revision=proposal,
            scope="proposal",
            required_draft_source_kind="human_correction",
        )

    assert raised.value.code == "corrected_draft_provenance_sources_mismatch"


def test_global_human_correction_rejects_duplicate_sources():
    proposal, bundle, draft, _artifacts, _input_rows = (
        _persisted_proposal_bundle_fixture()
    )
    duplicate = _replace_draft_sources(
        draft,
        [
            (proposal.source_global_draft_id, proposal.source_global_draft_hash),
            (INPUT_ID, H64_B),
            (INPUT_ID, H64_B),
        ],
    )
    session = _Session(_Result(bundle), _Result(duplicate))

    with pytest.raises(ApprovalStoreError) as raised:
        _lock_validation_bundle(
            session,
            revision=proposal,
            scope="proposal",
            required_draft_source_kind="human_correction",
        )

    assert raised.value.code == "corrected_draft_provenance_invalid"


def test_global_part2_import_still_requires_only_original_draft():
    proposal, bundle, draft, artifacts, _input_rows = (
        _persisted_proposal_bundle_fixture(authorized_inputs=False)
    )
    session = _Session(_Result(bundle), _Result(draft), _Result(artifacts))

    resolved = _lock_validation_bundle(
        session,
        revision=proposal,
        scope="proposal",
        required_draft_source_kind="part2_import",
    )

    assert resolved["validation_bundle_id"] == proposal.validation_bundle_id
    assert len(session.statements) == 3


def test_persisted_bundle_rejects_wrong_run_scope_or_content():
    revision, _bundle, _draft, _artifacts = _persisted_bundle_fixture()
    session = _Session(_Result(None))
    with pytest.raises(ApprovalStoreError) as exc:
        _lock_validation_bundle(session, revision=revision, scope="warehouse")
    assert exc.value.code == "validation_bundle_not_found_or_mismatch"
    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "approval_run_id" in sql
    assert "ticket_id" in sql
    assert "scope_key" in sql
    assert "content_hash" in sql


def test_persisted_bundle_rejects_draft_payload_from_other_content():
    revision, bundle, draft, _artifacts = _persisted_bundle_fixture()
    draft = dict(draft)
    draft["content_payload"] = {
        **draft["content_payload"],
        "content": "stale corrected content",
    }
    draft["payload_hash"] = canonical_hash(draft["content_payload"])
    session = _Session(_Result(bundle), _Result(draft))
    with pytest.raises(ApprovalStoreError) as exc:
        _lock_validation_bundle(session, revision=revision, scope="warehouse")
    assert exc.value.code == "corrected_draft_revision_mismatch"


def test_eligible_bundle_rejects_failed_safe_preview():
    revision, bundle, draft, artifacts = _persisted_bundle_fixture(
        preview_status="blocked"
    )
    session = _Session(_Result(bundle), _Result(draft), _Result(artifacts))
    with pytest.raises(ApprovalStoreError) as exc:
        _lock_validation_bundle(session, revision=revision, scope="warehouse")
    assert exc.value.code == "eligible_bundle_preview_not_passed"


def test_part2_global_source_requires_exact_persisted_hashes():
    revision = SimpleNamespace(
        source_global_draft_id=INPUT_ID,
        source_global_draft_hash=canonical_hash({"draft": "global"}),
        source_global_evaluation_id=KEY,
        source_global_evaluation_hash=canonical_hash({"evaluation": "passed"}),
    )
    session = _Session(
        _Result(
            {
                "payload": {"draft": "global"},
                "payload_hash": revision.source_global_draft_hash,
            }
        ),
        _Result(
            {
                "payload": {"evaluation": "passed"},
                "payload_hash": revision.source_global_evaluation_hash,
            }
        ),
    )
    _verify_part2_global_source(
        session,
        revision=revision,
        response_run_id=RUN_ID,
        require_same_terms=False,
    )
    assert len(session.statements) == 2


def test_part2_global_source_rejects_claimed_hash_not_in_database():
    revision = SimpleNamespace(
        source_global_draft_id=INPUT_ID,
        source_global_draft_hash=H64,
        source_global_evaluation_id=KEY,
        source_global_evaluation_hash=H64_B,
    )
    session = _Session(
        _Result({"payload": {"draft": "global"}, "payload_hash": H64_B})
    )
    with pytest.raises(ApprovalStoreError) as exc:
        _verify_part2_global_source(
            session,
            revision=revision,
            response_run_id=RUN_ID,
            require_same_terms=False,
        )
    assert exc.value.code == "source_global_draft_mismatch"


def test_handoff_accepts_exact_selected_global_sources():
    command, payload = _initial_import_command()
    resolved = _validated_handoff({"payload": payload}, command)
    assert resolved.global_draft_id == command.proposal.source_global_draft_id
    assert (
        resolved.global_evaluation_result_id
        == command.proposal.source_global_evaluation_id
    )


@pytest.mark.parametrize(
    "field",
    ["source_global_draft_id", "source_global_evaluation_id"],
)
def test_handoff_rejects_other_global_source_from_same_run(field):
    command, payload = _initial_import_command()
    proposal = command.proposal.model_copy(update={field: uuid4()})
    divergent = command.model_copy(update={"proposal": proposal})
    with pytest.raises(ApprovalStoreError) as raised:
        _validated_handoff({"payload": payload}, divergent)
    assert raised.value.code == "response_handoff_global_source_mismatch"


def test_initial_part2_import_uses_exclusive_run_lock_and_real_bundle(monkeypatch):
    command, handoff_payload = _initial_import_command()
    source_revision_id = command.proposal.source_department_revision_ids[0]
    session = _Session(
        _Result(
            {
                "approval_run_id": RUN_ID,
                "ticket_id": TICKET_ID,
                "response_run_id": RUN_ID,
                "response_handoff_hash": command.response_handoff_hash,
                "active_departments": ["warehouse"],
                "environment": "production",
                "status": "active",
            }
        ),
        _Result(None),
        _Result(None),
        _Result(
            {
                "run_id": RUN_ID,
                "ticket_id": TICKET_ID,
                "payload": handoff_payload,
                "payload_hash": command.response_handoff_hash,
            }
        ),
        _Result(
            [
                {
                    "revision_id": source_revision_id,
                    "approval_run_id": RUN_ID,
                    "ticket_id": TICKET_ID,
                    "department_id": "warehouse",
                    "is_current": True,
                    "status": "approved",
                }
            ]
        ),
        _Result([]),
        _Result(),
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._verify_part2_global_source",
        lambda *args, **kwargs: None,
    )
    authorized = []
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._authorize_run_management",
        lambda *args, **kwargs: authorized.append(kwargs)
        or SimpleNamespace(
            authorization_id=AUTHORIZATION_ID,
            authorization_version=1,
        ),
    )
    bundle_calls = []
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._lock_validation_bundle",
        lambda *args, **kwargs: bundle_calls.append(kwargs) or {"eligible": True},
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._insert_event",
        lambda *args, **kwargs: None,
    )
    result = create_initial_proposal_revision(
        session,
        command=command,
        identity=_identity(True),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
        execution_id=EXECUTION_ID,
    )
    assert result.revision_no == 1
    assert result.eligible_for_synthesis is True
    assert len(authorized) == 1
    assert bundle_calls[0]["required_draft_source_kind"] == "part2_import"
    run_lock_sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in run_lock_sql
    insert_sql = str(session.statements[-1].compile(dialect=postgresql.dialect()))
    assert "validation_bundle_id" in insert_sql
    assert "supersedes_revision_id" not in insert_sql


def test_input_replay_requires_current_authorization(monkeypatch, business_input):
    value, global_actor = business_input
    identity = _identity(global_actor)
    session = _Session(_Result({"sentinel": "persisted"}))
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._lock_active_run",
        lambda *args, **kwargs: {"status": "active"},
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._authorize",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ApprovalStoreError("authorization_revoked")
        ),
    )
    with pytest.raises(ApprovalStoreError) as exc:
        record_authorized_business_input(
            session,
            business_input=value,
            identity=identity,
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
            idempotency_key=KEY,
            execution_id=EXECUTION_ID,
        )
    assert exc.value.code == "authorization_revoked"


def test_contract_rejects_conversion_without_conversion_evidence():
    payload = CurrencyConversionInput(
        source_currency="EUR", target_currency="USD",
        conversion_rate=Decimal("1.08"), effective_at=NOW,
        source_term_codes=["global-base"],
    )
    provenance = _provenance("approved_quote")
    actor = _actor(True)
    with pytest.raises(ValueError, match="conversion evidence"):
        AuthorizedBusinessInput(
            input_id=INPUT_ID, ticket_id=TICKET_ID, approval_run_id=RUN_ID,
            input_kind="currency_conversion", payload=payload,
            provenance=provenance, actor=actor,
            payload_hash=authorized_business_input_hash(
                input_kind="currency_conversion", payload=payload,
                provenance=provenance, actor=actor,
            ), recorded_at=NOW,
        )


def test_audit_projection_contains_hashes_not_commercial_payload(monkeypatch, business_input):
    value, global_actor = business_input
    captured = {}
    session = _Session(_Result(None), _Result({
        "input_id": value.input_id,
        "approval_run_id": value.approval_run_id,
        "payload_hash": value.payload_hash,
    }))
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._lock_active_run",
        lambda *args, **kwargs: {"status": "active"},
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._authorize",
        lambda *args, **kwargs: SimpleNamespace(
            authorization_id=AUTHORIZATION_ID, authorization_version=1
        ),
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._insert_event",
        lambda session, values: captured.update(values),
    )
    record_authorized_business_input(
        session, business_input=value, identity=_identity(global_actor),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
        idempotency_key=KEY, execution_id=EXECUTION_ID,
    )
    serialized = repr(captured["safe_input_ref"])
    assert value.payload_hash in serialized
    assert "amount" not in serialized
    assert "conversion_rate" not in serialized


def test_department_supersession_uses_cas_and_preserves_part2_iteration(monkeypatch):
    revision = _corrected_revision()
    predecessor = {
        "revision_id": revision.supersedes_revision_id,
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "department_id": "warehouse",
        "review_revision_no": 1,
        "source_part2_draft_id": revision.source_part2_draft_id,
        "source_part2_draft_hash": revision.source_part2_draft_hash,
        "source_part2_evaluation_id": revision.source_part2_evaluation_id,
        "source_part2_evaluation_hash": revision.source_part2_evaluation_hash,
        "source_part2_iteration": 3,
        "is_current": True,
        "status": "approved",
        "content_hash": H64,
        "state_version": 5,
    }
    session = _Session(
        _Result(None),
        _Result(predecessor),
        _Result([{"input_id": INPUT_ID, "payload_hash": H64}]),
        _Result(rowcount=1),
        _Result(),
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._lock_active_run",
        lambda *args, **kwargs: {
            "status": "active",
            "response_run_id": UUID("20000000-0000-0000-0000-000000000017"),
        },
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._authorize",
        lambda *args, **kwargs: SimpleNamespace(
            authorization_id=AUTHORIZATION_ID, authorization_version=1
        ),
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._lock_validation_bundle",
        lambda *args, **kwargs: {"eligible": True},
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._insert_event",
        lambda *args, **kwargs: None,
    )
    result = create_corrected_department_revision(
        session,
        revision=revision,
        expected_predecessor_status="approved",
        expected_predecessor_content_hash=H64,
        expected_predecessor_state_version=5,
        identity=_identity(False),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
        idempotency_key=KEY,
        execution_id=EXECUTION_ID,
    )
    assert result.review_revision_no == 2
    assert result.status == "pending_approval"
    update_sql = str(session.statements[3].compile(dialect=postgresql.dialect()))
    assert "state_version" in update_sql
    assert "content_hash" in update_sql
    assert "is_current" in update_sql
    insert_sql = str(session.statements[4].compile(dialect=postgresql.dialect()))
    assert "source_part2_iteration" in insert_sql
    assert "validation_bundle_id" in insert_sql
    assert "rfp_approval_department_revisions" in insert_sql


def test_department_supersession_rejects_part2_heritage_change(monkeypatch):
    revision = _corrected_revision()
    predecessor = {
        "revision_id": revision.supersedes_revision_id,
        "review_revision_no": 1,
        "source_part2_draft_id": revision.source_part2_draft_id,
        "source_part2_draft_hash": H64_B,
        "source_part2_evaluation_id": revision.source_part2_evaluation_id,
        "source_part2_evaluation_hash": revision.source_part2_evaluation_hash,
        "source_part2_iteration": revision.source_part2_iteration,
        "is_current": True,
        "status": "approved",
        "content_hash": H64,
        "state_version": 5,
    }
    session = _Session(_Result(None), _Result(predecessor))
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._lock_active_run",
        lambda *args, **kwargs: {"status": "active", "response_run_id": RUN_ID},
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._authorize",
        lambda *args, **kwargs: SimpleNamespace(
            authorization_id=AUTHORIZATION_ID, authorization_version=1
        ),
    )
    with pytest.raises(ApprovalStoreError) as exc:
        create_corrected_department_revision(
            session,
            revision=revision,
            expected_predecessor_status="approved",
            expected_predecessor_content_hash=H64,
            expected_predecessor_state_version=5,
            identity=_identity(False),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
            idempotency_key=KEY,
            execution_id=EXECUTION_ID,
        )
    assert exc.value.code == "part2_heritage_changed"


def _persisted_revision_row(revision):
    return {
        "revision_id": revision.revision_id,
        "approval_run_id": revision.approval_run_id,
        "department_id": revision.department.value,
        "review_revision_no": revision.review_revision_no,
        "status": revision.status,
        "state_version": 0,
    }


def test_initial_revision_replay_rechecks_historical_actor_and_current_grant(
    monkeypatch,
):
    revision = _corrected_revision()
    session = _Session(_Result(_persisted_revision_row(revision)))
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._lock_active_run",
        lambda *args, **kwargs: {"status": "active", "response_run_id": RUN_ID},
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._revision_event_replay",
        lambda *args, **kwargs: _replay_event(),
    )
    authorized = []
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._authorize",
        lambda *args, **kwargs: authorized.append(kwargs) or SimpleNamespace(),
    )
    result = create_corrected_department_revision(
        session,
        revision=revision,
        expected_predecessor_status="approved",
        expected_predecessor_content_hash=H64,
        expected_predecessor_state_version=5,
        identity=_identity(False),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
        idempotency_key=KEY,
        execution_id=EXECUTION_ID,
    )
    assert result.replayed is True
    assert len(authorized) == 1
    assert authorized[0]["department"] == "warehouse"
    assert len(session.statements) == 1
    assert str(session.statements[0]).lstrip().startswith("SELECT")


def test_revision_replay_revocation_returns_nothing_and_mutates_nothing(monkeypatch):
    revision = _corrected_revision()
    session = _Session(_Result(_persisted_revision_row(revision)))
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._lock_active_run",
        lambda *args, **kwargs: {"status": "active", "response_run_id": RUN_ID},
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._revision_event_replay",
        lambda *args, **kwargs: _replay_event(),
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._authorize",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ApprovalStoreError("authorization_revoked")
        ),
    )
    with pytest.raises(ApprovalStoreError) as exc:
        create_corrected_department_revision(
            session,
            revision=revision,
            expected_predecessor_status="approved",
            expected_predecessor_content_hash=H64,
            expected_predecessor_state_version=5,
            identity=_identity(False),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
            idempotency_key=KEY,
            execution_id=EXECUTION_ID,
        )
    assert exc.value.code == "authorization_revoked"
    assert len(session.statements) == 1
    assert str(session.statements[0]).lstrip().startswith("SELECT")


def test_concurrent_replay_recovery_checks_persisted_actor_before_result(
    monkeypatch,
):
    revision = _corrected_revision()
    predecessor = {
        "revision_id": revision.supersedes_revision_id,
        "review_revision_no": 1,
        "is_current": False,
        "status": "superseded",
        "content_hash": H64,
        "state_version": 6,
    }
    session = _Session(_Result(predecessor))
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._lock_active_run",
        lambda *args, **kwargs: {"status": "active", "response_run_id": RUN_ID},
    )
    events = iter([
        None,
        _replay_event(
            actor_binding_id=UUID("20000000-0000-0000-0000-000000000099")
        ),
    ])
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._revision_event_replay",
        lambda *args, **kwargs: next(events),
    )
    monkeypatch.setattr(
        "services.rfp_approval.commercial_store._authorize",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    with pytest.raises(ApprovalStoreError) as exc:
        create_corrected_department_revision(
            session,
            revision=revision,
            expected_predecessor_status="approved",
            expected_predecessor_content_hash=H64,
            expected_predecessor_state_version=5,
            identity=_identity(False),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 1),
            idempotency_key=KEY,
            execution_id=EXECUTION_ID,
        )
    assert exc.value.code == "revision_replay_actor_mismatch"
    assert len(session.statements) == 1
    assert str(session.statements[0]).lstrip().startswith("SELECT")
