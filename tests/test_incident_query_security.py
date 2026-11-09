"""Deterministic acceptance tests for operational incidents and tenant access."""

import asyncio
from datetime import date
from pathlib import Path

import pytest
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session

from services.api.access_control import AccessContext, build_access_context
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.api.database import get_db
from services.api.cache import orders_cache, products_cache
from services.api.inventory_models import SKU, StockEntry
from services.api.routes.inventory import get_product, list_orders_endpoint, list_products
from services.incidents.contracts import IncidentCreate, IncidentUpdate
from services.incidents.contracts import ClientCreate, MembershipCreate
from services.api.routes.access import create_client, create_membership
from services.incidents.models import clients, operational_security_metadata, user_client_memberships
from services.incidents.service import (
    IncidentConflict,
    IncidentForbidden,
    IncidentNotFound,
    IncidentValidationError,
    change_incident_status,
    create_incident,
    read_incident,
)
from services.incidents.router import router as incident_router
from services.incidents.router import post_incident

CLIENT_A = "11111111-1111-4111-8111-111111111111"
CLIENT_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    operational_security_metadata.create_all(engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.execute(insert(clients), [
            {"client_id": CLIENT_A, "display_name": "Verified A", "is_active": True, "created_by_user_id": "admin"},
            {"client_id": CLIENT_B, "display_name": "Verified B", "is_active": True, "created_by_user_id": "admin"},
        ])
        db.execute(insert(user_client_memberships), [
            {"user_id": "manager-a", "client_id": CLIENT_A, "can_view_sensitive": True, "assigned_by_user_id": "admin"},
            {"user_id": "user-a", "client_id": CLIENT_A, "can_view_sensitive": True, "assigned_by_user_id": "admin"},
            {"user_id": "manager-b", "client_id": CLIENT_B, "can_view_sensitive": False, "assigned_by_user_id": "admin"},
        ])
        db.commit()
        yield db
    products_cache.clear()
    orders_cache.clear()


def access(user_id: str, role: Role, *clients_: str, sensitive: bool = False) -> AccessContext:
    return AccessContext(
        user_id=user_id,
        role=role,
        allowed_client_ids=frozenset(clients_),
        sensitive_client_ids=frozenset(clients_ if sensitive else ()),
        global_access=role is Role.admin,
    )


def payload(
    client_id: str = CLIENT_A,
    email: str | None = "customer@example.com",
    carrier: str = "UPS",
) -> IncidentCreate:
    return IncidentCreate(
        client_id=client_id,
        incident_date=date(2026, 11, 8),
        country="US",
        customer_type="B2C",
        tracking_number="TRACK-12345",
        carrier=carrier,
        category="DELAYED_DELIVERY",
        description="Package has not arrived.",
        customer_email=email,
        source_channel="backoffice",
    )


def test_sequential_id_history_and_optimistic_version(session: Session):
    manager = access("manager-a", Role.manager, CLIENT_A, sensitive=True)
    first = create_incident(session, payload(), manager)
    second = create_incident(session, payload(email=None), manager)
    assert (first.incident_id, second.incident_id) == ("TRF-000001", "TRF-000002")
    assert first.history[0].event_type == "CREATED"

    closed = change_incident_status(
        session,
        first.incident_id,
        IncidentUpdate(expected_version=1, status="CLOSED", satisfaction_score=5),
        manager,
    )
    assert closed.version == 2
    assert [item.new_status for item in closed.history] == ["OPEN", "CLOSED"]
    with pytest.raises(IncidentConflict):
        change_incident_status(
            session,
            first.incident_id,
            IncidentUpdate(expected_version=1, status="OPEN", reason="stale"),
            manager,
        )
    reopened = change_incident_status(
        session,
        first.incident_id,
        IncidentUpdate(expected_version=2, status="OPEN", reason="Carrier resumed search"),
        manager,
    )
    assert reopened.version == 3
    discarded = change_incident_status(
        session,
        first.incident_id,
        IncidentUpdate(expected_version=3, status="DISCARDED", reason="Duplicate report"),
        manager,
    )
    assert discarded.version == 4
    assert [item.new_status for item in discarded.history] == [
        "OPEN", "CLOSED", "OPEN", "DISCARDED"
    ]


def test_valid_operational_carrier_and_omitted_email_create_open_incident(
    session: Session,
):
    manager_access = access("manager-a", Role.manager, CLIENT_A)
    created = create_incident(
        session,
        payload(email=None, carrier="QA Test Carrier"),
        manager_access,
    )
    assert created.incident_id == "TRF-000001"
    assert created.status == "OPEN"
    assert created.customer_email is None
    assert [(item.version, item.event_type, item.new_status) for item in created.history] == [
        (1, "CREATED", "OPEN")
    ]


def test_router_accepts_valid_operational_payload_without_email(session: Session):
    manager = UserInDB(
        id="manager-a",
        email="manager@example.com",
        hashed_password="x",
        role=Role.manager,
    )
    created = post_incident(
        payload(email=None, carrier="QA Test Carrier"), session, manager
    )
    assert created.status == "OPEN"
    assert created.incident_id == "TRF-000001"


@pytest.mark.parametrize(
    "changes",
    [
        {"country": "MX"},
        {"category": "UNKNOWN"},
        {"customer_email": "not-an-email"},
        {"tracking_number": "SHORT"},
        {"description": "bad"},
    ],
)
def test_operational_contract_rejects_genuinely_invalid_payloads(changes):
    valid = payload().model_dump()
    valid.update(changes)
    with pytest.raises(ValidationError):
        IncidentCreate(**valid)


def test_invalid_state_transitions_are_rejected(session: Session):
    manager = access("manager-a", Role.manager, CLIENT_A)
    created = create_incident(session, payload(), manager)
    with pytest.raises(IncidentValidationError):
        change_incident_status(
            session,
            created.incident_id,
            IncidentUpdate(expected_version=1, status="OPEN"),
            manager,
        )
    closed = change_incident_status(
        session,
        created.incident_id,
        IncidentUpdate(expected_version=1, status="CLOSED", satisfaction_score=4),
        manager,
    )
    with pytest.raises(IncidentValidationError):
        change_incident_status(
            session,
            closed.incident_id,
            IncidentUpdate(expected_version=2, status="OPEN"),
            manager,
        )


def test_cross_tenant_is_hidden_and_user_is_read_only(session: Session):
    manager = access("manager-a", Role.manager, CLIENT_A)
    incident = create_incident(session, payload(), manager)
    with pytest.raises(IncidentNotFound):
        read_incident(session, incident.incident_id, access("manager-b", Role.manager, CLIENT_B))
    with pytest.raises(IncidentForbidden):
        create_incident(session, payload(), access("user-a", Role.user, CLIENT_A))


def test_sensitive_email_is_redacted_without_permission(session: Session):
    incident = create_incident(session, payload(), access("manager-a", Role.manager, CLIENT_A, sensitive=True))
    redacted = read_incident(session, incident.incident_id, access("user-a", Role.user, CLIENT_A))
    assert redacted.customer_email is None
    assert incident.customer_email == "customer@example.com"
    dumped = redacted.model_dump()
    assert "source_reference" not in dumped
    assert "created_by_user_id" not in dumped


def test_access_context_comes_from_persisted_membership(session: Session):
    context = build_access_context(
        session,
        UserInDB(id="manager-a", email="manager@example.com", hashed_password="x", role=Role.manager),
    )
    assert context.allowed_client_ids == frozenset({CLIENT_A})
    assert context.can_view_sensitive(CLIENT_A)


def test_ddl_is_explicit_and_not_part_of_sqlmodel_startup():
    assert "incident" not in SQLModel.metadata.tables
    incident_id = operational_security_metadata.tables["incident"].c.incident_id
    assert incident_id.nullable is False
    assert incident_id.computed is not None

    repo_root = Path(__file__).resolve().parents[1]
    ddl = (repo_root / "infra/sql/incident_query_security.sql").read_text(
        encoding="utf-8"
    )
    preflight = (
        repo_root / "infra/sql/incident_query_security_preflight_v1.sql"
    ).read_text(encoding="utf-8")
    assert "SEQUENCE NAME public.incident_id_seq" in ddl
    assert "SEQUENCE NAME public.incident_history_id_seq" in ddl
    assert "incident_id varchar(10) GENERATED ALWAYS" in ddl
    assert "CREATE FUNCTION public.reject_incident_history_mutation" in ddl
    assert "ON public.incident_history" in ddl
    assert "public.incident_history_id_seq" in preflight
    assert "BEGIN TRANSACTION READ ONLY" in preflight


def test_inventory_reads_are_tenant_scoped_and_hide_actor(session: Session):
    sku_a = SKU(name="A", sku="A-1", client_id=CLIENT_A, client_name="A", category="electronics", warehouse="LA")
    sku_b = SKU(name="B", sku="B-1", client_id=CLIENT_B, client_name="B", category="electronics", warehouse="LA")
    session.add(sku_a); session.add(sku_b); session.commit(); session.refresh(sku_a); session.refresh(sku_b)
    session.add(StockEntry(sku_id=sku_a.id, quantity=4, reference="in-a", warehouse="LA", user_uuid="private-actor"))
    session.add(StockEntry(sku_id=sku_b.id, quantity=9, reference="in-b", warehouse="LA", user_uuid="other-actor"))
    session.commit()
    manager = UserInDB(id="manager-a", email="manager@example.com", hashed_password="x", role=Role.manager)

    products = list_products(session=session, current_user=manager)
    assert [(item.sku, item.current_stock) for item in products] == [("A-1", 4)]
    assert get_product(id=sku_a.id, session=session, current_user=manager).sku == "A-1"
    with pytest.raises(Exception) as exc_info:
        get_product(id=sku_b.id, session=session, current_user=manager)
    assert getattr(exc_info.value, "status_code", None) == 404
    orders = list_orders_endpoint(session=session, current_user=manager)
    assert len(orders) == 1
    assert "user_uuid" not in orders[0].model_dump()

    manager_b = UserInDB(id="manager-b", email="manager-b@example.com", hashed_password="x", role=Role.manager)
    assert [item.sku for item in list_products(session=session, current_user=manager_b)] == ["B-1"]
    unassigned = UserInDB(id="no-clients", email="none@example.com", hashed_password="x", role=Role.user)
    assert list_products(session=session, current_user=unassigned) == []
    admin = UserInDB(id="admin", email="admin@example.com", hashed_password="x", role=Role.admin)
    assert {item.sku for item in list_products(session=session, current_user=admin)} == {"A-1", "B-1"}


def test_admin_registers_verified_client_and_existing_tinydb_user(session: Session, monkeypatch):
    admin = UserInDB(id="admin", email="admin@example.com", hashed_password="x", role=Role.admin)
    client_id = "33333333-3333-4333-8333-333333333333"
    created = create_client(
        ClientCreate(client_id=client_id, display_name="Verified C"),
        session,
        admin,
    )
    assert created.client_id == client_id

    from services.api.routes import access as access_routes
    monkeypatch.setattr(
        access_routes,
        "get_user_by_id",
        lambda user_id: type(
            "VerifiedUser", (), {"is_active": True, "role": Role.manager}
        )()
        if user_id == "known-user"
        else None,
    )
    membership = create_membership(
        MembershipCreate(user_id="known-user", client_id=client_id, can_view_sensitive=False),
        session,
        admin,
    )
    assert membership.client_id == client_id


def test_sensitive_membership_is_rejected_for_role_user(session: Session, monkeypatch):
    from services.api.routes import access as access_routes

    monkeypatch.setattr(
        access_routes,
        "get_user_by_id",
        lambda _user_id: type(
            "VerifiedUser", (), {"is_active": True, "role": Role.user}
        )(),
    )
    with pytest.raises(HTTPException) as exc_info:
        create_membership(
            MembershipCreate(
                user_id="user-sensitive",
                client_id=CLIENT_A,
                can_view_sensitive=True,
            ),
            session,
            UserInDB(
                id="admin",
                email="admin@example.com",
                hashed_password="x",
                role=Role.admin,
            ),
        )
    assert exc_info.value.status_code == 422


def test_legacy_incident_routes_precede_dynamic_operational_route():
    from services.api.main import app

    analyze_index = next(i for i, route in enumerate(app.routes) if getattr(route, "path", None) == "/api/incidents/analyze")
    export_index = next(i for i, route in enumerate(app.routes) if getattr(route, "path", None) == "/api/incidents/results/export")
    dynamic_index = next(
        i
        for i, route in enumerate(app.routes)
        if any(getattr(child, "path", None) == "/api/incidents/{incident_id}" for child in getattr(getattr(route, "original_router", None), "routes", ()))
    )
    assert analyze_index < dynamic_index
    assert export_index < dynamic_index


def test_backoffice_proxies_operational_access_routes():
    config = (
        Path(__file__).resolve().parents[1]
        / "uis"
        / "backoffice"
        / "next.config.ts"
    ).read_text(encoding="utf-8")
    assert 'source: "/api/access/:path*"' in config
    assert 'destination: `${backendUrl}/api/access/:path*`' in config


def test_backoffice_incident_telemetry_uses_sanitized_path_template():
    source = (
        Path(__file__).resolve().parents[1]
        / "uis"
        / "backoffice"
        / "lib"
        / "incidentsApi.ts"
    ).read_text(encoding="utf-8")
    assert '"/api/incidents/{incident_id}"' in source
    assert "{ pathTemplate: path }" not in source


def test_operational_http_crud_auth_and_hidden_cross_tenant(session: Session):
    api = FastAPI()
    api.include_router(incident_router)
    current = {"user": UserInDB(id="manager-a", email="manager@example.com", hashed_password="x", role=Role.manager)}

    def db_override():
        yield session

    def auth_override():
        if current["user"] is None:
            raise HTTPException(status_code=401, detail="Not authenticated.")
        return current["user"]

    api.dependency_overrides[get_db] = db_override
    api.dependency_overrides[get_current_user] = auth_override

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            create_body = payload().model_dump(mode="json")
            response = await client.post("/api/incidents", json=create_body)
            assert response.status_code == 201
            incident_id = response.json()["incident_id"]
            assert (await client.get(f"/api/incidents/{incident_id}")).status_code == 200
            closed = await client.patch(
                f"/api/incidents/{incident_id}",
                json={"expected_version": 1, "status": "CLOSED", "satisfaction_score": 4},
            )
            assert closed.status_code == 200 and closed.json()["version"] == 2

            current["user"] = UserInDB(id="user-a", email="user@example.com", hashed_password="x", role=Role.user)
            assert (await client.post("/api/incidents", json=create_body)).status_code == 403
            current["user"] = UserInDB(id="manager-b", email="manager-b@example.com", hashed_password="x", role=Role.manager)
            hidden = await client.get(f"/api/incidents/{incident_id}")
            absent = await client.get("/api/incidents/TRF-999999")
            assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json())
            assert hidden.status_code == 404
            current["user"] = None
            assert (await client.get(f"/api/incidents/{incident_id}")).status_code == 401

    asyncio.run(scenario())
