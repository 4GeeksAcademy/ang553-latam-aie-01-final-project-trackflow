"""Authenticated application of durable RFP human decisions.

This service is intentionally request-scoped.  It receives a sealed
``VerifiedAuthenticationContext`` from the authenticator, resolves the current
persisted identity, claims exactly the requested decision, and delegates all
business mutation, authorization revalidation, fencing and CAS behavior to the
existing approval store.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlmodel import Session

from services.rfp_approval.authorization import AuthorizationReference
from services.rfp_approval.identity import (
    ResolvedIdentity,
    VerifiedAuthenticationContext,
    resolve_authenticated_identity,
)
from services.rfp_approval.models import rfp_human_decisions
from services.rfp_approval.store import (
    DecisionApplicationResult,
    apply_claimed_decision,
    claim_decision_by_id,
)


SessionFactory = Callable[[], AbstractContextManager[Session]]


class DecisionApplicationServiceError(RuntimeError):
    """Fail-closed request/application coordination error."""

    def __init__(self, code: str) -> None:
        super().__init__("RFP human decision application was rejected")
        self.code = code


class AuthenticatedDecisionApplicationService:
    """Claim and apply one exact decision in a caller-owned transaction."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _decision_row(session: Session, decision_id: UUID) -> dict[str, object] | None:
        row = session.execute(
            select(
                rfp_human_decisions.c.decision_id,
                rfp_human_decisions.c.actor_principal_id,
                rfp_human_decisions.c.actor_binding_id,
                rfp_human_decisions.c.authorization_id,
                rfp_human_decisions.c.authorization_version,
                rfp_human_decisions.c.environment,
                rfp_human_decisions.c.processing_status,
                rfp_human_decisions.c.claim_epoch,
            ).where(rfp_human_decisions.c.decision_id == decision_id)
        ).mappings().one_or_none()
        return dict(row) if row is not None else None

    @staticmethod
    def _assert_actor(
        row: dict[str, object], identity: ResolvedIdentity
    ) -> None:
        if (
            row["actor_principal_id"] != identity.principal_id
            or row["actor_binding_id"] != identity.binding_id
            or row["environment"] != identity.environment
        ):
            raise DecisionApplicationServiceError("decision_actor_mismatch")

    @staticmethod
    def _reference(row: dict[str, object]) -> AuthorizationReference:
        return AuthorizationReference(
            authorization_id=row["authorization_id"],  # type: ignore[arg-type]
            authorization_version=row["authorization_version"],  # type: ignore[arg-type]
        )

    @staticmethod
    def _terminal_replay(
        session: Session,
        *,
        row: dict[str, object],
        identity: ResolvedIdentity,
        reference: AuthorizationReference,
        execution_id: UUID,
    ) -> DecisionApplicationResult:
        # Terminal replay is authorized again by apply_claimed_decision before
        # any persisted result is returned. Fence arguments are intentionally
        # ignored by the store only for a coherent terminal decision.
        return apply_claimed_decision(
            session,
            decision_id=row["decision_id"],  # type: ignore[arg-type]
            claim_token=uuid4(),
            claim_epoch=row["claim_epoch"],  # type: ignore[arg-type]
            expected_revision_state_version=0,
            identity=identity,
            authorization_reference=reference,
            execution_id=execution_id,
        )

    def apply(
        self,
        *,
        decision_id: UUID,
        authentication: VerifiedAuthenticationContext,
        execution_id: UUID | None = None,
        lease_seconds: int = 60,
        claim_token: UUID | None = None,
    ) -> DecisionApplicationResult:
        """Apply or securely replay one decision using current authentication."""

        if not isinstance(authentication, VerifiedAuthenticationContext):
            raise DecisionApplicationServiceError("verified_authentication_required")
        operation_id = execution_id or uuid4()
        with self._session_factory() as session, session.begin():
            identity = resolve_authenticated_identity(session, context=authentication)
            row = self._decision_row(session, decision_id)
            if row is None:
                raise DecisionApplicationServiceError("decision_not_found")
            self._assert_actor(row, identity)
            reference = self._reference(row)
            if row["processing_status"] in {"applied", "rejected_stale"}:
                return self._terminal_replay(
                    session,
                    row=row,
                    identity=identity,
                    reference=reference,
                    execution_id=operation_id,
                )

            claim = claim_decision_by_id(
                session,
                decision_id=decision_id,
                execution_id=operation_id,
                lease_seconds=lease_seconds,
                claim_token=claim_token,
            )
            if claim is None:
                # A concurrent request may have committed a terminal result
                # after the initial read. Re-read and authorize that replay;
                # otherwise no row is changed by this transaction.
                current = self._decision_row(session, decision_id)
                if current is not None and current["processing_status"] in {
                    "applied",
                    "rejected_stale",
                }:
                    self._assert_actor(current, identity)
                    return self._terminal_replay(
                        session,
                        row=current,
                        identity=identity,
                        reference=self._reference(current),
                        execution_id=operation_id,
                    )
                raise DecisionApplicationServiceError("decision_not_claimable")
            if claim.decision_id != decision_id:
                raise DecisionApplicationServiceError("claimed_decision_mismatch")
            return apply_claimed_decision(
                session,
                decision_id=claim.decision_id,
                claim_token=claim.claim_token,
                claim_epoch=claim.claim_epoch,
                expected_revision_state_version=claim.expected_revision_state_version,
                identity=identity,
                authorization_reference=reference,
                execution_id=operation_id,
            )
