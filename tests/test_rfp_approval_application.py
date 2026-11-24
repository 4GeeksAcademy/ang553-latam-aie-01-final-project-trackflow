from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from services.rfp_approval.application import (
    AuthenticatedDecisionApplicationService,
    DecisionApplicationServiceError,
)
from services.rfp_approval.authorization import AuthorizationDenied
from services.rfp_approval.identity import VerifiedAuthenticationContext
from services.rfp_approval.store import DecisionApplicationResult, DecisionClaim


NOW = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
DECISION_ID = UUID(int=1)
RUN_ID = UUID(int=2)
REVISION_ID = UUID(int=3)
PRINCIPAL_ID = UUID(int=4)
BINDING_ID = UUID(int=5)
AUTHORIZATION_ID = UUID(int=6)
CLAIM_TOKEN = UUID(int=7)
EXECUTION_ID = UUID(int=8)


def _authentication() -> VerifiedAuthenticationContext:
    return VerifiedAuthenticationContext._from_verified_authenticator(
        environment="qa",
        auth_source="oidc",
        issuer="urn:trackflow:qa:oidc",
        subject="carlos-subject",
        trackflow_user_id="carlos-qa",
        general_role="manager",
    )


def _identity(**changes):
    values = {
        "principal_id": PRINCIPAL_ID,
        "binding_id": BINDING_ID,
        "environment": "qa",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _row(**changes):
    values = {
        "decision_id": DECISION_ID,
        "actor_principal_id": PRINCIPAL_ID,
        "actor_binding_id": BINDING_ID,
        "authorization_id": AUTHORIZATION_ID,
        "authorization_version": 3,
        "environment": "qa",
        "processing_status": "queued",
        "claim_epoch": 0,
    }
    values.update(changes)
    return values


class _Result:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def one_or_none(self):
        return self.value


class _Session:
    def __init__(self, *rows):
        self.rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def begin(self):
        return nullcontext()

    def execute(self, statement):
        return _Result(self.rows.pop(0))


def _claim() -> DecisionClaim:
    return DecisionClaim(
        decision_id=DECISION_ID,
        approval_run_id=RUN_ID,
        revision_id=REVISION_ID,
        claim_token=CLAIM_TOKEN,
        claim_epoch=1,
        lease_until=NOW + timedelta(minutes=1),
        expected_revision_state_version=4,
    )


def _applied(status="approved", *, replayed=False) -> DecisionApplicationResult:
    return DecisionApplicationResult(
        decision_id=DECISION_ID,
        revision_id=REVISION_ID,
        action="approve",
        processing_status="applied",
        revision_status=status,
        revision_state_version=5,
        replayed=replayed,
    )


def test_authenticated_service_claims_exact_decision_and_applies_with_fence(
    monkeypatch,
) -> None:
    claimed = {}
    applied = {}
    monkeypatch.setattr(
        "services.rfp_approval.application.resolve_authenticated_identity",
        lambda session, *, context: _identity(),
    )

    def claim(session, **kwargs):
        claimed.update(kwargs)
        return _claim()

    def apply(session, **kwargs):
        applied.update(kwargs)
        return _applied()

    monkeypatch.setattr(
        "services.rfp_approval.application.claim_decision_by_id", claim
    )
    monkeypatch.setattr(
        "services.rfp_approval.application.apply_claimed_decision", apply
    )
    service = AuthenticatedDecisionApplicationService(lambda: _Session(_row()))

    result = service.apply(
        decision_id=DECISION_ID,
        authentication=_authentication(),
        execution_id=EXECUTION_ID,
        claim_token=CLAIM_TOKEN,
    )

    assert result.revision_status == "approved"
    assert claimed["decision_id"] == DECISION_ID
    assert claimed["claim_token"] == CLAIM_TOKEN
    assert applied["decision_id"] == DECISION_ID
    assert applied["claim_token"] == CLAIM_TOKEN
    assert applied["claim_epoch"] == 1
    assert applied["expected_revision_state_version"] == 4
    assert applied["authorization_reference"].authorization_id == AUTHORIZATION_ID
    assert applied["authorization_reference"].authorization_version == 3


def test_service_requires_sealed_verified_authentication_context() -> None:
    service = AuthenticatedDecisionApplicationService(lambda: _Session(_row()))

    with pytest.raises(DecisionApplicationServiceError) as raised:
        service.apply(
            decision_id=DECISION_ID,
            authentication=object(),  # type: ignore[arg-type]
        )

    assert raised.value.code == "verified_authentication_required"


@pytest.mark.parametrize(
    "identity",
    [
        _identity(principal_id=UUID(int=90)),
        _identity(binding_id=UUID(int=91)),
        _identity(environment="production"),
    ],
)
def test_actor_mismatch_is_rejected_before_claim(monkeypatch, identity) -> None:
    claims = []
    monkeypatch.setattr(
        "services.rfp_approval.application.resolve_authenticated_identity",
        lambda session, *, context: identity,
    )
    monkeypatch.setattr(
        "services.rfp_approval.application.claim_decision_by_id",
        lambda *args, **kwargs: claims.append(kwargs),
    )
    service = AuthenticatedDecisionApplicationService(lambda: _Session(_row()))

    with pytest.raises(DecisionApplicationServiceError) as raised:
        service.apply(decision_id=DECISION_ID, authentication=_authentication())

    assert raised.value.code == "decision_actor_mismatch"
    assert claims == []


def test_missing_or_unclaimable_decision_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.rfp_approval.application.resolve_authenticated_identity",
        lambda session, *, context: _identity(),
    )
    service = AuthenticatedDecisionApplicationService(lambda: _Session(None))
    with pytest.raises(DecisionApplicationServiceError) as missing:
        service.apply(decision_id=DECISION_ID, authentication=_authentication())
    assert missing.value.code == "decision_not_found"

    monkeypatch.setattr(
        "services.rfp_approval.application.claim_decision_by_id",
        lambda *args, **kwargs: None,
    )
    service = AuthenticatedDecisionApplicationService(
        lambda: _Session(_row(), _row(processing_status="claimed"))
    )
    with pytest.raises(DecisionApplicationServiceError) as unavailable:
        service.apply(decision_id=DECISION_ID, authentication=_authentication())
    assert unavailable.value.code == "decision_not_claimable"


def test_terminal_replay_reauthorizes_without_claiming(monkeypatch) -> None:
    claims = []
    applications = []
    monkeypatch.setattr(
        "services.rfp_approval.application.resolve_authenticated_identity",
        lambda session, *, context: _identity(),
    )
    monkeypatch.setattr(
        "services.rfp_approval.application.claim_decision_by_id",
        lambda *args, **kwargs: claims.append(kwargs),
    )

    def replay(session, **kwargs):
        applications.append(kwargs)
        return _applied(replayed=True)

    monkeypatch.setattr(
        "services.rfp_approval.application.apply_claimed_decision", replay
    )
    service = AuthenticatedDecisionApplicationService(
        lambda: _Session(_row(processing_status="applied", claim_epoch=1))
    )

    result = service.apply(
        decision_id=DECISION_ID,
        authentication=_authentication(),
        execution_id=EXECUTION_ID,
    )

    assert result.replayed is True
    assert claims == []
    assert applications[0]["decision_id"] == DECISION_ID


def test_revoked_authorization_rolls_out_of_application_service(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.rfp_approval.application.resolve_authenticated_identity",
        lambda session, *, context: _identity(),
    )
    monkeypatch.setattr(
        "services.rfp_approval.application.claim_decision_by_id",
        lambda *args, **kwargs: _claim(),
    )

    def denied(*args, **kwargs):
        raise AuthorizationDenied("authorization_revoked")

    monkeypatch.setattr(
        "services.rfp_approval.application.apply_claimed_decision", denied
    )
    service = AuthenticatedDecisionApplicationService(lambda: _Session(_row()))

    with pytest.raises(AuthorizationDenied) as raised:
        service.apply(decision_id=DECISION_ID, authentication=_authentication())

    assert raised.value.code == "authorization_revoked"


@pytest.mark.parametrize(
    "revision_status", ["approved", "rejected", "changes_requested", "superseded"]
)
def test_service_returns_exact_store_outcome(monkeypatch, revision_status) -> None:
    monkeypatch.setattr(
        "services.rfp_approval.application.resolve_authenticated_identity",
        lambda session, *, context: _identity(),
    )
    monkeypatch.setattr(
        "services.rfp_approval.application.claim_decision_by_id",
        lambda *args, **kwargs: _claim(),
    )
    monkeypatch.setattr(
        "services.rfp_approval.application.apply_claimed_decision",
        lambda *args, **kwargs: _applied(revision_status),
    )
    service = AuthenticatedDecisionApplicationService(lambda: _Session(_row()))

    result = service.apply(
        decision_id=DECISION_ID, authentication=_authentication()
    )

    assert result.revision_status == revision_status
    assert (result.revision_status == "approved") is (revision_status == "approved")
