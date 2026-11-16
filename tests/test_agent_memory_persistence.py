"""MEM-092 persistence, authorization, policy and state-machine tests."""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable
from sqlalchemy.pool import StaticPool
from sqlmodel import Session

from services.api.access_control import AccessContext
from services.api.agent_memory.contracts import (
    EntryStatus,
    MemoryCategory,
    MemoryProposalCreate,
    MemoryScope,
    ProposalOutcome,
    ProposalStatus,
)
from services.api.agent_memory.models import (
    memory_decisions,
    memory_entries,
    memory_proposals,
)
from services.api.agent_memory.service import (
    MemoryConflict,
    MemoryForbidden,
    MemoryPolicyViolation,
    MemoryStore,
    canonical_key_for,
)
from services.api.auth_models import Role
from services.incidents.models import (
    clients,
    operational_security_metadata,
    user_client_memberships,
)


CLIENT_A = "11111111-1111-4111-8111-111111111111"
CLIENT_B = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    operational_security_metadata.create_all(engine)
    with Session(engine) as db:
        db.execute(
            insert(clients),
            [
                {
                    "client_id": CLIENT_A,
                    "display_name": "Verified A",
                    "is_active": True,
                    "created_by_user_id": "admin",
                },
                {
                    "client_id": CLIENT_B,
                    "display_name": "Verified B",
                    "is_active": True,
                    "created_by_user_id": "admin",
                },
            ],
        )
        db.execute(
            insert(user_client_memberships),
            [
                {
                    "user_id": "manager-a",
                    "client_id": CLIENT_A,
                    "can_view_sensitive": False,
                    "assigned_by_user_id": "admin",
                },
                {
                    "user_id": "manager-b",
                    "client_id": CLIENT_B,
                    "can_view_sensitive": False,
                    "assigned_by_user_id": "admin",
                },
            ],
        )
        db.commit()
        yield db


def access(user_id: str, role: Role, *client_ids: str) -> AccessContext:
    return AccessContext(
        user_id=user_id,
        role=role,
        allowed_client_ids=frozenset(client_ids),
        sensitive_client_ids=frozenset(),
        global_access=role is Role.admin,
    )


def carrier_payload(
    *,
    conversation_id=None,
    interaction="turn-1",
    client_id=CLIENT_A,
    rule="SEUR no longer covers the rural Zaragoza zone.",
) -> MemoryProposalCreate:
    content = {"carrier": "SEUR", "country": "ES", "coverage_rule": rule}
    return MemoryProposalCreate(
        conversation_id=conversation_id or uuid4(),
        origin_interaction_id=interaction,
        origin_message_sha256=hashlib.sha256(interaction.encode()).hexdigest(),
        scope=MemoryScope.client,
        client_id=client_id,
        category=MemoryCategory.carrier_coverage,
        content=content,
        canonical_key=canonical_key_for(MemoryCategory.carrier_coverage, content),
    )


def b2b_payload(conversation_id=None) -> MemoryProposalCreate:
    content = {
        "preference_type": "metric_priority",
        "preference": "Show returns before shipment volume.",
    }
    return MemoryProposalCreate(
        conversation_id=conversation_id or uuid4(),
        origin_interaction_id="turn-b2b",
        origin_message_sha256="a" * 64,
        scope=MemoryScope.client,
        client_id=CLIENT_A,
        category=MemoryCategory.b2b_report_preference,
        content=content,
        canonical_key=canonical_key_for(
            MemoryCategory.b2b_report_preference, content
        ),
    )


def test_approved_proposal_is_audited_consolidated_and_retrievable(session: Session):
    store = MemoryStore()
    manager = access("manager-a", Role.manager, CLIENT_A)
    created = store.create_proposal(session, carrier_payload(), manager, now=NOW)

    result = store.resolve_proposal(
        session,
        created.id,
        ProposalOutcome.approved,
        "decision-1",
        manager,
        now=NOW + timedelta(minutes=1),
    )

    assert result.proposal.status is ProposalStatus.approved
    assert result.decision.outcome is ProposalOutcome.approved
    assert result.entry is not None
    assert result.entry.authorizing_decision_id == result.decision.id
    assert result.entry.verification_status == "user_reported"
    assert store.list_active(session, manager, now=NOW + timedelta(minutes=2)) == [
        result.entry
    ]
    assert session.execute(select(memory_decisions)).mappings().all()[0][
        "actor_user_id"
    ] == "manager-a"


def test_rejected_proposal_is_audited_without_active_memory(session: Session):
    store = MemoryStore()
    manager = access("manager-a", Role.manager, CLIENT_A)
    created = store.create_proposal(session, carrier_payload(), manager, now=NOW)
    result = store.resolve_proposal(
        session,
        created.id,
        ProposalOutcome.rejected,
        "decision-reject",
        manager,
        now=NOW + timedelta(minutes=1),
    )
    assert result.decision.outcome is ProposalOutcome.rejected
    assert result.entry is None
    assert store.list_active(session, manager, now=NOW + timedelta(minutes=2)) == []


def test_only_one_pending_per_owned_conversation_and_origin_is_idempotent(
    session: Session,
):
    store = MemoryStore()
    manager = access("manager-a", Role.manager, CLIENT_A)
    conversation_id = uuid4()
    payload = carrier_payload(conversation_id=conversation_id)
    first = store.create_proposal(session, payload, manager, now=NOW)
    retry = store.create_proposal(session, payload, manager, now=NOW)
    assert retry.id == first.id
    with pytest.raises(MemoryConflict):
        store.create_proposal(
            session,
            b2b_payload(conversation_id=conversation_id),
            manager,
            now=NOW,
        )


def test_decision_retry_is_idempotent_and_key_cannot_be_reused(session: Session):
    store = MemoryStore()
    manager = access("manager-a", Role.manager, CLIENT_A)
    first = store.create_proposal(session, carrier_payload(), manager, now=NOW)
    accepted = store.resolve_proposal(
        session, first.id, ProposalOutcome.approved, "same-key", manager, now=NOW
    )
    retry = store.resolve_proposal(
        session, first.id, ProposalOutcome.approved, "same-key", manager, now=NOW
    )
    assert retry.decision.id == accepted.decision.id

    second = store.create_proposal(
        session, carrier_payload(interaction="turn-2"), manager, now=NOW
    )
    with pytest.raises(MemoryConflict):
        store.resolve_proposal(
            session, second.id, ProposalOutcome.rejected, "same-key", manager, now=NOW
        )


def test_idempotency_key_is_scoped_to_actor(session: Session):
    store = MemoryStore()
    manager_a = access("manager-a", Role.manager, CLIENT_A)
    manager_b = access("manager-b", Role.manager, CLIENT_B)
    proposal_a = store.create_proposal(session, carrier_payload(), manager_a, now=NOW)
    proposal_b = store.create_proposal(
        session,
        carrier_payload(client_id=CLIENT_B, interaction="manager-b-turn"),
        manager_b,
        now=NOW,
    )

    decision_a = store.resolve_proposal(
        session, proposal_a.id, ProposalOutcome.rejected, "shared-key", manager_a, now=NOW
    )
    decision_b = store.resolve_proposal(
        session, proposal_b.id, ProposalOutcome.rejected, "shared-key", manager_b, now=NOW
    )

    assert decision_a.decision.id != decision_b.decision.id


def test_consolidation_conflict_rolls_back_proposal_and_decision(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    store = MemoryStore()
    manager = access("manager-a", Role.manager, CLIENT_A)
    proposal = store.create_proposal(session, carrier_payload(), manager, now=NOW)

    def fail_consolidation(*_args, **_kwargs):
        raise MemoryConflict("simulated consolidation race")

    monkeypatch.setattr(store, "_consolidate", fail_consolidation)
    with pytest.raises(MemoryConflict, match="simulated consolidation race"):
        store.resolve_proposal(
            session,
            proposal.id,
            ProposalOutcome.approved,
            "rollback-key",
            manager,
            now=NOW,
        )

    persisted = session.execute(
        select(memory_proposals).where(memory_proposals.c.id == str(proposal.id))
    ).mappings().one()
    assert persisted["status"] == ProposalStatus.pending.value
    assert persisted["resolved_at"] is None
    assert persisted["version"] == 1
    assert session.execute(select(memory_decisions)).first() is None
    assert session.execute(select(memory_entries)).first() is None


def test_cross_tenant_and_cross_identity_resolution_are_hidden(session: Session):
    store = MemoryStore()
    manager_a = access("manager-a", Role.manager, CLIENT_A)
    created = store.create_proposal(session, carrier_payload(), manager_a, now=NOW)
    manager_b = access("manager-b", Role.manager, CLIENT_B)
    assert store.get_pending(session, created.conversation_id, manager_b) is None
    with pytest.raises(Exception) as exc:
        store.resolve_proposal(
            session, created.id, ProposalOutcome.approved, "foreign", manager_b, now=NOW
        )
    assert "not found" in str(exc.value)
    assert store.list_active(session, manager_b, now=NOW) == []


def test_global_memory_is_admin_only_and_client_scope_requires_membership(
    session: Session,
):
    store = MemoryStore()
    content = {"carrier": "DHL", "country": "US", "coverage_rule": "Use air service."}
    global_payload = MemoryProposalCreate(
        conversation_id=uuid4(),
        origin_interaction_id="global-1",
        origin_message_sha256="b" * 64,
        scope=MemoryScope.global_,
        category=MemoryCategory.carrier_coverage,
        content=content,
        canonical_key=canonical_key_for(MemoryCategory.carrier_coverage, content),
    )
    with pytest.raises(MemoryForbidden):
        store.create_proposal(
            session, global_payload, access("manager-a", Role.manager, CLIENT_A), now=NOW
        )
    created = store.create_proposal(
        session, global_payload, access("admin", Role.admin), now=NOW
    )
    assert created.scope is MemoryScope.global_
    with pytest.raises(MemoryForbidden):
        store.create_proposal(
            session,
            carrier_payload(client_id=CLIENT_B),
            access("manager-a", Role.manager, CLIENT_A),
            now=NOW,
        )


def test_user_role_cannot_read_even_global_memory(session: Session):
    store = MemoryStore()
    admin = access("admin", Role.admin)
    content = {"carrier": "DHL", "country": "US", "coverage_rule": "Use air service."}
    payload = MemoryProposalCreate(
        conversation_id=uuid4(),
        origin_interaction_id="global-read-guard",
        origin_message_sha256="d" * 64,
        scope=MemoryScope.global_,
        category=MemoryCategory.carrier_coverage,
        content=content,
        canonical_key=canonical_key_for(MemoryCategory.carrier_coverage, content),
    )
    proposal = store.create_proposal(session, payload, admin, now=NOW)
    store.resolve_proposal(
        session, proposal.id, ProposalOutcome.approved, "global-approval", admin, now=NOW
    )

    with pytest.raises(MemoryForbidden):
        store.list_active(session, access("basic-user", Role.user), now=NOW)


@pytest.mark.parametrize(
    "rule",
    [
        "Deliver to 123 Main Street.",
        "Entregar en Calle Mayor 12.",
        "Entregar en C/ Mayor 12.",
        "La dirección B2B está confirmada.",
        "Use internal warehouse route through aisle C.",
        "The active negotiation includes a pricing offer.",
        "Tracking XJ447112 is delayed.",
        "Bearer abc.def.ghi",
        "DATABASE password=hunter2",
    ],
)
def test_prohibited_content_is_rejected_before_any_persistence(
    session: Session, rule: str
):
    store = MemoryStore()
    with pytest.raises(MemoryPolicyViolation):
        store.create_proposal(
            session,
            carrier_payload(rule=rule),
            access("manager-a", Role.manager, CLIENT_A),
            now=NOW,
        )
    assert session.execute(select(memory_proposals)).first() is None
    assert session.execute(select(memory_decisions)).first() is None


def test_operational_zone_is_not_mistaken_for_a_street_address(session: Session):
    store = MemoryStore()
    proposal = store.create_proposal(
        session,
        carrier_payload(rule="SEUR cubre la zona rural de Zaragoza los martes."),
        access("manager-a", Role.manager, CLIENT_A),
        now=NOW,
    )
    assert proposal.status is ProposalStatus.pending


def test_single_package_is_not_a_recurring_incident(session: Session):
    store = MemoryStore()
    content = {
        "country": "US",
        "pattern": "One delayed parcel",
        "operational_context": "Only one report exists.",
        "recurrence_count": 1,
    }
    payload = MemoryProposalCreate(
        conversation_id=uuid4(),
        origin_interaction_id="isolated",
        origin_message_sha256="c" * 64,
        scope=MemoryScope.client,
        client_id=CLIENT_A,
        category=MemoryCategory.recurring_incident,
        content=content,
        canonical_key="us:placeholder",
    )
    with pytest.raises(MemoryPolicyViolation):
        store.create_proposal(
            session, payload, access("manager-a", Role.manager, CLIENT_A), now=NOW
        )


def test_edit_records_decision_and_requires_confirmation_of_new_proposal(
    session: Session,
):
    store = MemoryStore()
    manager = access("manager-a", Role.manager, CLIENT_A)
    conversation_id = uuid4()
    original = store.create_proposal(
        session, carrier_payload(conversation_id=conversation_id), manager, now=NOW
    )
    edited = carrier_payload(
        conversation_id=conversation_id,
        interaction="edit-turn",
        rule="SEUR serves the zone only on weekdays.",
    )
    result = store.resolve_proposal(
        session,
        original.id,
        ProposalOutcome.edited,
        "edit-decision",
        manager,
        edited_payload=edited,
        now=NOW + timedelta(minutes=1),
    )
    assert result.proposal.status is ProposalStatus.edited
    assert result.entry is None
    assert result.replacement_proposal is not None
    assert result.replacement_proposal.status is ProposalStatus.pending
    assert result.replacement_proposal.parent_proposal_id == original.id


def test_expiry_is_audited_and_time_bound_entries_are_cleaned(session: Session):
    store = MemoryStore()
    manager = access("manager-a", Role.manager, CLIENT_A)
    pending = store.create_proposal(session, carrier_payload(), manager, now=NOW)
    proposal_count, entry_count = store.expire_and_clean(
        session, now=NOW + timedelta(hours=25)
    )
    assert (proposal_count, entry_count) == (1, 0)
    assert store.get_pending(session, pending.conversation_id, manager) is None
    decision = session.execute(select(memory_decisions)).mappings().one()
    assert decision["outcome"] == ProposalOutcome.expired.value

    approved = store.create_proposal(
        session,
        carrier_payload(interaction="later"),
        manager,
        now=NOW + timedelta(days=1),
    )
    result = store.resolve_proposal(
        session,
        approved.id,
        ProposalOutcome.approved,
        "approve-later",
        manager,
        now=NOW + timedelta(days=1),
    )
    assert result.entry is not None
    proposal_count, entry_count = store.expire_and_clean(
        session, now=NOW + timedelta(days=32)
    )
    assert (proposal_count, entry_count) == (0, 1)
    status = session.execute(select(memory_entries.c.status)).scalar_one()
    assert status == EntryStatus.expired.value


def test_approval_after_requested_validity_expires_creates_no_entry(session: Session):
    store = MemoryStore()
    manager = access("manager-a", Role.manager, CLIENT_A)
    payload = carrier_payload().model_copy(
        update={"valid_until": NOW + timedelta(minutes=5)}
    )
    proposal = store.create_proposal(session, payload, manager, now=NOW)

    result = store.resolve_proposal(
        session,
        proposal.id,
        ProposalOutcome.approved,
        "late-approval",
        manager,
        now=NOW + timedelta(minutes=6),
    )

    assert result.decision.outcome is ProposalOutcome.expired
    assert result.entry is None
    assert session.execute(select(memory_entries)).first() is None


def test_changed_fact_supersedes_active_version_without_duplicate(session: Session):
    store = MemoryStore()
    manager = access("manager-a", Role.manager, CLIENT_A)
    first = store.create_proposal(session, carrier_payload(), manager, now=NOW)
    first_result = store.resolve_proposal(
        session, first.id, ProposalOutcome.approved, "d1", manager, now=NOW
    )
    second = store.create_proposal(
        session,
        carrier_payload(
            interaction="changed",
            rule="SEUR covers the rural Zaragoza zone on weekdays only.",
        ),
        manager,
        now=NOW + timedelta(minutes=2),
    )
    second_result = store.resolve_proposal(
        session,
        second.id,
        ProposalOutcome.approved,
        "d2",
        manager,
        now=NOW + timedelta(minutes=3),
    )
    assert first_result.entry is not None and second_result.entry is not None
    assert second_result.entry.version == 2
    assert second_result.entry.supersedes_entry_id == first_result.entry.id
    statuses = session.execute(
        select(memory_entries.c.status).order_by(memory_entries.c.version)
    ).scalars().all()
    assert statuses == [EntryStatus.superseded.value, EntryStatus.active.value]


def test_ddl_is_explicit_append_only_and_separate_from_sqlmodel():
    from sqlmodel import SQLModel

    assert "agent_memory_proposal" not in SQLModel.metadata.tables
    root = Path(__file__).resolve().parents[1]
    ddl = (root / "infra/sql/agent_memory.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE public.agent_memory_proposal" in ddl
    assert "CREATE TABLE public.agent_memory_conversation" in ddl
    assert "fk_agent_memory_proposal_conversation_owner" in ddl
    assert "CREATE TABLE public.agent_memory_decision" in ddl
    assert "CREATE TABLE public.agent_memory_entry" in ddl
    assert "uq_agent_memory_one_pending_conversation" in ddl
    assert "agent_memory_decision_append_only" in ddl
    assert "uq_agent_memory_decision_actor_idempotency" in ddl
    assert "authorizing_decision_id uuid NOT NULL UNIQUE" in ddl
    assert "CREATE FUNCTION public.enforce_agent_memory_entry_authorization" in ddl
    assert "decision.outcome = 'approved'" in ddl
    assert "proposal.status = 'approved'" in ddl
    assert "proposal.client_id IS NOT DISTINCT FROM NEW.client_id" in ddl
    assert "proposal.content = NEW.content" in ddl
    assert "agent_memory_entry_authorization" in ddl
    assert "content jsonb" in ddl
    assert "origin_message" not in ddl.replace("origin_message_sha256", "")


def test_uuid_models_compile_for_postgresql_and_sqlite():
    for table in (memory_proposals, memory_decisions, memory_entries):
        postgres_sql = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        sqlite_sql = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        assert "UUID" in postgres_sql
        assert "CREATE TABLE" in sqlite_sql
        assert table.c.id.type.compile(dialect=postgresql.dialect()) == "UUID"
        assert table.c.id.type.compile(dialect=sqlite.dialect()) == "CHAR(32)"
    assert memory_proposals.c.conversation_id.type.compile(
        dialect=postgresql.dialect()
    ) == "UUID"
    assert memory_decisions.c.proposal_id.type.compile(
        dialect=postgresql.dialect()
    ) == "UUID"
    assert memory_entries.c.authorizing_decision_id.type.compile(
        dialect=postgresql.dialect()
    ) == "UUID"
