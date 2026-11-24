from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from services.rfp_approval.identity import (
    IdentityResolutionDenied,
    VerifiedAuthenticationContext,
    resolve_authenticated_identity,
)


PRINCIPAL_ID = UUID("00000000-0000-0000-0000-000000000101")
BINDING_ID = UUID("00000000-0000-0000-0000-000000000102")


class _MappingResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    def mappings(self) -> "_MappingResult":
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self.row


class _Session:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _MappingResult(self.row)


def _context(
    *,
    environment: str = "production",
    auth_source: str = "oidc",
    issuer: str = "https://identity.trackflow.example/realms/trackflow",
    subject: str = "oidc-subject-ana",
    user_id: str = "trackflow-user-ana",
    role: str = "user",
) -> VerifiedAuthenticationContext:
    return VerifiedAuthenticationContext._from_verified_authenticator(
        environment=environment,
        auth_source=auth_source,
        issuer=issuer,
        subject=subject,
        trackflow_user_id=user_id,
        general_role=role,
    )


def _row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "principal_id": PRINCIPAL_ID,
        "binding_id": BINDING_ID,
        "environment": "production",
        "auth_source": "oidc",
        "issuer": "https://identity.trackflow.example/realms/trackflow",
        "subject": "oidc-subject-ana",
        "trackflow_user_id": "trackflow-user-ana",
        "responsible_code": "ana_whitfield",
        "synthetic": False,
        "principal_active": True,
        "binding_active": True,
    }
    row.update(changes)
    return row


def test_context_cannot_be_constructed_without_authenticator_proof() -> None:
    with pytest.raises(TypeError, match="authenticator proof"):
        VerifiedAuthenticationContext(
            environment="production",
            auth_source="oidc",
            issuer="https://issuer.example",
            subject="subject",
            trackflow_user_id="user",
            general_role="admin",
            _proof=object(),
        )


def test_resolved_identity_cannot_be_constructed_from_request_ids() -> None:
    from services.rfp_approval.identity import ResolvedIdentity

    with pytest.raises(TypeError, match="persisted resolution proof"):
        ResolvedIdentity(
            environment="production",
            auth_source="oidc",
            issuer="https://issuer.example",
            subject="subject",
            trackflow_user_id="user",
            general_role="admin",
            principal_id=PRINCIPAL_ID,
            binding_id=BINDING_ID,
            responsible_code="ana_whitfield",
            synthetic=False,
            _proof=object(),
        )


def test_local_context_requires_environment_specific_trusted_issuer() -> None:
    with pytest.raises(ValueError, match="local issuer"):
        _context(auth_source="local", issuer="trackflow-local", environment="qa")
    local = _context(
        auth_source="local",
        issuer="urn:trackflow:qa:local",
        environment="qa",
        subject="local-user",
        user_id="local-user",
    )
    assert local.subject == local.trackflow_user_id == "local-user"


def test_resolves_exact_verified_binding_without_client_selected_ids() -> None:
    session = _Session(_row())
    identity = resolve_authenticated_identity(session, context=_context())
    assert identity.principal_id == PRINCIPAL_ID
    assert identity.binding_id == BINDING_ID
    assert identity.responsible_code == "ana_whitfield"
    assert "principal_id" not in VerifiedAuthenticationContext.__dataclass_fields__
    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "rfp_identity_bindings.issuer" in compiled
    assert "rfp_identity_bindings.subject" in compiled
    assert "rfp_principals.trackflow_user_id" in compiled


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"principal_active": False}, "principal_inactive"),
        ({"binding_active": False}, "binding_inactive"),
        ({"environment": "qa"}, "environment_mismatch"),
        ({"subject": "another-subject"}, "authenticated_subject_mismatch"),
        ({"trackflow_user_id": "another-user"}, "trackflow_user_mismatch"),
    ],
)
def test_rejects_inactive_or_mismatched_persisted_identity(
    changes: dict[str, object], code: str
) -> None:
    with pytest.raises(IdentityResolutionDenied) as raised:
        resolve_authenticated_identity(_Session(_row(**changes)), context=_context())
    assert raised.value.code == code


def test_missing_binding_is_denied_without_name_or_email_fallback() -> None:
    with pytest.raises(IdentityResolutionDenied) as raised:
        resolve_authenticated_identity(_Session(None), context=_context(role="admin"))
    assert raised.value.code == "binding_not_found"


def test_synthetic_identity_is_never_resolved_as_production() -> None:
    with pytest.raises(IdentityResolutionDenied) as raised:
        resolve_authenticated_identity(
            _Session(_row(synthetic=True)), context=_context(environment="production")
        )
    assert raised.value.code == "synthetic_production_identity"


def test_qa_token_cannot_resolve_a_production_binding() -> None:
    qa_context = _context(
        environment="qa",
        issuer="https://qa.identity.trackflow.example/realms/trackflow",
    )
    with pytest.raises(IdentityResolutionDenied) as raised:
        resolve_authenticated_identity(_Session(_row()), context=qa_context)
    assert raised.value.code == "environment_mismatch"
