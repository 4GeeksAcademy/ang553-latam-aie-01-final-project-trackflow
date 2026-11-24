from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.hashing import canonical_value
from pipelines.rfp_approval.contracts import (
    ArbitrationConflict,
    ArbitrationResolution,
    ArbitrationTarget,
    AuthenticatedPrincipal,
    AuthorizedHumanActor,
    ConflictAssessment,
    CorrectedRevisionEvidence,
    CurrencyMismatchFacts,
    HumanArbitrationExecution,
    ImmutableArtifactReference,
    QuantityFact,
    QuantityScope,
    VolumeCapacityFacts,
    arbitration_conflict_fingerprint,
    arbitration_resolution_hash,
    conflict_assessment_hash,
)
from services.rfp_approval.arbitration import (
    ArbitrationStoreError,
    _authorization_target,
    _deserialize_assessed_content_hashes,
    _serialize_assessed_content_hashes,
    persist_conflict_assessment,
    record_arbitration_resolution,
)
from services.rfp_approval.authorization import AuthorizationDenied
from services.rfp_approval.identity import ResolvedIdentity, VerifiedAuthenticationContext


NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
RUN_ID, TICKET_ID, CONFLICT_ID = UUID(int=1), UUID(int=2), UUID(int=3)
AUTH_ID, PRINCIPAL_ID, BINDING_ID = UUID(int=20), UUID(int=21), UUID(int=22)
H64 = "a" * 64


class _Result:
    def __init__(self, *, one=None, rowcount=None, scalar=None):
        self.one = one
        self.rowcount = rowcount
        self.scalar = scalar

    def mappings(self):
        return self

    def one_or_none(self):
        return self.one

    def scalar_one(self):
        return self.scalar


class _Session:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    def in_transaction(self):
        return True

    def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)


def _authentication():
    return VerifiedAuthenticationContext._from_verified_authenticator(
        environment="production",
        auth_source="local",
        issuer="trackflow-local",
        subject="subject-user",
        trackflow_user_id="user-user",
        general_role="user",
    )


def _identity():
    return ResolvedIdentity._from_persisted_binding(
        environment="production",
        auth_source="local",
        issuer="trackflow-local",
        subject="subject-user",
        trackflow_user_id="user-user",
        general_role="user",
        principal_id=PRINCIPAL_ID,
        binding_id=BINDING_ID,
        responsible_code="sofia_ramos",
        synthetic=False,
    )


def _resolution():
    principal = AuthenticatedPrincipal(
        issuer="trackflow-local",
        subject="subject-user",
        trackflow_user_id="user-user",
        auth_source="local",
        general_role="user",
        synthetic=False,
    )
    actor = AuthorizedHumanActor(
        principal=principal,
        authorization_id=AUTH_ID,
        official_responsible="Sofía Ramos",
        permission="arbitrate",
        department_scope=DepartmentId.reverse,
        trigger_scope="returns-sla-breach",
    )
    target = ArbitrationTarget(
        department=DepartmentId.reverse,
        department_revision_id=UUID(int=30),
    )
    reason = "Request a corrected return SLA of at least 48 hours."
    resolution_hash = arbitration_resolution_hash(
        conflict_id=CONFLICT_ID,
        conflict_fingerprint=H64,
        action="request_changes",
        targets=[target],
        authorized_input_ids=[],
        reason=reason,
    )
    return ArbitrationResolution(
        resolution_id=UUID(int=31),
        conflict_id=CONFLICT_ID,
        conflict_fingerprint=H64,
        trigger="returns-sla-breach",
        action="request_changes",
        targets=[target],
        authorized_input_ids=[],
        reason=reason,
        execution=HumanArbitrationExecution(
            execution_id=UUID(int=32),
            conflict_id=CONFLICT_ID,
            actor=actor,
            decided_at=NOW,
        ),
        resolution_hash=resolution_hash,
    )


def _conflict_pair():
    facts = CurrencyMismatchFacts(
        client_country="ES",
        expected_currency="EUR",
        proposal_revision_id=UUID(int=50),
        proposal_currency="USD",
        monetary_terms=[],
        mismatched_revision_ids=[UUID(int=50)],
        conflict_demonstrated=True,
    )
    assessment_hash = conflict_assessment_hash(
        trigger=facts.trigger,
        facts=facts,
        outcome="demonstrated",
        missing_input_codes=[],
    )
    assessment = ConflictAssessment(
        assessment_id=UUID(int=51),
        trigger=facts.trigger,
        facts=facts,
        outcome="demonstrated",
        missing_input_codes=[],
        assessment_hash=assessment_hash,
        assessed_at=NOW,
    )
    targets = [ArbitrationTarget(proposal_revision_id=UUID(int=50))]
    conflict = ArbitrationConflict(
        conflict_id=UUID(int=52),
        approval_run_id=RUN_ID,
        trigger=facts.trigger,
        facts=facts,
        affected_targets=targets,
        status="demonstrated",
        missing_input_codes=[],
        fingerprint=arbitration_conflict_fingerprint(
            trigger=facts.trigger, facts=facts, affected_targets=targets
        ),
        detected_at=NOW,
    )
    return assessment, conflict


def _accepted_currency_resolution() -> ArbitrationResolution:
    _, conflict = _conflict_pair()
    corrected_id = UUID(int=70)
    facts = CurrencyMismatchFacts(
        client_country="ES",
        expected_currency="EUR",
        proposal_revision_id=corrected_id,
        proposal_currency="EUR",
        monetary_terms=[],
        mismatched_revision_ids=[],
        conflict_demonstrated=False,
    )
    assessment_hash = conflict_assessment_hash(
        trigger=facts.trigger,
        facts=facts,
        outcome="no_conflict",
        missing_input_codes=[],
    )
    assessment = ConflictAssessment(
        assessment_id=UUID(int=71),
        trigger=facts.trigger,
        facts=facts,
        outcome="no_conflict",
        missing_input_codes=[],
        assessment_hash=assessment_hash,
        assessed_at=NOW,
    )
    validation = ImmutableArtifactReference(
        artifact_type="revision_validation",
        artifact_id=UUID(int=72),
        contract_version="rfp-approval-validation-bundle-v1",
        payload_hash="b" * 64,
        evaluated_content_hash=H64,
    )
    recheck = ImmutableArtifactReference(
        artifact_type="conflict_recheck",
        artifact_id=UUID(int=71),
        contract_version="rfp-conflict-assessment-v1",
        payload_hash=assessment_hash,
        evaluated_content_hash=H64,
    )
    evidence = CorrectedRevisionEvidence(
        original_target=conflict.affected_targets[0],
        corrected_revision_id=corrected_id,
        corrected_content_hash=H64,
        revision_validation=validation,
        conflict_recheck=recheck,
    )
    principal = AuthenticatedPrincipal(
        issuer="trackflow-local",
        subject="subject-user",
        trackflow_user_id="user-user",
        auth_source="local",
        general_role="user",
        synthetic=False,
    )
    actor = AuthorizedHumanActor(
        principal=principal,
        authorization_id=AUTH_ID,
        official_responsible="Miguel Torres",
        permission="arbitrate",
        trigger_scope="currency-mismatch",
    )
    reason = "The corrected proposal now uses the documented client-country currency."
    resolution_hash = arbitration_resolution_hash(
        conflict_id=conflict.conflict_id,
        conflict_fingerprint=conflict.fingerprint,
        action="accept_resolution",
        targets=conflict.affected_targets,
        authorized_input_ids=[],
        reason=reason,
        corrected_evidence=[evidence],
        post_correction_assessment=assessment,
    )
    return ArbitrationResolution(
        resolution_id=UUID(int=74),
        conflict_id=conflict.conflict_id,
        conflict_fingerprint=conflict.fingerprint,
        trigger="currency-mismatch",
        action="accept_resolution",
        targets=conflict.affected_targets,
        authorized_input_ids=[],
        reason=reason,
        execution=HumanArbitrationExecution(
            execution_id=UUID(int=75),
            conflict_id=conflict.conflict_id,
            actor=actor,
            decided_at=NOW,
        ),
        corrected_evidence=[evidence],
        post_correction_assessment=assessment,
        resolution_hash=resolution_hash,
    )


def test_conflict_is_persisted_open_with_two_append_only_events() -> None:
    assessment, conflict = _conflict_pair()
    session = _Session(
        [
            _Result(one={"status": "active"}),
            _Result(one={"proposal_revision_id": UUID(int=50)}),
            _Result(one=None),
            _Result(),
            _Result(one=None),
            _Result(),
            _Result(scalar=1),
            _Result(rowcount=1),
            _Result(rowcount=1),
        ]
    )

    record = persist_conflict_assessment(
        session,
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        assessment=assessment,
        assessed_content_hashes={"proposal": H64},
        conflict=conflict,
        execution_id=UUID(int=53),
    )

    assert record.conflict_status == "open"
    assert record.replayed is False
    assert session.statements[3].compile().params["assessed_content_hashes"] == [
        {"scope_key": "proposal", "content_hash": H64}
    ]
    assert session.results == []


def test_insufficient_information_persists_assessment_without_false_conflict() -> None:
    facts = VolumeCapacityFacts(
        required_volume=QuantityFact(
            value=Decimal("5000"),
            dimension="item_count",
            unit="order",
            period="month",
            operational_scope=QuantityScope(scope_code="approval-run"),
        ),
        comparison_status="missing_capacity",
        exceeds_capacity=None,
    )
    digest = conflict_assessment_hash(
        trigger=facts.trigger,
        facts=facts,
        outcome="insufficient_information",
        missing_input_codes=["WAREHOUSE_CAPACITY_REQUIRED"],
    )
    assessment = ConflictAssessment(
        assessment_id=UUID(int=54),
        trigger=facts.trigger,
        facts=facts,
        outcome="insufficient_information",
        missing_input_codes=["WAREHOUSE_CAPACITY_REQUIRED"],
        assessment_hash=digest,
        assessed_at=NOW,
    )
    session = _Session(
        [
            _Result(one={"status": "active"}),
            _Result(one=None),
            _Result(),
            _Result(scalar=1),
            _Result(rowcount=1),
            _Result(rowcount=1),
        ]
    )

    record = persist_conflict_assessment(
        session,
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        assessment=assessment,
        assessed_content_hashes={"lastmile": H64},
        conflict=None,
        execution_id=UUID(int=55),
    )

    assert record.outcome == "insufficient_information"
    assert record.conflict_id is None
    assert record.conflict_status is None


def test_assessed_content_hashes_have_a_stable_exact_array_representation() -> None:
    hashes = {"warehouse": "b" * 64, "proposal": H64}

    persisted = _serialize_assessed_content_hashes(hashes)

    assert persisted == [
        {"scope_key": "proposal", "content_hash": H64},
        {"scope_key": "warehouse", "content_hash": "b" * 64},
    ]
    assert _deserialize_assessed_content_hashes(persisted) == hashes


@pytest.mark.parametrize(
    "persisted",
    [
        [
            {"scope_key": "proposal", "content_hash": H64},
            {"scope_key": "proposal", "content_hash": "b" * 64},
        ],
        [{"scope_key": "proposal", "content_hash": H64, "extra": "forbidden"}],
        [
            {"scope_key": "warehouse", "content_hash": "b" * 64},
            {"scope_key": "proposal", "content_hash": H64},
        ],
    ],
)
def test_noncanonical_assessed_content_hash_arrays_are_rejected(persisted) -> None:
    with pytest.raises(ArbitrationStoreError) as raised:
        _deserialize_assessed_content_hashes(persisted)

    assert raised.value.code == "persisted_assessed_content_hashes_invalid"


def test_duplicate_conflict_replay_performs_no_new_write_or_event() -> None:
    assessment, conflict = _conflict_pair()
    session = _Session(
        [
            _Result(one={"status": "active"}),
            _Result(one={"proposal_revision_id": UUID(int=50)}),
            _Result(
                one={
                    "assessment_id": assessment.assessment_id,
                    "assessed_content_hashes": [
                        {"scope_key": "proposal", "content_hash": H64}
                    ],
                }
            ),
            _Result(
                one={
                    "conflict_id": conflict.conflict_id,
                    "status": "open",
                }
            ),
        ]
    )

    record = persist_conflict_assessment(
        session,
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        assessment=assessment,
        assessed_content_hashes={"proposal": H64},
        conflict=conflict,
        execution_id=UUID(int=53),
    )

    assert record.replayed is True
    assert record.conflict_status == "open"
    assert len(session.statements) == 4


def test_assessment_replay_rejects_divergent_content_sources() -> None:
    assessment, conflict = _conflict_pair()
    session = _Session(
        [
            _Result(one={"status": "active"}),
            _Result(one={"proposal_revision_id": UUID(int=50)}),
            _Result(
                one={
                    "assessment_id": assessment.assessment_id,
                    "assessed_content_hashes": [
                        {"scope_key": "proposal", "content_hash": H64}
                    ],
                }
            ),
        ]
    )

    with pytest.raises(ArbitrationStoreError) as raised:
        persist_conflict_assessment(
            session,
            approval_run_id=RUN_ID,
            ticket_id=TICKET_ID,
            assessment=assessment,
            assessed_content_hashes={"proposal": "b" * 64},
            conflict=conflict,
            execution_id=UUID(int=53),
        )

    assert raised.value.code == "assessment_content_hashes_mismatch"
    assert len(session.statements) == 3


def _patch_authorization(monkeypatch):
    identity = _identity()
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_authenticated_identity",
        lambda *args, **kwargs: identity,
    )
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_effective_authorization",
        lambda *args, **kwargs: SimpleNamespace(
            authorization_id=AUTH_ID,
            authorization_version=1,
        ),
    )


def test_request_changes_is_authenticated_audited_and_does_not_resolve(monkeypatch) -> None:
    _patch_authorization(monkeypatch)
    resolution = _resolution()
    conflict = {
        "conflict_id": CONFLICT_ID,
        "fingerprint": H64,
        "trigger_id": "returns-sla-breach",
        "affected_targets": canonical_value(resolution.targets),
        "status": "open",
        "state_version": 4,
    }
    session = _Session(
        [
            _Result(one={"status": "active"}),
            _Result(one=conflict),
            _Result(one=None),
            _Result(one=None),
            _Result(),
            _Result(rowcount=1),
            _Result(scalar=7),
            _Result(rowcount=1),
            _Result(rowcount=1),
        ]
    )

    record = record_arbitration_resolution(
        session,
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        resolution=resolution,
        idempotency_key=UUID(int=40),
        authentication=_authentication(),
        execution_id=UUID(int=41),
    )

    assert record.conflict_status == "changes_requested"
    assert record.action == "request_changes"
    assert record.replayed is False
    assert session.results == []


def test_revoked_or_expired_arbitration_grant_fails_before_business_write(
    monkeypatch,
) -> None:
    identity = _identity()
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_authenticated_identity",
        lambda *args, **kwargs: identity,
    )

    def denied(*args, **kwargs):
        raise AuthorizationDenied("authorization_revoked")

    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_effective_authorization", denied
    )
    session = _Session([_Result(one={"status": "active"})])

    with pytest.raises(AuthorizationDenied) as raised:
        record_arbitration_resolution(
            session,
            approval_run_id=RUN_ID,
            ticket_id=TICKET_ID,
            resolution=_resolution(),
            idempotency_key=UUID(int=40),
            authentication=_authentication(),
            execution_id=UUID(int=41),
        )

    assert raised.value.code == "authorization_revoked"
    assert len(session.statements) == 1
def test_material_resolution_replay_is_reauthorized_without_new_write(monkeypatch) -> None:
    _patch_authorization(monkeypatch)
    resolution = _resolution()
    conflict = {
        "conflict_id": CONFLICT_ID,
        "fingerprint": H64,
        "trigger_id": "returns-sla-breach",
        "affected_targets": canonical_value(resolution.targets),
        "status": "changes_requested",
        "state_version": 5,
    }
    session = _Session(
        [
            _Result(one={"status": "active", "active_departments": ["reverse"]}),
            _Result(one=conflict),
            _Result(one=None),
            _Result(
                one={
                    "resolution_id": resolution.resolution_id,
                    "conflict_id": CONFLICT_ID,
                    "action": "request_changes",
                }
            ),
        ]
    )

    record = record_arbitration_resolution(
        session,
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        resolution=resolution,
        idempotency_key=UUID(int=42),
        authentication=_authentication(),
        execution_id=UUID(int=43),
    )

    assert record.replayed is True
    assert record.conflict_status == "changes_requested"
    assert len(session.statements) == 4


def test_resolution_with_targets_different_from_conflict_is_rejected(monkeypatch) -> None:
    _patch_authorization(monkeypatch)
    resolution = _resolution()
    session = _Session(
        [
            _Result(one={"status": "active", "active_departments": ["reverse"]}),
            _Result(
                one={
                    "conflict_id": CONFLICT_ID,
                    "fingerprint": H64,
                    "trigger_id": "returns-sla-breach",
                    "affected_targets": [
                        {
                            "department": "warehouse",
                            "department_revision_id": str(UUID(int=99)),
                            "proposal_revision_id": None,
                        }
                    ],
                    "status": "open",
                    "state_version": 1,
                }
            ),
        ]
    )

    with pytest.raises(ArbitrationStoreError) as raised:
        record_arbitration_resolution(
            session,
            approval_run_id=RUN_ID,
            ticket_id=TICKET_ID,
            resolution=resolution,
            idempotency_key=UUID(int=44),
            authentication=_authentication(),
            execution_id=UUID(int=45),
        )

    assert raised.value.code == "resolution_conflict_mismatch"
    assert len(session.statements) == 2


def test_corrected_revision_and_no_conflict_recheck_resolve_durable_conflict(
    monkeypatch,
) -> None:
    miguel_identity = ResolvedIdentity._from_persisted_binding(
        environment="production",
        auth_source="local",
        issuer="trackflow-local",
        subject="subject-user",
        trackflow_user_id="user-user",
        general_role="user",
        principal_id=PRINCIPAL_ID,
        binding_id=BINDING_ID,
        responsible_code="miguel_torres",
        synthetic=False,
    )
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_authenticated_identity",
        lambda *args, **kwargs: miguel_identity,
    )
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_effective_authorization",
        lambda *args, **kwargs: SimpleNamespace(
            authorization_id=AUTH_ID, authorization_version=3
        ),
    )
    resolution = _accepted_currency_resolution()
    conflict = {
        "conflict_id": resolution.conflict_id,
        "fingerprint": resolution.conflict_fingerprint,
        "trigger_id": resolution.trigger,
        "affected_targets": canonical_value(resolution.targets),
        "status": "changes_requested",
        "state_version": 6,
    }
    session = _Session(
        [
            _Result(one={"status": "active", "active_departments": ["warehouse", "lastmile"]}),
            _Result(one=conflict),
            _Result(one=None),
            _Result(one=None),
            _Result(
                one={
                    "assessment_hash": resolution.post_correction_assessment.assessment_hash
                }
            ),
            _Result(one={"proposal_revision_id": UUID(int=50)}),
            _Result(
                one={
                    "proposal_revision_id": UUID(int=70),
                    "is_current": True,
                    "content_hash": H64,
                    "revision_no": 2,
                    "validation_bundle_id": UUID(int=72),
                }
            ),
            _Result(one={"validation_bundle_id": UUID(int=72)}),
            _Result(
                one={
                    "assessed_content_hashes": [
                        {"scope_key": "proposal", "content_hash": H64}
                    ]
                }
            ),
            _Result(),
            _Result(),
            _Result(rowcount=1),
            _Result(scalar=10),
            _Result(rowcount=1),
            _Result(rowcount=1),
        ]
    )

    record = record_arbitration_resolution(
        session,
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        resolution=resolution,
        idempotency_key=UUID(int=76),
        authentication=_authentication(),
        execution_id=UUID(int=77),
    )

    assert record.conflict_status == "resolved"
    assert record.replayed is False
    assert session.results == []


def test_conflict_recheck_requires_hash_under_the_exact_target_scope(
    monkeypatch,
) -> None:
    miguel_identity = ResolvedIdentity._from_persisted_binding(
        environment="production",
        auth_source="local",
        issuer="trackflow-local",
        subject="subject-user",
        trackflow_user_id="user-user",
        general_role="user",
        principal_id=PRINCIPAL_ID,
        binding_id=BINDING_ID,
        responsible_code="miguel_torres",
        synthetic=False,
    )
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_authenticated_identity",
        lambda *args, **kwargs: miguel_identity,
    )
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_effective_authorization",
        lambda *args, **kwargs: SimpleNamespace(
            authorization_id=AUTH_ID, authorization_version=3
        ),
    )
    resolution = _accepted_currency_resolution()
    session = _Session(
        [
            _Result(one={"status": "active", "active_departments": ["warehouse"]}),
            _Result(
                one={
                    "conflict_id": resolution.conflict_id,
                    "fingerprint": resolution.conflict_fingerprint,
                    "trigger_id": resolution.trigger,
                    "affected_targets": canonical_value(resolution.targets),
                    "status": "changes_requested",
                    "state_version": 6,
                }
            ),
            _Result(one=None),
            _Result(one=None),
            _Result(
                one={
                    "assessment_hash": resolution.post_correction_assessment.assessment_hash
                }
            ),
            _Result(one={"proposal_revision_id": UUID(int=50)}),
            _Result(
                one={
                    "proposal_revision_id": UUID(int=70),
                    "is_current": True,
                    "content_hash": H64,
                    "revision_no": 2,
                    "validation_bundle_id": UUID(int=72),
                }
            ),
            _Result(one={"validation_bundle_id": UUID(int=72)}),
            _Result(
                one={
                    "assessed_content_hashes": [
                        {"scope_key": "warehouse", "content_hash": H64}
                    ]
                }
            ),
        ]
    )

    with pytest.raises(ArbitrationStoreError) as raised:
        record_arbitration_resolution(
            session,
            approval_run_id=RUN_ID,
            ticket_id=TICKET_ID,
            resolution=resolution,
            idempotency_key=UUID(int=76),
            authentication=_authentication(),
            execution_id=UUID(int=77),
        )

    assert raised.value.code == "conflict_recheck_not_persisted"
    assert session.results == []


def test_revision_four_is_rejected_without_resolving_the_conflict(monkeypatch) -> None:
    miguel_identity = _identity()
    object.__setattr__(miguel_identity, "responsible_code", "miguel_torres")
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_authenticated_identity",
        lambda *args, **kwargs: miguel_identity,
    )
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_effective_authorization",
        lambda *args, **kwargs: SimpleNamespace(
            authorization_id=AUTH_ID, authorization_version=3
        ),
    )
    resolution = _accepted_currency_resolution()
    session = _Session(
        [
            _Result(one={"status": "active", "active_departments": ["warehouse"]}),
            _Result(
                one={
                    "conflict_id": resolution.conflict_id,
                    "fingerprint": resolution.conflict_fingerprint,
                    "trigger_id": resolution.trigger,
                    "affected_targets": canonical_value(resolution.targets),
                    "status": "changes_requested",
                    "state_version": 6,
                }
            ),
            _Result(one=None),
            _Result(one=None),
            _Result(
                one={
                    "assessment_hash": resolution.post_correction_assessment.assessment_hash
                }
            ),
            _Result(one={"proposal_revision_id": UUID(int=50)}),
            _Result(
                one={
                    "proposal_revision_id": UUID(int=70),
                    "is_current": True,
                    "content_hash": H64,
                    "revision_no": 4,
                    "validation_bundle_id": UUID(int=72),
                }
            ),
        ]
    )

    with pytest.raises(ArbitrationStoreError) as raised:
        record_arbitration_resolution(
            session,
            approval_run_id=RUN_ID,
            ticket_id=TICKET_ID,
            resolution=resolution,
            idempotency_key=UUID(int=78),
            authentication=_authentication(),
            execution_id=UUID(int=79),
        )

    assert raised.value.code == "revision_limit_reached"
    assert session.results == []


def test_authority_target_is_exact_for_reverse_and_cross_department_triggers() -> None:
    reverse = _authorization_target(_resolution(), ["reverse"])
    assert reverse.department_scope == "reverse"
    assert reverse.target_policy == "department_exact"
    assert reverse.affected_departments == frozenset({"reverse"})

    assessment, conflict = _conflict_pair()
    del assessment
    principal = _resolution().execution.actor.principal
    actor = AuthorizedHumanActor(
        principal=principal,
        authorization_id=AUTH_ID,
        official_responsible="Miguel Torres",
        permission="arbitrate",
        department_scope=None,
        trigger_scope="currency-mismatch",
    )
    reason = "Keep the documented client-country currency; request an authorized quote."
    resolution_hash = arbitration_resolution_hash(
        conflict_id=conflict.conflict_id,
        conflict_fingerprint=conflict.fingerprint,
        action="request_changes",
        targets=conflict.affected_targets,
        authorized_input_ids=[],
        reason=reason,
    )
    currency_resolution = ArbitrationResolution(
        resolution_id=UUID(int=60),
        conflict_id=conflict.conflict_id,
        conflict_fingerprint=conflict.fingerprint,
        trigger="currency-mismatch",
        action="request_changes",
        targets=conflict.affected_targets,
        authorized_input_ids=[],
        reason=reason,
        execution=HumanArbitrationExecution(
            execution_id=UUID(int=61),
            conflict_id=conflict.conflict_id,
            actor=actor,
            decided_at=NOW,
        ),
        resolution_hash=resolution_hash,
    )
    currency = _authorization_target(currency_resolution, ["warehouse", "lastmile"])
    assert currency.target_policy == "cross_department"
    assert currency.affected_departments == frozenset({"warehouse", "lastmile"})


def test_ana_cannot_be_constructed_as_an_arbitrator() -> None:
    principal = _resolution().execution.actor.principal

    with pytest.raises(ValidationError, match="official TrackFlow arbitrator"):
        AuthorizedHumanActor(
            principal=principal,
            authorization_id=AUTH_ID,
            official_responsible="Ana Whitfield",
            permission="arbitrate",
            trigger_scope="returns-sla-breach",
        )


def test_identity_claiming_a_different_actor_is_rejected(monkeypatch) -> None:
    identity = _identity()
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_authenticated_identity",
        lambda *args, **kwargs: identity,
    )
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_effective_authorization",
        lambda *args, **kwargs: SimpleNamespace(
            authorization_id=UUID(int=999), authorization_version=1
        ),
    )
    session = _Session([_Result(one={"status": "active"})])

    with pytest.raises(ArbitrationStoreError) as raised:
        record_arbitration_resolution(
            session,
            approval_run_id=RUN_ID,
            ticket_id=TICKET_ID,
            resolution=_resolution(),
            idempotency_key=UUID(int=40),
            authentication=_authentication(),
            execution_id=UUID(int=41),
        )

    assert raised.value.code == "resolution_actor_identity_mismatch"
    assert len(session.statements) == 1

# Module-end marker prevents editors from adding a second blank line at EOF.
def test_actor_label_cannot_disagree_with_persisted_official_assignment(
    monkeypatch,
) -> None:
    miguel_identity = ResolvedIdentity._from_persisted_binding(
        environment="production",
        auth_source="local",
        issuer="trackflow-local",
        subject="subject-user",
        trackflow_user_id="user-user",
        general_role="user",
        principal_id=PRINCIPAL_ID,
        binding_id=BINDING_ID,
        responsible_code="miguel_torres",
        synthetic=False,
    )
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_authenticated_identity",
        lambda *args, **kwargs: miguel_identity,
    )
    monkeypatch.setattr(
        "services.rfp_approval.arbitration.resolve_effective_authorization",
        lambda *args, **kwargs: SimpleNamespace(
            authorization_id=AUTH_ID, authorization_version=1
        ),
    )
    session = _Session([_Result(one={"status": "active"})])

    with pytest.raises(ArbitrationStoreError) as raised:
        record_arbitration_resolution(
            session,
            approval_run_id=RUN_ID,
            ticket_id=TICKET_ID,
            resolution=_resolution(),
            idempotency_key=UUID(int=80),
            authentication=_authentication(),
            execution_id=UUID(int=81),
        )

    assert raised.value.code == "resolution_actor_identity_mismatch"
    assert len(session.statements) == 1
