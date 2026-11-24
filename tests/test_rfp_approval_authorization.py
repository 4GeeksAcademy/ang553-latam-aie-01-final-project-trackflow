from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from services.rfp_approval.authorization import (
    AuthorizationDenied,
    AuthorizationReference,
    AuthorizationTarget,
    _effective_authorization,
    resolve_effective_authorization,
)
from services.rfp_approval.identity import ResolvedIdentity


NOW = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-000000000201")
PRINCIPAL_ID = UUID("00000000-0000-0000-0000-000000000202")
BINDING_ID = UUID("00000000-0000-0000-0000-000000000203")


def _identity(
    responsible: str | None,
    *,
    role: str = "user",
    environment: str = "production",
    synthetic: bool = False,
) -> ResolvedIdentity:
    return ResolvedIdentity._from_persisted_binding(
        environment=environment,
        auth_source="oidc",
        issuer="https://identity.trackflow.example/realms/trackflow",
        subject=f"subject-{responsible or 'admin'}",
        trackflow_user_id=f"user-{responsible or 'admin'}",
        general_role=role,
        principal_id=PRINCIPAL_ID,
        binding_id=BINDING_ID,
        responsible_code=responsible,
        synthetic=synthetic,
    )


def _row(
    identity: ResolvedIdentity,
    target: AuthorizationTarget,
    **changes: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "authorization_id": AUTHORIZATION_ID,
        "authorization_version": 3,
        "principal_id": identity.principal_id,
        "binding_id": identity.binding_id,
        "environment": identity.environment,
        "permission": target.permission,
        "department_scope": target.department_scope,
        "trigger_scope": target.trigger_scope,
        "target_policy": target.target_policy,
        "active_from": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=1),
        "revoked_at": None,
        "synthetic": identity.synthetic,
        "current_responsible_code": identity.responsible_code,
        "principal_active": True,
        "binding_active": True,
        "evaluated_at": NOW,
    }
    row.update(changes)
    return row


def _evaluate(identity: ResolvedIdentity, target: AuthorizationTarget, **changes: object):
    return _effective_authorization(identity, target, _row(identity, target, **changes), None)


WAREHOUSE_APPROVAL = AuthorizationTarget(
    permission="approve_department",
    department_scope="warehouse",
    target_policy="department_exact",
    affected_departments=frozenset({"warehouse"}),
)
LASTMILE_APPROVAL = AuthorizationTarget(
    permission="approve_department",
    department_scope="lastmile",
    target_policy="department_exact",
    affected_departments=frozenset({"lastmile"}),
)
REVERSE_APPROVAL = AuthorizationTarget(
    permission="approve_department",
    department_scope="reverse",
    target_policy="department_exact",
    affected_departments=frozenset({"reverse"}),
)


@pytest.mark.parametrize(
    "responsible,target",
    [
        ("ana_whitfield", WAREHOUSE_APPROVAL),
        ("carlos_vega", LASTMILE_APPROVAL),
        ("sofia_ramos", REVERSE_APPROVAL),
        (
            "ana_whitfield",
            AuthorizationTarget(
                permission="provide_business_input",
                department_scope="warehouse",
                target_policy="department_exact",
                affected_departments=frozenset({"warehouse"}),
            ),
        ),
        (
            "carlos_vega",
            AuthorizationTarget(
                permission="provide_business_input",
                department_scope="lastmile",
                target_policy="department_exact",
                affected_departments=frozenset({"lastmile"}),
            ),
        ),
        (
            "sofia_ramos",
            AuthorizationTarget(
                permission="provide_business_input",
                department_scope="reverse",
                target_policy="department_exact",
                affected_departments=frozenset({"reverse"}),
            ),
        ),
        (
            "miguel_torres",
            AuthorizationTarget(
                permission="provide_business_input", target_policy="proposal_wide"
            ),
        ),
    ],
)
def test_official_department_and_business_input_matrix_allows_exact_grants(
    responsible: str, target: AuthorizationTarget
) -> None:
    effective = _evaluate(_identity(responsible), target)
    assert effective.permission == target.permission
    assert effective.department_scope == target.department_scope


def test_admin_role_never_implies_department_approval() -> None:
    with pytest.raises(AuthorizationDenied) as raised:
        _evaluate(_identity(None, role="admin"), WAREHOUSE_APPROVAL)
    assert raised.value.code == "official_department_owner_required"


def test_admin_without_persisted_grant_is_denied_and_role_is_not_a_grant() -> None:
    admin = _identity(None, role="admin")
    with pytest.raises(AuthorizationDenied) as raised:
        resolve_effective_authorization(
            _Session([]), identity=admin, target=WAREHOUSE_APPROVAL
        )
    assert raised.value.code == "no_effective_authorization"


def test_general_admin_role_does_not_override_anas_explicit_official_grant() -> None:
    effective = _evaluate(_identity("ana_whitfield", role="admin"), WAREHOUSE_APPROVAL)
    assert effective.permission == "approve_department"


@pytest.mark.parametrize(
    "responsible,target",
    [
        ("carlos_vega", WAREHOUSE_APPROVAL),
        ("ana_whitfield", LASTMILE_APPROVAL),
    ],
)
def test_department_owners_cannot_approve_each_others_sections(
    responsible: str, target: AuthorizationTarget
) -> None:
    with pytest.raises(AuthorizationDenied) as raised:
        _evaluate(_identity(responsible), target)
    assert raised.value.code == "official_department_owner_required"


def test_sofia_arbitrates_only_reverse_returns_sla() -> None:
    allowed = AuthorizationTarget(
        permission="arbitrate",
        department_scope="reverse",
        trigger_scope="returns-sla-breach",
        target_policy="department_exact",
        affected_departments=frozenset({"reverse"}),
    )
    assert _evaluate(_identity("sofia_ramos"), allowed).trigger_scope == "returns-sla-breach"
    forbidden = AuthorizationTarget(
        permission="arbitrate",
        trigger_scope="currency-mismatch",
        target_policy="cross_department",
        affected_departments=frozenset({"warehouse", "lastmile"}),
    )
    with pytest.raises(AuthorizationDenied) as raised:
        _evaluate(_identity("sofia_ramos"), forbidden)
    assert raised.value.code == "sofia_reverse_returns_only"


@pytest.mark.parametrize("trigger", ["volume-vs-capacity", "currency-mismatch"])
def test_miguel_arbitrates_official_cross_department_triggers(trigger: str) -> None:
    target = AuthorizationTarget(
        permission="arbitrate",
        trigger_scope=trigger,
        target_policy="cross_department",
        affected_departments=frozenset({"warehouse", "lastmile"}),
    )
    assert _evaluate(_identity("miguel_torres"), target).trigger_scope == trigger


def test_miguel_handles_returns_only_outside_reverse_exclusive_scope() -> None:
    allowed = AuthorizationTarget(
        permission="arbitrate",
        trigger_scope="returns-sla-breach",
        target_policy="non_reverse_or_cross",
        affected_departments=frozenset({"warehouse"}),
    )
    assert _evaluate(_identity("miguel_torres"), allowed).trigger_scope == "returns-sla-breach"
    reverse_only = AuthorizationTarget(
        permission="arbitrate",
        trigger_scope="returns-sla-breach",
        target_policy="non_reverse_or_cross",
        affected_departments=frozenset({"reverse"}),
    )
    with pytest.raises(AuthorizationDenied) as raised:
        _evaluate(_identity("miguel_torres"), reverse_only)
    assert raised.value.code == "reverse_returns_reserved_for_sofia"


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"principal_active": False}, "principal_inactive"),
        ({"binding_active": False}, "binding_inactive"),
        ({"revoked_at": NOW - timedelta(minutes=1)}, "authorization_revoked"),
        ({"expires_at": NOW}, "authorization_expired"),
        ({"active_from": NOW + timedelta(minutes=1)}, "authorization_not_started"),
        ({"permission": "manage_approval_run"}, "permission_mismatch"),
        ({"department_scope": "lastmile"}, "department_scope_mismatch"),
        ({"environment": "qa"}, "environment_mismatch"),
    ],
)
def test_inactive_revoked_expired_or_future_grants_are_rejected(
    changes: dict[str, object], code: str
) -> None:
    with pytest.raises(AuthorizationDenied) as raised:
        _evaluate(_identity("ana_whitfield"), WAREHOUSE_APPROVAL, **changes)
    assert raised.value.code == code


def test_authorization_reference_requires_exact_id_and_version() -> None:
    identity = _identity("ana_whitfield")
    reference = AuthorizationReference(AUTHORIZATION_ID, 4)
    with pytest.raises(AuthorizationDenied) as raised:
        _effective_authorization(
            identity,
            WAREHOUSE_APPROVAL,
            _row(identity, WAREHOUSE_APPROVAL),
            reference,
        )
    assert raised.value.code == "authorization_reference_mismatch"


def test_synthetic_identity_cannot_authorize_in_production() -> None:
    with pytest.raises(AuthorizationDenied) as raised:
        _evaluate(
            _identity("ana_whitfield", environment="production", synthetic=True),
            WAREHOUSE_APPROVAL,
        )
    assert raised.value.code == "synthetic_production_identity"


def test_stale_identity_is_rejected_after_official_assignment_changes() -> None:
    stale_ana = _identity("ana_whitfield")
    with pytest.raises(AuthorizationDenied) as raised:
        _evaluate(
            stale_ana,
            WAREHOUSE_APPROVAL,
            current_responsible_code="carlos_vega",
        )
    assert raised.value.code == "official_assignment_changed"


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> "_Rows":
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows


class _Session:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _Rows(self.rows)


def test_postgresql_query_rechecks_exact_scope_time_state_and_reference() -> None:
    identity = _identity("ana_whitfield")
    reference = AuthorizationReference(AUTHORIZATION_ID, 3)
    session = _Session([_row(identity, WAREHOUSE_APPROVAL)])
    effective = resolve_effective_authorization(
        session,
        identity=identity,
        target=WAREHOUSE_APPROVAL,
        reference=reference,
        lock_rows=True,
    )
    assert effective.authorization_id == AUTHORIZATION_ID
    compiled = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled
    assert "responsible_code" in compiled
    assert "active_from <= statement_timestamp()" in compiled
    assert "expires_at IS NULL" in compiled
    assert "revoked_at IS NULL" in compiled
    assert "authorization_version" in compiled


def test_postgresql_query_uses_current_assignment_and_statement_time() -> None:
    identity = _identity("ana_whitfield")
    session = _Session([_row(identity, WAREHOUSE_APPROVAL)])
    resolve_effective_authorization(
        session,
        identity=identity,
        target=WAREHOUSE_APPROVAL,
        lock_rows=True,
    )
    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "rfp_principals.responsible_code AS current_responsible_code" in compiled
    assert "rfp_principals.responsible_code = 'ana_whitfield'" in compiled
    assert compiled.count("statement_timestamp()") == 3
    assert "CURRENT_TIMESTAMP" not in compiled
    assert compiled.rstrip().endswith("FOR UPDATE")


def test_missing_or_ambiguous_effective_grant_is_denied() -> None:
    identity = _identity("ana_whitfield")
    with pytest.raises(AuthorizationDenied) as missing:
        resolve_effective_authorization(
            _Session([]), identity=identity, target=WAREHOUSE_APPROVAL
        )
    assert missing.value.code == "no_effective_authorization"
    duplicate = _row(identity, WAREHOUSE_APPROVAL)
    with pytest.raises(AuthorizationDenied) as ambiguous:
        resolve_effective_authorization(
            _Session([duplicate, duplicate]), identity=identity, target=WAREHOUSE_APPROVAL
        )
    assert ambiguous.value.code == "ambiguous_effective_authorization"
