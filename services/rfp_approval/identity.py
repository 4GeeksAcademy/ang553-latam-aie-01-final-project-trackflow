"""Resolve authenticated TrackFlow identities to persisted RFP principals.

The context accepted here is an internal hand-off from an authenticator.  It is
not an HTTP/request model and deliberately contains no client-selectable
``principal_id`` or ``binding_id`` fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, select
from sqlmodel import Session

from services.rfp_approval.models import rfp_identity_bindings, rfp_principals


Environment = Literal["production", "qa"]
AuthSource = Literal["local", "oidc"]
GeneralRole = Literal["admin", "manager", "user"]
ResponsibleCode = Literal[
    "ana_whitfield", "carlos_vega", "sofia_ramos", "miguel_torres"
]

_AUTHENTICATOR_PROOF = object()
_RESOLUTION_PROOF = object()


class IdentityResolutionDenied(RuntimeError):
    """An authenticated subject has no active, coherent persisted binding."""

    def __init__(self, code: str) -> None:
        super().__init__("authenticated identity is not authorized for RFP approval")
        self.code = code


@dataclass(frozen=True, slots=True, init=False)
class VerifiedAuthenticationContext:
    """Sealed result produced only after local/OIDC authentication succeeds.

    The future API adapter must call ``_from_verified_authenticator`` with
    claims already verified by the local JWT decoder or strict OIDC decoder.
    Request payloads must never be deserialized into this type.
    """

    environment: Environment
    auth_source: AuthSource
    issuer: str
    subject: str
    trackflow_user_id: str
    general_role: GeneralRole
    _proof: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        environment: Environment,
        auth_source: AuthSource,
        issuer: str,
        subject: str,
        trackflow_user_id: str,
        general_role: GeneralRole,
        _proof: object,
    ) -> None:
        if _proof is not _AUTHENTICATOR_PROOF:
            raise TypeError("VerifiedAuthenticationContext requires authenticator proof")
        if environment not in {"production", "qa"}:
            raise ValueError("unsupported authentication environment")
        if auth_source not in {"local", "oidc"}:
            raise ValueError("unsupported authentication source")
        if general_role not in {"admin", "manager", "user"}:
            raise ValueError("unsupported general role")
        values = {"issuer": issuer, "subject": subject, "trackflow_user_id": trackflow_user_id}
        if any(not value.strip() for value in values.values()):
            raise ValueError("verified authentication identifiers must be non-empty")
        expected_local_issuer = (
            "trackflow-local" if environment == "production" else "urn:trackflow:qa:local"
        )
        if auth_source == "local" and issuer != expected_local_issuer:
            raise ValueError("local issuer does not match the authenticated environment")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "auth_source", auth_source)
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "trackflow_user_id", trackflow_user_id)
        object.__setattr__(self, "general_role", general_role)
        object.__setattr__(self, "_proof", _proof)

    @classmethod
    def _from_verified_authenticator(
        cls,
        *,
        environment: Environment,
        auth_source: AuthSource,
        issuer: str,
        subject: str,
        trackflow_user_id: str,
        general_role: GeneralRole,
    ) -> "VerifiedAuthenticationContext":
        """Internal adapter seam; callers must already have verified credentials."""

        return cls(
            environment=environment,
            auth_source=auth_source,
            issuer=issuer,
            subject=subject,
            trackflow_user_id=trackflow_user_id,
            general_role=general_role,
            _proof=_AUTHENTICATOR_PROOF,
        )


@dataclass(frozen=True, slots=True, init=False)
class ResolvedIdentity:
    environment: Environment
    auth_source: AuthSource
    issuer: str
    subject: str
    trackflow_user_id: str
    general_role: GeneralRole
    principal_id: UUID
    binding_id: UUID
    responsible_code: ResponsibleCode | None
    synthetic: bool
    _proof: object = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        environment: Environment,
        auth_source: AuthSource,
        issuer: str,
        subject: str,
        trackflow_user_id: str,
        general_role: GeneralRole,
        principal_id: UUID,
        binding_id: UUID,
        responsible_code: ResponsibleCode | None,
        synthetic: bool,
        _proof: object,
    ) -> None:
        if _proof is not _RESOLUTION_PROOF:
            raise TypeError("ResolvedIdentity requires persisted resolution proof")
        values = {
            "environment": environment,
            "auth_source": auth_source,
            "issuer": issuer,
            "subject": subject,
            "trackflow_user_id": trackflow_user_id,
            "general_role": general_role,
            "principal_id": principal_id,
            "binding_id": binding_id,
            "responsible_code": responsible_code,
            "synthetic": synthetic,
            "_proof": _proof,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @classmethod
    def _from_persisted_binding(
        cls,
        *,
        environment: Environment,
        auth_source: AuthSource,
        issuer: str,
        subject: str,
        trackflow_user_id: str,
        general_role: GeneralRole,
        principal_id: UUID,
        binding_id: UUID,
        responsible_code: ResponsibleCode | None,
        synthetic: bool,
    ) -> "ResolvedIdentity":
        return cls(
            environment=environment,
            auth_source=auth_source,
            issuer=issuer,
            subject=subject,
            trackflow_user_id=trackflow_user_id,
            general_role=general_role,
            principal_id=principal_id,
            binding_id=binding_id,
            responsible_code=responsible_code,
            synthetic=synthetic,
            _proof=_RESOLUTION_PROOF,
        )


def _is_resolved_identity(value: object) -> bool:
    return isinstance(value, ResolvedIdentity) and value._proof is _RESOLUTION_PROOF


def _resolved_identity(
    context: VerifiedAuthenticationContext, row: dict[str, object] | None
) -> ResolvedIdentity:
    if context._proof is not _AUTHENTICATOR_PROOF:
        raise IdentityResolutionDenied("unverified_context")
    if row is None:
        raise IdentityResolutionDenied("binding_not_found")
    if not bool(row["principal_active"]):
        raise IdentityResolutionDenied("principal_inactive")
    if not bool(row["binding_active"]):
        raise IdentityResolutionDenied("binding_inactive")
    if row["environment"] != context.environment:
        raise IdentityResolutionDenied("environment_mismatch")
    if row["auth_source"] != context.auth_source:
        raise IdentityResolutionDenied("authentication_source_mismatch")
    if row["issuer"] != context.issuer or row["subject"] != context.subject:
        raise IdentityResolutionDenied("authenticated_subject_mismatch")
    if row["trackflow_user_id"] != context.trackflow_user_id:
        raise IdentityResolutionDenied("trackflow_user_mismatch")
    synthetic = bool(row["synthetic"])
    if context.environment == "production" and synthetic:
        raise IdentityResolutionDenied("synthetic_production_identity")
    return ResolvedIdentity._from_persisted_binding(
        environment=context.environment,
        auth_source=context.auth_source,
        issuer=context.issuer,
        subject=context.subject,
        trackflow_user_id=context.trackflow_user_id,
        general_role=context.general_role,
        principal_id=row["principal_id"],  # type: ignore[arg-type]
        binding_id=row["binding_id"],  # type: ignore[arg-type]
        responsible_code=row["responsible_code"],  # type: ignore[arg-type]
        synthetic=synthetic,
    )


def resolve_authenticated_identity(
    session: Session, *, context: VerifiedAuthenticationContext
) -> ResolvedIdentity:
    """Resolve an exact verified issuer/subject binding in the caller transaction."""

    if not isinstance(context, VerifiedAuthenticationContext):
        raise IdentityResolutionDenied("unverified_context")
    statement = (
        select(
            rfp_principals.c.principal_id,
            rfp_principals.c.trackflow_user_id,
            rfp_principals.c.responsible_code,
            rfp_principals.c.synthetic,
            rfp_principals.c.active.label("principal_active"),
            rfp_identity_bindings.c.binding_id,
            rfp_identity_bindings.c.environment,
            rfp_identity_bindings.c.auth_source,
            rfp_identity_bindings.c.issuer,
            rfp_identity_bindings.c.subject,
            rfp_identity_bindings.c.active.label("binding_active"),
        )
        .select_from(
            rfp_identity_bindings.join(
                rfp_principals,
                and_(
                    rfp_principals.c.principal_id == rfp_identity_bindings.c.principal_id,
                    rfp_principals.c.environment == rfp_identity_bindings.c.environment,
                ),
            )
        )
        .where(
            rfp_identity_bindings.c.environment == context.environment,
            rfp_identity_bindings.c.auth_source == context.auth_source,
            rfp_identity_bindings.c.issuer == context.issuer,
            rfp_identity_bindings.c.subject == context.subject,
            rfp_principals.c.trackflow_user_id == context.trackflow_user_id,
        )
    )
    row = session.execute(statement).mappings().one_or_none()
    return _resolved_identity(context, dict(row) if row is not None else None)
