"""Opt-in PostgreSQL evidence for SEC-114 authorized tracking noninterference."""

from __future__ import annotations

import os
import asyncio
from uuid import uuid4

import pytest
from langchain_core.messages import ToolMessage
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, create_engine, select

from services.api.access_control import AccessContext
from services.api.auth_models import Role
from services.api.inventory_models import CommercialOrder, SKU, StockExit
from services.api.inventory_service import lookup_authorized_tracking
from agents.customer_support.external_graph import ExternalSupportAgent
from agents.customer_support.mcp_tools import SupportMCPSettings, SupportMCPTools
from services.rag.resilience import QueryDeadline


CLIENT_A = "11111111-1111-4111-8111-111111111111"
CLIENT_B = "22222222-2222-4222-8222-222222222222"


def _test_database_url() -> str:
    value = os.getenv("TRACKFLOW_SEC114_TEST_DATABASE_URL")
    if not value:
        pytest.skip("TRACKFLOW_SEC114_TEST_DATABASE_URL is not configured")
    parsed = make_url(value)
    if parsed.drivername not in {"postgresql", "postgresql+psycopg2"}:
        raise RuntimeError("SEC-114 integration requires PostgreSQL")
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("SEC-114 PostgreSQL must be loopback-only")
    if parsed.database != "trackflow_sec114_test":
        raise RuntimeError("SEC-114 PostgreSQL database name is not authorized")
    return value


@pytest.fixture
def postgres_session():
    engine = create_engine(_test_database_url(), pool_pre_ping=True)
    tables = [SKU.__table__, CommercialOrder.__table__, StockExit.__table__]
    SQLModel.metadata.drop_all(engine, tables=list(reversed(tables)), checkfirst=True)
    SQLModel.metadata.create_all(engine, tables=tables)
    try:
        with Session(engine) as session:
            yield session
    finally:
        SQLModel.metadata.drop_all(
            engine, tables=list(reversed(tables)), checkfirst=True
        )
        engine.dispose()


def _access(client_id: str) -> AccessContext:
    return AccessContext(
        user_id=f"manager-{client_id[-4:]}",
        role=Role.manager,
        allowed_client_ids=frozenset({client_id}),
        sensitive_client_ids=frozenset(),
    )


def _add_line(
    session: Session,
    *,
    client_id: str,
    tracking_number: str,
    order: CommercialOrder | None = None,
) -> CommercialOrder:
    sku = SKU(
        name="Synthetic SEC-114 product",
        sku=f"SEC114-{uuid4().hex[:10]}",
        client_name="Synthetic client",
        client_id=client_id,
        category="electronics",
        warehouse="LA",
    )
    selected_order = order or CommercialOrder(
        order_id=uuid4(), client_id=client_id, warehouse="LA"
    )
    session.add(sku)
    if order is None:
        session.add(selected_order)
    session.commit()
    session.refresh(sku)
    session.refresh(selected_order)
    session.add(
        StockExit(
            sku_id=sku.id,
            quantity=1,
            exit_type="dispatch",
            tracking_number=tracking_number,
            warehouse="LA",
            user_uuid="synthetic-sec114",
            commercial_order_id=selected_order.id,
            dispatch_id=uuid4(),
        )
    )
    session.commit()
    return selected_order


def test_real_postgres_tracking_noninterference_and_authorized_duplicates(
    postgres_session: Session,
):
    session = postgres_session
    order_a = _add_line(
        session, client_id=CLIENT_A, tracking_number="45821"
    )
    baseline = lookup_authorized_tracking(
        session, tracking_number="45821", access=_access(CLIENT_A)
    )
    assert baseline.status.value == "found"
    assert baseline.dispatch_state.value == "recorded_dispatch"
    assert baseline.commercial_order_id == order_a.order_id

    order_b = _add_line(session, client_id=CLIENT_B, tracking_number="45821")
    assert lookup_authorized_tracking(
        session, tracking_number="45821", access=_access(CLIENT_A)
    ) == baseline
    _add_line(session, client_id=CLIENT_B, tracking_number="FOREIGN-ONLY")
    assert lookup_authorized_tracking(
        session, tracking_number="FOREIGN-ONLY", access=_access(CLIENT_A)
    ).status.value == "not_found"
    for foreign_line in session.exec(
        select(StockExit).where(StockExit.commercial_order_id == order_b.id)
    ).all():
        session.delete(foreign_line)
    session.commit()
    assert lookup_authorized_tracking(
        session, tracking_number="45821", access=_access(CLIENT_A)
    ) == baseline

    _add_line(
        session,
        client_id=CLIENT_A,
        tracking_number="45821",
        order=order_a,
    )
    assert lookup_authorized_tracking(
        session, tracking_number="45821", access=_access(CLIENT_A)
    ) == baseline

    _add_line(session, client_id=CLIENT_A, tracking_number="45821")
    conflict = lookup_authorized_tracking(
        session, tracking_number="45821", access=_access(CLIENT_A)
    )
    assert conflict.status.value == "unavailable"
    assert conflict.dispatch_state is None
    assert conflict.commercial_order_id is None


class _ServiceBackedTrackingTool:
    name = "get_authorized_tracking"

    def __init__(self, session: Session, access: AccessContext) -> None:
        self._session = session
        self._access = access

    async def ainvoke(self, call):
        tracking_number = call["args"]["input"]["tracking_number"]
        result = lookup_authorized_tracking(
            self._session,
            tracking_number=tracking_number,
            access=self._access,
        )
        return ToolMessage(
            content="authorized tracking projection",
            tool_call_id=call["id"],
            status="success",
            artifact={"structured_content": result.model_dump(mode="json")},
        )


class _TrackingOnlyClient:
    def __init__(self, tool: _ServiceBackedTrackingTool) -> None:
        self._tool = tool

    async def get_tools(self, *, server_name=None):
        assert server_name == "trackflow"
        return [
            _ForbiddenTool("get_incident"),
            _ForbiddenTool("get_inventory_product"),
            self._tool,
        ]


class _ForbiddenTool:
    def __init__(self, name: str) -> None:
        self.name = name

    async def ainvoke(self, call):
        raise AssertionError(f"unexpected MCP tool call: {self.name}")


def test_real_postgres_flows_through_mcp_adapter_and_support_graph(
    postgres_session: Session,
):
    order_a = _add_line(
        postgres_session,
        client_id=CLIENT_A,
        tracking_number="45821",
    )
    adapter = SupportMCPTools(
        SecretStr("synthetic-subject-token"),
        SupportMCPSettings("http://127.0.0.1:1/mcp", 2),
        client=_TrackingOnlyClient(
            _ServiceBackedTrackingTool(postgres_session, _access(CLIENT_A))
        ),
    )

    def forbidden_rag_factory():
        raise AssertionError("tracking-only routing must not invoke RAG")

    execution = asyncio.run(
        ExternalSupportAgent().execute(
            "Please check tracking #45821",
            session=postgres_session,
            access=_access(CLIENT_A),
            deadline=QueryDeadline(20),
            rag_factory=forbidden_rag_factory,
            tracking_tool=adapter.lookup_tracking,
        )
    )

    tracking = execution.result.tracking
    assert tracking is not None
    assert tracking.tracking_number == "45821"
    assert tracking.commercial_order_id == order_a.order_id
    assert tracking.dispatch_state.value == "recorded_dispatch"
    normalized_answer = execution.result.answer.casefold()
    assert "recorded dispatch" in normalized_answer
    assert "delivered" not in normalized_answer
    assert "in transit" not in normalized_answer
    assert "eta" not in normalized_answer
