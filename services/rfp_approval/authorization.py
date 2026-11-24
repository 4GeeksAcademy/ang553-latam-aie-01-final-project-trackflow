"""Persisted, exact-scope authorization for RFP approval operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlmodel import Session

from services.rfp_approval.identity import (
    Environment,
    ResolvedIdentity,
    ResponsibleCode,
    _is_resolved_identity,
)
from services.rfp_approval.models import (
    rfp_authorizations,
    rfp_identity_bindings,
    rfp_principals,
)


Permission = Literal[
    "start_approval_run",
    "manage_approval_run",
    "approve_department",
    "provide_business_input",
    "arbitrate",
]
Department = Literal["warehouse", "lastmile", "reverse"]
Trigger = Literal["volume-vs-capacity", "returns-sla-breach", "currency-mismatch"]
TargetPolicy = Literal[
    "run", "department_exact", "proposal_wide", "cross_department", "non_reverse_or_cross"
]

_DEPARTMENT_OWNER = {
    "warehouse": "ana_whitfield",
    "lastmile": "carlos_vega",
    "reverse": "sofia_ramos",
}


class AuthorizationDenied(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("persisted authorization denied")
        self.code = code


@dataclass(frozen=True, slots=True)
class AuthorizationReference:
    authorization_id: UUID
    authorization_version: int

    def __post_init__(self) -> None:
        if self.authorization_version < 1:
            raise ValueError("authorization_version must be positive")


@dataclass(frozen=True, slots=True)
class AuthorizationTarget:
    permission: Permission
    department_scope: Department | None = None
    trigger_scope: Trigger | None = None
    target_policy: TargetPolicy = "run"
    affected_departments: frozenset[Department] = frozenset()

    def __post_init__(self) -> None:
        if self.permission in {"start_approval_run", "manage_approval_run"}:
            valid = (
                self.department_scope is None
                and self.trigger_scope is None
                and self.target_policy == "run"
                and not self.affected_departments
            )
        elif self.permission == "approve_department":
            valid = (
                self.department_scope is not None
                and self.trigger_scope is None
                and self.target_policy == "department_exact"
                and self.affected_departments == frozenset({self.department_scope})
            )
        elif self.permission == "provide_business_input":
            valid = self.trigger_scope is None and (
                (
                    self.department_scope is not None
                    and self.target_policy == "department_exact"
                    and self.affected_departments == frozenset({self.department_scope})
                )
                or (
                    self.department_scope is None
                    and self.target_policy == "proposal_wide"
                    and not self.affected_departments
                )
            )
        else:
            valid = self.trigger_scope is not None and bool(self.affected_departments)
        if not valid:
            raise ValueError("authorization target shape is invalid")


@dataclass(frozen=True, slots=True)
class EffectiveAuthorization:
    authorization_id: UUID
    authorization_version: int
    principal_id: UUID
    binding_id: UUID
    environment: Environment
    permission: Permission
    department_scope: Department | None
    trigger_scope: Trigger | None
    target_policy: TargetPolicy
    evaluated_at: datetime


def _official_policy(
    responsible: ResponsibleCode | None, target: AuthorizationTarget
) -> None:
    if target.permission == "approve_department":
        if responsible != _DEPARTMENT_OWNER[target.department_scope]:
            raise AuthorizationDenied("official_department_owner_required")
        return
    if target.permission == "provide_business_input":
        if responsible == "miguel_torres":
            if target.department_scope is not None or target.target_policy != "proposal_wide":
                raise AuthorizationDenied("global_business_input_required")
        elif responsible != _DEPARTMENT_OWNER.get(target.department_scope):
            raise AuthorizationDenied("official_department_owner_required")
        return
    if target.permission == "arbitrate":
        if responsible == "sofia_ramos":
            if not (
                target.trigger_scope == "returns-sla-breach"
                and target.department_scope == "reverse"
                and target.target_policy == "department_exact"
                and target.affected_departments == frozenset({"reverse"})
            ):
                raise AuthorizationDenied("sofia_reverse_returns_only")
            return
        if responsible == "miguel_torres":
            if target.department_scope is not None:
                raise AuthorizationDenied("miguel_cross_scope_required")
            if target.trigger_scope in {"volume-vs-capacity", "currency-mismatch"}:
                if target.target_policy != "cross_department":
                    raise AuthorizationDenied("miguel_cross_scope_required")
                return
            if target.trigger_scope == "returns-sla-breach":
                if (
                    target.target_policy != "non_reverse_or_cross"
                    or not (target.affected_departments - {"reverse"})
                ):
                    raise AuthorizationDenied("reverse_returns_reserved_for_sofia")
                return
        raise AuthorizationDenied("official_arbitrator_required")
    # Run administration remains grant-based; a general role is never consulted.


def _effective_authorization(
    identity: ResolvedIdentity,
    target: AuthorizationTarget,
    row: dict[str, object],
    reference: AuthorizationReference | None,
) -> EffectiveAuthorization:
    if not _is_resolved_identity(identity):
        raise AuthorizationDenied("unresolved_identity")
    if not bool(row["principal_active"]):
        raise AuthorizationDenied("principal_inactive")
    if not bool(row["binding_active"]):
        raise AuthorizationDenied("binding_inactive")
    if row["environment"] != identity.environment:
        raise AuthorizationDenied("environment_mismatch")
    if row["principal_id"] != identity.principal_id or row["binding_id"] != identity.binding_id:
        raise AuthorizationDenied("identity_mismatch")
    current_responsible = row["current_responsible_code"]
    if current_responsible != identity.responsible_code:
        raise AuthorizationDenied("official_assignment_changed")
    if identity.environment == "production" and (identity.synthetic or bool(row["synthetic"])):
        raise AuthorizationDenied("synthetic_production_identity")
    if row["permission"] != target.permission:
        raise AuthorizationDenied("permission_mismatch")
    if row["department_scope"] != target.department_scope:
        raise AuthorizationDenied("department_scope_mismatch")
    if row["trigger_scope"] != target.trigger_scope:
        raise AuthorizationDenied("trigger_scope_mismatch")
    if row["target_policy"] != target.target_policy:
        raise AuthorizationDenied("target_policy_mismatch")
    evaluated_at = row["evaluated_at"]
    if not isinstance(evaluated_at, datetime) or evaluated_at.tzinfo is None:
        raise AuthorizationDenied("invalid_evaluation_time")
    if row["active_from"] > evaluated_at:  # type: ignore[operator]
        raise AuthorizationDenied("authorization_not_started")
    expires_at = row["expires_at"]
    if expires_at is not None and expires_at <= evaluated_at:  # type: ignore[operator]
        raise AuthorizationDenied("authorization_expired")
    if row["revoked_at"] is not None:
        raise AuthorizationDenied("authorization_revoked")
    if reference is not None and (
        row["authorization_id"] != reference.authorization_id
        or row["authorization_version"] != reference.authorization_version
    ):
        raise AuthorizationDenied("authorization_reference_mismatch")
    _official_policy(current_responsible, target)  # type: ignore[arg-type]
    return EffectiveAuthorization(
        authorization_id=row["authorization_id"],  # type: ignore[arg-type]
        authorization_version=row["authorization_version"],  # type: ignore[arg-type]
        principal_id=identity.principal_id,
        binding_id=identity.binding_id,
        environment=identity.environment,
        permission=target.permission,
        department_scope=target.department_scope,
        trigger_scope=target.trigger_scope,
        target_policy=target.target_policy,
        evaluated_at=evaluated_at,
    )


def resolve_effective_authorization(
    session: Session,
    *,
    identity: ResolvedIdentity,
    target: AuthorizationTarget,
    reference: AuthorizationReference | None = None,
    lock_rows: bool = False,
) -> EffectiveAuthorization:
    """Resolve and revalidate one exact grant inside the caller transaction.

    ``lock_rows`` makes the read suitable for a later transactional store, but
    this module alone does not claim end-to-end TOCTOU protection.  Gate 2B.3
    must keep the locks through the business write and test expiry/revocation
    races, including authorization reads that wait for a concurrent lock.
    """

    if not _is_resolved_identity(identity):
        raise AuthorizationDenied("unresolved_identity")
    statement_now = func.statement_timestamp()
    statement = (
        select(
            rfp_authorizations.c.authorization_id,
            rfp_authorizations.c.authorization_version,
            rfp_authorizations.c.principal_id,
            rfp_authorizations.c.environment,
            rfp_authorizations.c.permission,
            rfp_authorizations.c.department_scope,
            rfp_authorizations.c.trigger_scope,
            rfp_authorizations.c.target_policy,
            rfp_authorizations.c.active_from,
            rfp_authorizations.c.expires_at,
            rfp_authorizations.c.revoked_at,
            rfp_principals.c.synthetic,
            rfp_principals.c.trackflow_user_id,
            rfp_principals.c.responsible_code.label("current_responsible_code"),
            rfp_principals.c.active.label("principal_active"),
            rfp_identity_bindings.c.binding_id,
            rfp_identity_bindings.c.auth_source,
            rfp_identity_bindings.c.issuer,
            rfp_identity_bindings.c.subject,
            rfp_identity_bindings.c.active.label("binding_active"),
            statement_now.label("evaluated_at"),
        )
        .select_from(
            rfp_authorizations.join(
                rfp_principals,
                and_(
                    rfp_principals.c.principal_id == rfp_authorizations.c.principal_id,
                    rfp_principals.c.environment == rfp_authorizations.c.environment,
                ),
            ).join(
                rfp_identity_bindings,
                and_(
                    rfp_identity_bindings.c.principal_id == rfp_authorizations.c.principal_id,
                    rfp_identity_bindings.c.environment == rfp_authorizations.c.environment,
                    rfp_identity_bindings.c.binding_id == identity.binding_id,
                    rfp_identity_bindings.c.auth_source == identity.auth_source,
                    rfp_identity_bindings.c.issuer == identity.issuer,
                    rfp_identity_bindings.c.subject == identity.subject,
                ),
            )
        )
        .where(
            rfp_authorizations.c.principal_id == identity.principal_id,
            rfp_authorizations.c.environment == identity.environment,
            rfp_principals.c.trackflow_user_id == identity.trackflow_user_id,
            rfp_principals.c.responsible_code.is_(identity.responsible_code)
            if identity.responsible_code is None
            else rfp_principals.c.responsible_code == identity.responsible_code,
            rfp_authorizations.c.permission == target.permission,
            rfp_authorizations.c.department_scope.is_(target.department_scope)
            if target.department_scope is None
            else rfp_authorizations.c.department_scope == target.department_scope,
            rfp_authorizations.c.trigger_scope.is_(target.trigger_scope)
            if target.trigger_scope is None
            else rfp_authorizations.c.trigger_scope == target.trigger_scope,
            rfp_authorizations.c.target_policy == target.target_policy,
            rfp_principals.c.active.is_(True),
            rfp_identity_bindings.c.active.is_(True),
            rfp_authorizations.c.active_from <= statement_now,
            or_(
                rfp_authorizations.c.expires_at.is_(None),
                rfp_authorizations.c.expires_at > statement_now,
            ),
            rfp_authorizations.c.revoked_at.is_(None),
        )
    )
    if reference is not None:
        statement = statement.where(
            rfp_authorizations.c.authorization_id == reference.authorization_id,
            rfp_authorizations.c.authorization_version == reference.authorization_version,
        )
    if lock_rows:
        statement = statement.with_for_update()
    rows = session.execute(statement).mappings().all()
    if not rows:
        raise AuthorizationDenied("no_effective_authorization")
    if len(rows) != 1:
        raise AuthorizationDenied("ambiguous_effective_authorization")
    return _effective_authorization(identity, target, dict(rows[0]), reference)
