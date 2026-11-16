"""Opt-in MEM-092 integration against an empty, disposable PostgreSQL database.

The suite never reads ``DATABASE_URL`` as its target and never starts
infrastructure.  An operator must provide a dedicated loopback database via
``TRACKFLOW_MEMORY_TEST_DATABASE_URL``.  The database name must clearly identify
it as TrackFlow QA and its public schema must be empty before this suite applies
the reviewed incident and memory DDL artifacts.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from threading import Barrier
from typing import Mapping
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import Session

from services.api.access_control import AccessContext
from services.api.agent_memory.contracts import (
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
from services.api.agent_memory.service import MemoryConflict, MemoryStore, canonical_key_for
from services.api.auth_models import Role
from services.incidents.models import clients
from scripts.agent_memory_cleanup import execute_cleanup


TEST_URL_ENV = "TRACKFLOW_MEMORY_TEST_DATABASE_URL"
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
_QA_DATABASE = re.compile(r"(?:^|[_-])(?:test|qa|mem092)(?:[_-]|$)")


def _target(url: URL) -> tuple[str | None, int, str | None]:
    host = "127.0.0.1" if url.host in {"localhost", "127.0.0.1", "::1"} else url.host
    return host, url.port or 5432, url.database


def _validated_test_url(raw_url: str, environment: Mapping[str, str]) -> URL:
    if not raw_url:
        raise ValueError(f"{TEST_URL_ENV} is required")
    url = make_url(raw_url)
    if url.drivername not in {"postgresql", "postgresql+psycopg2"}:
        raise ValueError("memory integration requires PostgreSQL with psycopg2")
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg2")
    if url.host not in {"localhost", "127.0.0.1", "::1"} or url.port is None:
        raise ValueError("memory integration requires loopback with an explicit port")
    database = (url.database or "").casefold()
    if "trackflow" not in database or _QA_DATABASE.search(database) is None:
        raise ValueError("memory integration requires a clearly named TrackFlow QA database")
    production_url = environment.get("DATABASE_URL")
    if production_url:
        if raw_url == production_url:
            raise ValueError(f"{TEST_URL_ENV} must not reuse DATABASE_URL")
        try:
            if _target(url) == _target(make_url(production_url)):
                raise ValueError(f"{TEST_URL_ENV} must not target the DATABASE_URL database")
        except ValueError:
            raise
        except Exception:
            pass
    return url


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    raw_url = os.getenv(TEST_URL_ENV)
    if not raw_url:
        pytest.skip(f"{TEST_URL_ENV} is required for opt-in PostgreSQL integration")
    url = _validated_test_url(raw_url, os.environ)
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            identity = connection.execute(
                text("SELECT current_database(), inet_server_addr()::text")
            ).one()
            if identity[0] != url.database:
                pytest.fail("connected database identity does not match the authorized URL")
            object_count = connection.execute(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')"
                )
            ).scalar_one()
            function_count = connection.execute(
                text(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname='public'"
                )
            ).scalar_one()
        if object_count or function_count:
            pytest.fail("memory integration requires an empty synthetic public schema")

        incident_ddl = (ROOT / "infra/sql/incident_query_security.sql").read_text(
            encoding="utf-8"
        )
        memory_ddl = (ROOT / "infra/sql/agent_memory.sql").read_text(encoding="utf-8")
        raw_connection = engine.raw_connection()
        try:
            with raw_connection.cursor() as cursor:
                cursor.execute(incident_ddl)
                cursor.execute(memory_ddl)
            raw_connection.commit()
        finally:
            raw_connection.close()
        yield engine
    finally:
        engine.dispose()


def _access(user_id: str, role: Role, *client_ids: str) -> AccessContext:
    return AccessContext(
        user_id=user_id,
        role=role,
        allowed_client_ids=frozenset(client_ids),
        sensitive_client_ids=frozenset(),
        global_access=role is Role.admin,
    )


def _add_client(engine: Engine, label: str) -> str:
    client_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            insert(clients).values(
                client_id=client_id,
                display_name=f"MEM-092 QA {label} {uuid4().hex}",
                is_active=True,
                created_by_user_id="qa-fixture",
            )
        )
    return client_id


def _conversation_insert(conversation_id: str, actor: str):
    return text(
        "INSERT INTO agent_memory_conversation "
        "(id,user_id,created_at,last_seen_at) VALUES "
        "(:id,:actor,:created,:created)"
    ), {"id": conversation_id, "actor": actor, "created": NOW}


def _carrier_payload(
    client_id: str,
    *,
    conversation_id: UUID | None = None,
    interaction: str | None = None,
) -> MemoryProposalCreate:
    origin = interaction or f"qa-{uuid4().hex}"
    content = {
        "carrier": "SEUR",
        "country": "ES",
        "coverage_rule": "SEUR covers the Zaragoza operational zone on weekdays.",
    }
    return MemoryProposalCreate(
        conversation_id=conversation_id or uuid4(),
        origin_interaction_id=origin,
        origin_message_sha256=hashlib.sha256(origin.encode()).hexdigest(),
        scope=MemoryScope.client,
        client_id=client_id,
        category=MemoryCategory.carrier_coverage,
        content=content,
        canonical_key=canonical_key_for(MemoryCategory.carrier_coverage, content),
    )


def _raw_proposal_decision(
    engine: Engine,
    *,
    client_id: str,
    actor: str,
    outcome: str,
    idempotency_key: str,
) -> tuple[str, str, dict[str, object], str]:
    proposal_id, decision_id = str(uuid4()), str(uuid4())
    content: dict[str, object] = {
        "carrier": "DHL",
        "country": "US",
        "coverage_rule": "DHL serves the QA operational zone.",
    }
    canonical = f"us:dhl:{uuid4().hex}"
    with engine.begin() as connection:
        statement, values = _conversation_insert(proposal_id, actor)
        connection.execute(statement, values)
        connection.execute(
            text(
                "INSERT INTO agent_memory_proposal "
                "(id,conversation_id,user_id,scope,client_id,category,content,canonical_key,"
                "origin_interaction_id,origin_message_sha256,status,valid_until,expires_at,"
                "created_at,resolved_at,version) VALUES "
                "(:id,:conversation,:actor,'client',:client,'carrier_coverage',CAST(:content AS jsonb),"
                ":canonical,:origin,:digest,:status,:valid,:expires,:created,:resolved,2)"
            ),
            {
                "id": proposal_id,
                "conversation": proposal_id,
                "actor": actor,
                "client": client_id,
                "content": json.dumps(content),
                "canonical": canonical,
                "origin": f"raw-{uuid4().hex}",
                "digest": "a" * 64,
                "status": outcome,
                "valid": NOW + timedelta(days=1),
                "expires": NOW + timedelta(hours=1),
                "created": NOW,
                "resolved": NOW + timedelta(minutes=1),
            },
        )
        connection.execute(
            text(
                "INSERT INTO agent_memory_decision "
                "(id,proposal_id,outcome,actor_user_id,idempotency_key,decided_at) "
                "VALUES (:id,:proposal,:outcome,:actor,:key,:decided)"
            ),
            {
                "id": decision_id,
                "proposal": proposal_id,
                "outcome": outcome,
                "actor": actor,
                "key": idempotency_key,
                "decided": NOW + timedelta(minutes=1),
            },
        )
    return proposal_id, decision_id, content, canonical


def _entry_insert(
    decision_id: str,
    client_id: str,
    proposal_content: dict[str, object],
    **changes,
):
    values = {
        "id": str(uuid4()),
        "scope": "client",
        "client": client_id,
        "category": "carrier_coverage",
        "content": json.dumps(proposal_content),
        "canonical": changes.pop("canonical", None),
        "decision": decision_id,
        "valid": NOW + timedelta(minutes=2),
        "expires": NOW + timedelta(days=1),
        "actor": "qa-actor",
    }
    values.update(changes)
    return text(
        "INSERT INTO agent_memory_entry "
        "(id,scope,client_id,category,content,canonical_key,verification_status,"
        "authorizing_decision_id,status,version,valid_from,expires_at,created_by_user_id,"
        "created_at,updated_at) VALUES "
        "(:id,:scope,:client,:category,CAST(:content AS jsonb),:canonical,'user_reported',"
        ":decision,'active',1,:valid,:expires,:actor,:valid,:valid)"
    ), values


def test_url_guard_rejects_database_url_remote_and_ambiguous_names():
    safe = "postgresql://qa:pw@127.0.0.1:55432/trackflow_mem092_test"
    normalized = _validated_test_url(safe, {})
    assert normalized.drivername == "postgresql+psycopg2"
    assert normalized.host == "127.0.0.1"
    assert normalized.database == "trackflow_mem092_test"
    with pytest.raises(ValueError, match="reuse DATABASE_URL"):
        _validated_test_url(safe, {"DATABASE_URL": safe})
    with pytest.raises(ValueError, match="target the DATABASE_URL database"):
        _validated_test_url(
            safe,
            {
                "DATABASE_URL": (
                    "postgresql://another-user:another-password@localhost:55432/"
                    "trackflow_mem092_test"
                )
            },
        )
    with pytest.raises(ValueError, match="loopback"):
        _validated_test_url(
            "postgresql://qa:pw@db.example.com:5432/trackflow_mem092_test", {}
        )
    with pytest.raises(ValueError, match="clearly named"):
        _validated_test_url("postgresql://qa:pw@127.0.0.1:55432/trackflow", {})


def test_entry_insert_can_express_an_intentional_authorization_mismatch():
    _, values = _entry_insert(
        str(uuid4()),
        str(uuid4()),
        {"carrier": "DHL", "country": "US", "coverage_rule": "QA zone"},
        category="b2b_report_preference",
        content=json.dumps({"preference_type": "format", "preference": "CSV"}),
        canonical="format",
    )
    assert values["category"] == "b2b_report_preference"
    assert json.loads(values["content"]) == {
        "preference_type": "format",
        "preference": "CSV",
    }


def test_raw_conversation_fixture_is_scoped_to_known_proposal_and_actor():
    conversation_id = str(uuid4())
    actor = f"actor-{uuid4().hex}"
    statement, values = _conversation_insert(conversation_id, actor)
    assert "agent_memory_conversation" in str(statement)
    assert values == {"id": conversation_id, "actor": actor, "created": NOW}


def test_catalog_uses_postgresql_uuid_jsonb_and_reviewed_triggers(postgres_engine: Engine):
    with postgres_engine.connect() as connection:
        types = dict(
            connection.execute(
                text(
                    "SELECT column_name,data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='agent_memory_proposal' "
                    "AND column_name IN ('id','conversation_id','content')"
                )
            ).all()
        )
        triggers = set(
            connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE tgrelid IN "
                    "('agent_memory_decision'::regclass,'agent_memory_entry'::regclass) "
                    "AND NOT tgisinternal"
                )
            ).scalars()
        )
    assert types == {"id": "uuid", "conversation_id": "uuid", "content": "jsonb"}
    assert triggers == {"agent_memory_decision_append_only", "agent_memory_entry_authorization"}


def test_proposal_approval_entry_cycle_and_tenant_isolation(postgres_engine: Engine):
    client_a, client_b = _add_client(postgres_engine, "tenant-a"), _add_client(
        postgres_engine, "tenant-b"
    )
    manager_a = _access(f"manager-a-{uuid4().hex}", Role.manager, client_a)
    manager_b = _access(f"manager-b-{uuid4().hex}", Role.manager, client_b)
    store = MemoryStore()
    with Session(postgres_engine) as session:
        proposal_a = store.create_proposal(
            session, _carrier_payload(client_a), manager_a, now=NOW
        )
        result_a = store.resolve_proposal(
            session, proposal_a.id, ProposalOutcome.approved, "approve", manager_a, now=NOW
        )
        proposal_b = store.create_proposal(
            session, _carrier_payload(client_b), manager_b, now=NOW
        )
        result_b = store.resolve_proposal(
            session, proposal_b.id, ProposalOutcome.approved, "approve", manager_b, now=NOW
        )
        assert result_a.entry is not None and result_b.entry is not None
        assert {entry.client_id for entry in store.list_active(session, manager_a, now=NOW)} == {
            client_a
        }
        assert {entry.client_id for entry in store.list_active(session, manager_b, now=NOW)} == {
            client_b
        }


def test_authorization_trigger_rejects_rejected_and_mismatched_decisions(
    postgres_engine: Engine,
):
    client_id = _add_client(postgres_engine, "authorization-trigger")
    _, rejected_id, rejected_content, rejected_canonical = _raw_proposal_decision(
        postgres_engine,
        client_id=client_id,
        actor=f"rejected-{uuid4().hex}",
        outcome="rejected",
        idempotency_key=f"reject-{uuid4().hex}",
    )
    statement, values = _entry_insert(
        rejected_id, client_id, rejected_content, canonical=rejected_canonical
    )
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(statement, values)

    _, approved_id, approved_content, _ = _raw_proposal_decision(
        postgres_engine,
        client_id=client_id,
        actor=f"approved-{uuid4().hex}",
        outcome="approved",
        idempotency_key=f"approve-{uuid4().hex}",
    )
    statement, values = _entry_insert(
        approved_id,
        client_id,
        approved_content,
        category="b2b_report_preference",
        content=json.dumps({"preference_type": "format", "preference": "CSV"}),
        canonical="format",
    )
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(statement, values)


def test_decision_is_append_only_and_idempotency_is_actor_scoped(postgres_engine: Engine):
    client_id = _add_client(postgres_engine, "audit")
    proposal_a, decision_a, _, _ = _raw_proposal_decision(
        postgres_engine,
        client_id=client_id,
        actor=f"actor-a-{uuid4().hex}",
        outcome="rejected",
        idempotency_key="shared-key",
    )
    actor_b = f"actor-b-{uuid4().hex}"
    _raw_proposal_decision(
        postgres_engine,
        client_id=client_id,
        actor=actor_b,
        outcome="rejected",
        idempotency_key="shared-key",
    )
    with pytest.raises(IntegrityError):
        _raw_proposal_decision(
            postgres_engine,
            client_id=client_id,
            actor=actor_b,
            outcome="rejected",
            idempotency_key="shared-key",
        )
    for mutation in (
        "UPDATE agent_memory_decision SET decided_at=now() WHERE id=:id",
        "DELETE FROM agent_memory_decision WHERE id=:id",
    ):
        with pytest.raises(DBAPIError):
            with postgres_engine.begin() as connection:
                connection.execute(text(mutation), {"id": decision_a})
    with postgres_engine.connect() as connection:
        assert connection.execute(
            text("SELECT proposal_id::text FROM agent_memory_decision WHERE id=:id"),
            {"id": decision_a},
        ).scalar_one() == proposal_a


def test_concurrent_resolution_has_one_winner(postgres_engine: Engine):
    client_id = _add_client(postgres_engine, "resolve-race")
    manager = _access(f"race-manager-{uuid4().hex}", Role.manager, client_id)
    with Session(postgres_engine) as session:
        proposal = MemoryStore().create_proposal(
            session, _carrier_payload(client_id), manager, now=NOW
        )
    barrier = Barrier(2)

    def resolve(key: str) -> str:
        with Session(postgres_engine) as session:
            barrier.wait()
            try:
                MemoryStore().resolve_proposal(
                    session, proposal.id, ProposalOutcome.rejected, key, manager, now=NOW
                )
                return "won"
            except MemoryConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(resolve, ("race-a", "race-b")))
    assert sorted(results) == ["conflict", "won"]
    with postgres_engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM agent_memory_decision WHERE proposal_id=:proposal"),
            {"proposal": str(proposal.id)},
        ).scalar_one() == 1


def test_concurrent_pending_proposal_has_one_winner(postgres_engine: Engine):
    client_id = _add_client(postgres_engine, "pending-race")
    manager = _access(f"pending-manager-{uuid4().hex}", Role.manager, client_id)
    conversation = uuid4()
    barrier = Barrier(2)

    def create(origin: str) -> str:
        with Session(postgres_engine) as session:
            barrier.wait()
            try:
                MemoryStore().create_proposal(
                    session,
                    _carrier_payload(
                        client_id, conversation_id=conversation, interaction=origin
                    ),
                    manager,
                    now=NOW,
                )
                return "won"
            except MemoryConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, ("pending-a", "pending-b")))
    assert sorted(results) == ["conflict", "won"]


def test_real_postgres_rolls_back_failed_consolidation(postgres_engine: Engine):
    client_id = _add_client(postgres_engine, "rollback")
    manager = _access(f"rollback-manager-{uuid4().hex}", Role.manager, client_id)
    store = MemoryStore()
    with Session(postgres_engine) as session:
        proposal = store.create_proposal(
            session, _carrier_payload(client_id), manager, now=NOW
        )

        def fail(*_args, **_kwargs):
            raise MemoryConflict("forced integration rollback")

        store._consolidate = fail  # type: ignore[method-assign]
        with pytest.raises(MemoryConflict, match="forced integration rollback"):
            store.resolve_proposal(
                session,
                proposal.id,
                ProposalOutcome.approved,
                "rollback",
                manager,
                now=NOW,
            )
        persisted = session.execute(
            select(memory_proposals).where(memory_proposals.c.id == str(proposal.id))
        ).mappings().one()
        assert persisted["status"] == ProposalStatus.pending.value
        assert session.execute(
            select(memory_decisions).where(memory_decisions.c.proposal_id == str(proposal.id))
        ).first() is None
        partial_entries = session.execute(
            text(
                "SELECT count(*) FROM agent_memory_entry entry "
                "JOIN agent_memory_decision decision "
                "ON decision.id=entry.authorizing_decision_id "
                "WHERE decision.proposal_id=:proposal"
            ),
            {"proposal": str(proposal.id)},
        ).scalar_one()
        assert partial_entries == 0


def test_operational_cleanup_invoker_expires_and_preserves_audit(
    postgres_engine: Engine,
):
    client_id = _add_client(postgres_engine, "cleanup-invoker")
    manager = _access(f"cleanup-manager-{uuid4().hex}", Role.manager, client_id)
    store = MemoryStore()
    old_pending_time = NOW - timedelta(days=2)
    old_entry_time = NOW - timedelta(days=40)
    with Session(postgres_engine) as session:
        pending = store.create_proposal(
            session,
            _carrier_payload(client_id, interaction=f"pending-{uuid4().hex}"),
            manager,
            now=old_pending_time,
        )
        approved_proposal = store.create_proposal(
            session,
            _carrier_payload(client_id, interaction=f"approved-{uuid4().hex}"),
            manager,
            now=old_entry_time,
        )
        approved = store.resolve_proposal(
            session,
            approved_proposal.id,
            ProposalOutcome.approved,
            f"approve-{uuid4().hex}",
            manager,
            now=old_entry_time + timedelta(minutes=1),
        )
        assert approved.entry is not None

    first = execute_cleanup(
        session_factory=lambda: Session(postgres_engine),
        now=NOW,
    )
    second = execute_cleanup(
        session_factory=lambda: Session(postgres_engine),
        now=NOW,
    )

    assert first[0] >= 1
    assert first[1] >= 1
    assert second == (0, 0)
    with postgres_engine.connect() as connection:
        assert connection.execute(
            select(memory_proposals.c.status).where(
                memory_proposals.c.id == str(pending.id)
            )
        ).scalar_one() == ProposalStatus.expired.value
        assert connection.execute(
            select(memory_decisions.c.outcome).where(
                memory_decisions.c.proposal_id == str(pending.id)
            )
        ).scalar_one() == ProposalOutcome.expired.value
        assert connection.execute(
            select(memory_entries.c.status).where(
                memory_entries.c.id == str(approved.entry.id)
            )
        ).scalar_one() == "expired"
