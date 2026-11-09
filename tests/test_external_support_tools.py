"""Service-level security tests for external support read tools."""

from datetime import date

from sqlalchemy import create_engine, insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session

from agents.customer_support.tools import (
    InventoryLookupInput,
    TicketLookupInput,
    ToolStatus,
    apply_statement_timeout,
    lookup_inventory_product,
    lookup_ticket,
)
from services.api.access_control import AccessContext
from services.api.auth_models import Role
from services.api.inventory_models import SKU, StockEntry
from services.incidents.contracts import IncidentCreate, SourceChannel
from services.incidents.models import clients, operational_security_metadata
from services.incidents.service import create_incident
from services.rag.resilience import QueryDeadline


CLIENT_A = "11111111-1111-4111-8111-111111111111"
CLIENT_B = "22222222-2222-4222-8222-222222222222"


def access(client_id=CLIENT_A):
    return AccessContext(
        "manager-a",
        Role.manager,
        frozenset({client_id}),
        frozenset(),
    )


def build_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    operational_security_metadata.create_all(engine)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    session.execute(
        insert(clients),
        [
            {
                "client_id": CLIENT_A,
                "display_name": "Client A",
                "is_active": True,
                "created_by_user_id": "admin",
            },
            {
                "client_id": CLIENT_B,
                "display_name": "Client B",
                "is_active": True,
                "created_by_user_id": "admin",
            },
        ],
    )
    session.commit()
    return session


def create_ticket(session, client_id):
    return create_incident(
        session,
        IncidentCreate(
            client_id=client_id,
            incident_date=date(2026, 11, 9),
            country="US",
            customer_type="B2C",
            tracking_number="TRACK-0001",
            carrier="QA Carrier",
            category="DELAYED_DELIVERY",
            description="Package is delayed.",
            customer_email="private@example.com",
            source_channel=SourceChannel.backoffice,
            source_reference="private-reference",
        ),
        AccessContext("admin", Role.admin, frozenset(), frozenset(), True),
    )


def test_ticket_tool_uses_authorized_service_and_projects_only_safe_fields():
    session = build_session()
    try:
        incident = create_ticket(session, CLIENT_A)
        result = lookup_ticket(
            session,
            TicketLookupInput(incident.incident_id),
            access(),
            QueryDeadline(80),
        )
        assert result.status is ToolStatus.ok
        assert result.incident_id == incident.incident_id
        assert result.incident_status == "OPEN"
        assert set(result.__dataclass_fields__) == {
            "status",
            "incident_id",
            "incident_status",
            "category",
            "version",
            "updated_at",
        }
    finally:
        session.close()


def test_ticket_tool_hides_foreign_and_missing_records_identically():
    session = build_session()
    try:
        incident = create_ticket(session, CLIENT_B)
        foreign = lookup_ticket(
            session,
            TicketLookupInput(incident.incident_id),
            access(),
            QueryDeadline(80),
        )
        missing = lookup_ticket(
            session,
            TicketLookupInput("TRF-999999"),
            access(),
            QueryDeadline(80),
        )
        assert foreign == missing == type(foreign)(ToolStatus.not_found)
    finally:
        session.close()


def test_inventory_tool_distinguishes_zero_stock_from_hidden_or_missing():
    session = build_session()
    try:
        own = SKU(
            name="Own product",
            sku="OWN-1",
            client_id=CLIENT_A,
            client_name="Client A",
            category="fashion",
            warehouse="LA",
        )
        foreign = SKU(
            name="Foreign product",
            sku="FOREIGN-1",
            client_id=CLIENT_B,
            client_name="Client B",
            category="fashion",
            warehouse="LA",
        )
        session.add(own)
        session.add(foreign)
        session.commit()
        session.refresh(own)
        session.refresh(foreign)

        visible = lookup_inventory_product(
            session,
            InventoryLookupInput(own.id),
            access(),
            QueryDeadline(80),
        )
        hidden = lookup_inventory_product(
            session,
            InventoryLookupInput(foreign.id),
            access(),
            QueryDeadline(80),
        )
        missing = lookup_inventory_product(
            session,
            InventoryLookupInput(999999),
            access(),
            QueryDeadline(80),
        )
        assert visible.status is ToolStatus.ok
        assert visible.current_stock == 0
        assert hidden == missing == type(hidden)(ToolStatus.not_found)
        assert "client_id" not in visible.__dataclass_fields__
    finally:
        session.close()


def test_inventory_tool_preserves_existing_stock_calculation():
    session = build_session()
    try:
        product = SKU(
            name="Stocked product",
            sku="STOCK-1",
            client_id=CLIENT_A,
            client_name="Client A",
            category="fashion",
            warehouse="LA",
        )
        session.add(product)
        session.commit()
        session.refresh(product)
        session.add(
            StockEntry(
                sku_id=product.id,
                quantity=7,
                reference="PO-1",
                warehouse="LA",
                user_uuid="operator",
            )
        )
        session.commit()
        result = lookup_inventory_product(
            session,
            InventoryLookupInput(product.id),
            access(),
            QueryDeadline(80),
        )
        assert result.status is ToolStatus.ok
        assert result.current_stock == 7
    finally:
        session.close()


def test_statement_timeout_is_numeric_transaction_local_and_bounded():
    class Dialect:
        name = "postgresql"

    class Bind:
        dialect = Dialect()

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get_bind(self):
            return Bind()

        def execute(self, statement, parameters):
            self.calls.append((str(statement), parameters))

    session = FakeSession()
    milliseconds = apply_statement_timeout(session, QueryDeadline(80))
    assert milliseconds == 5000
    assert session.calls == [
        (
            "SELECT set_config('statement_timeout', :timeout, true)",
            {"timeout": "5000ms"},
        )
    ]

    class Clock:
        def __call__(self):
            return 10.0

    reserve_session = FakeSession()
    assert apply_statement_timeout(
        reserve_session,
        QueryDeadline(4, clock=Clock()),
    ) == 1000
    assert reserve_session.calls[0][1] == {"timeout": "1000ms"}


def test_inventory_input_rejects_non_positive_product_id():
    for value in (0, -1):
        try:
            InventoryLookupInput(value)
        except ValueError:
            continue
        raise AssertionError("non-positive product ID was accepted")


def test_expired_deadline_returns_timeout_without_querying_service():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    deadline = QueryDeadline(1, clock=clock)
    clock.value = 2.0
    session = build_session()
    try:
        assert lookup_ticket(
            session,
            TicketLookupInput("TRF-000001"),
            access(),
            deadline,
        ).status is ToolStatus.timeout
        assert lookup_inventory_product(
            session,
            InventoryLookupInput(1),
            access(),
            deadline,
        ).status is ToolStatus.timeout
    finally:
        session.close()


def test_postgresql_statement_cancellation_maps_to_ticket_timeout(monkeypatch):
    from agents.customer_support import tools

    class StatementCancelled(Exception):
        pgcode = "57014"

    def cancelled(*args, **kwargs):
        raise DBAPIError("SELECT", {}, StatementCancelled())

    monkeypatch.setattr(tools, "read_incident", cancelled)
    session = build_session()
    try:
        result = lookup_ticket(
            session,
            TicketLookupInput("TRF-000001"),
            access(),
            QueryDeadline(80),
        )
        assert result.status is ToolStatus.timeout
    finally:
        session.close()


def test_postgresql_statement_cancellation_maps_to_inventory_timeout(monkeypatch):
    from agents.customer_support import tools

    class StatementCancelled(Exception):
        sqlstate = "57014"

    def cancelled(*args, **kwargs):
        raise DBAPIError("SELECT", {}, StatementCancelled())

    monkeypatch.setattr(tools, "read_inventory_product", cancelled)
    session = build_session()
    try:
        result = lookup_inventory_product(
            session,
            InventoryLookupInput(1),
            access(),
            QueryDeadline(80),
        )
        assert result.status is ToolStatus.timeout
    finally:
        session.close()
