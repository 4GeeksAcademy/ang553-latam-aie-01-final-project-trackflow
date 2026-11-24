"""PostgreSQL integration for authenticated decision application.

The module deliberately reuses the proven Gate 2B.3A per-test schema fixture.
It remains skipped unless that suite's nine PostgreSQL guardrails are supplied
for a newly authorized disposable database.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys
from uuid import uuid4

from sqlalchemy import func, select
from sqlmodel import Session

from services.rfp_approval.application import (
    AuthenticatedDecisionApplicationService,
    DecisionApplicationServiceError,
)
from services.rfp_approval.identity import VerifiedAuthenticationContext
from services.rfp_approval.models import (
    rfp_approval_department_revisions,
    rfp_human_decisions,
    rfp_workflow_events,
)


_STORE_PG_PATH = Path(__file__).with_name(
    "test_rfp_approval_store_postgres_integration.py"
)
_STORE_PG_SPEC = importlib.util.spec_from_file_location(
    "trackflow_store_pg_fixture", _STORE_PG_PATH
)
assert _STORE_PG_SPEC is not None and _STORE_PG_SPEC.loader is not None
store_pg = importlib.util.module_from_spec(_STORE_PG_SPEC)
sys.modules[_STORE_PG_SPEC.name] = store_pg
_STORE_PG_SPEC.loader.exec_module(store_pg)


# Exposing the imported fixture keeps schema creation, migration ordering,
# exclusive search_path and targeted cleanup in one already validated place.
engine = store_pg.engine


def _authentication(scenario: store_pg.Scenario) -> VerifiedAuthenticationContext:
    identity = scenario.identity
    return VerifiedAuthenticationContext._from_verified_authenticator(
        environment=identity.environment,
        auth_source=identity.auth_source,
        issuer=identity.issuer,
        subject=identity.subject,
        trackflow_user_id=identity.trackflow_user_id,
        general_role=identity.general_role,
    )


def _event_count(engine, decision_id, event_type: str) -> int:
    with Session(engine) as session:
        return session.execute(
            select(func.count())
            .select_from(rfp_workflow_events)
            .where(
                rfp_workflow_events.c.decision_id == decision_id,
                rfp_workflow_events.c.event_type == event_type,
            )
        ).scalar_one()


def test_authenticated_request_recovers_queued_decision_and_replays_safely(
    engine,
) -> None:
    scenario = store_pg._scenario(engine)
    command, _ = store_pg._register(engine, scenario)
    service = AuthenticatedDecisionApplicationService(lambda: Session(engine))
    authentication = _authentication(scenario)

    applied = service.apply(
        decision_id=command.decision.decision_id,
        authentication=authentication,
        execution_id=uuid4(),
    )
    replay = service.apply(
        decision_id=command.decision.decision_id,
        authentication=authentication,
        execution_id=uuid4(),
    )

    assert applied.processing_status == "applied"
    assert applied.revision_status == "approved"
    assert applied.replayed is False
    assert replay.processing_status == "applied"
    assert replay.replayed is True
    assert _event_count(engine, command.decision.decision_id, "human_decision_claimed") == 1
    assert _event_count(engine, command.decision.decision_id, "human_decision_applied") == 1


def test_two_authenticated_requests_cannot_apply_one_decision_twice(engine) -> None:
    scenario = store_pg._scenario(engine)
    command, _ = store_pg._register(engine, scenario)
    authentication = _authentication(scenario)

    def apply_once():
        service = AuthenticatedDecisionApplicationService(lambda: Session(engine))
        try:
            return service.apply(
                decision_id=command.decision.decision_id,
                authentication=authentication,
                execution_id=uuid4(),
            )
        except DecisionApplicationServiceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: apply_once(), range(2)))

    service_errors = [item for item in outcomes if isinstance(item, str)]
    assert set(service_errors) <= {"decision_not_claimable"}
    assert any(not isinstance(item, str) for item in outcomes)
    with Session(engine) as session:
        decision_status = session.execute(
            select(rfp_human_decisions.c.processing_status).where(
                rfp_human_decisions.c.decision_id == command.decision.decision_id
            )
        ).scalar_one()
        revision = session.execute(
            select(
                rfp_approval_department_revisions.c.status,
                rfp_approval_department_revisions.c.applied_decision_id,
            ).where(
                rfp_approval_department_revisions.c.revision_id
                == scenario.revision_id
            )
        ).one()
    assert decision_status == "applied"
    assert revision.status == "approved"
    assert revision.applied_decision_id == command.decision.decision_id
    assert _event_count(engine, command.decision.decision_id, "human_decision_claimed") == 1
    assert _event_count(engine, command.decision.decision_id, "human_decision_applied") == 1
