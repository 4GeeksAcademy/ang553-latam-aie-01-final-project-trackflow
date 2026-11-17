"""Tenant authorization gates for every inventory mutation route."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import insert
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from services.api.auth_models import Role, UserInDB
from services.api.access_control import AccessContext
from services.api.operational_oidc import get_operational_user
from services.api.database import get_db
from services.api.inventory_models import CommercialOrder, SKU, StockExit
from services.api.inventory_outbox import inventory_outbox_metadata
from services.api.cache import inventory_cache_key
from services.api.inventory_schemas import (
    CommercialOrderCreate,
    CommercialOrderDispatchAppend,
    CommercialOrderDispatchCreate,
    InventoryPhysicalCountCreate,
    SKUCreate,
    SKUThresholdUpdate,
    StockEntryCreate,
    StockExitCreate,
)
from services.api.routes.inventory import (
    append_order_dispatches,
    configure_product_threshold,
    confirm_physical_count,
    create_inbound_order,
    create_order_with_dispatches,
    create_outbound_order,
    create_product,
    get_authorized_tracking,
    router as inventory_router,
)
import services.api.inventory_service as inventory_service
from services.incidents.models import clients, operational_security_metadata, user_client_memberships

CLIENT_A = "11111111-1111-4111-8111-111111111111"
CLIENT_B = "22222222-2222-4222-8222-222222222222"


def _add_recorded_dispatch(
    session: Session,
    *,
    client_id: str,
    tracking_number: str,
    suffix: str,
):
    sku = SKU(
        name=f"Tracked {suffix}",
        sku=f"TRACK-{suffix}",
        client_id=client_id,
        client_name=f"Client {suffix}",
        category="electronics",
        warehouse="LA",
    )
    order = CommercialOrder(
        order_id=uuid4(),
        client_id=client_id,
        warehouse="LA",
    )
    session.add(sku)
    session.add(order)
    session.commit()
    session.refresh(sku)
    session.refresh(order)
    dispatch = StockExit(
        sku_id=sku.id,
        quantity=1,
        exit_type="dispatch",
        tracking_number=tracking_number,
        warehouse="LA",
        user_uuid="test-user",
        commercial_order_id=order.id,
        dispatch_id=uuid4(),
    )
    session.add(dispatch)
    session.commit()
    session.refresh(dispatch)
    return sku, order, dispatch


def _user(user_id: str, role: Role) -> UserInDB:
    return UserInDB(id=user_id, email=f"{user_id}@example.com", hashed_password="x", role=role)


def _request():
    return SimpleNamespace(state=SimpleNamespace(request_id=None))


@pytest.fixture
def secured_inventory(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    inventory_outbox_metadata.create_all(engine)
    operational_security_metadata.create_all(engine)
    with Session(engine) as session:
        session.execute(insert(clients), [
            {"client_id": CLIENT_A, "display_name": "Verified A", "is_active": True, "created_by_user_id": "admin"},
            {"client_id": CLIENT_B, "display_name": "Verified B", "is_active": True, "created_by_user_id": "admin"},
        ])
        session.execute(insert(user_client_memberships), [
            {"user_id": "manager-a", "client_id": CLIENT_A, "can_view_sensitive": False, "assigned_by_user_id": "admin"},
            {"user_id": "manager-b", "client_id": CLIENT_B, "can_view_sensitive": False, "assigned_by_user_id": "admin"},
            {"user_id": "user-a", "client_id": CLIENT_A, "can_view_sensitive": False, "assigned_by_user_id": "admin"},
        ])
        session.commit()
        monkeypatch.setattr("services.api.routes.inventory.capture_telemetry_events", lambda _events: None)
        monkeypatch.setattr("services.api.inventory_service._capture_after_commit", lambda _events: None)
        yield session


def _payloads(sku_id: int):
    order_id = uuid4()
    return {
        "threshold": SKUThresholdUpdate(minimum_stock_threshold=2),
        "count": InventoryPhysicalCountCreate(count_id=uuid4(), sku_id=sku_id, observed_quantity=10, warehouse="LA"),
        "inbound": StockEntryCreate(sku_id=sku_id, quantity=20, reference="PO-SEC", warehouse="LA"),
        "outbound": StockExitCreate(sku_id=sku_id, quantity=1, exit_type="dispatch", tracking_number="TRACK-SEC", warehouse="LA"),
        "order": CommercialOrderCreate(
            order_id=order_id,
            client_id=CLIENT_A,
            warehouse="LA",
            dispatches=[CommercialOrderDispatchCreate(dispatch_id=uuid4(), sku_id=sku_id, quantity=1, tracking_number="ORDER-1")],
        ),
        "append": CommercialOrderDispatchAppend(
            order_id=order_id,
            dispatches=[CommercialOrderDispatchCreate(dispatch_id=uuid4(), sku_id=sku_id, quantity=1, tracking_number="ORDER-2")],
        ),
    }


def test_manager_can_mutate_only_assigned_inventory_and_user_cannot(secured_inventory: Session):
    session = secured_inventory
    manager = _user("manager-a", Role.manager)
    user = _user("user-a", Role.user)
    created = create_product(
        _request(),
        SKUCreate(name="Secured", sku="SEC-A", client_id=CLIENT_A, client_name="Verified A", category="electronics", warehouse="LA"),
        session,
        manager,
    )
    sku_id = created.id
    payload = _payloads(sku_id)
    create_inbound_order(_request(), payload["inbound"], session, manager)
    configure_product_threshold(sku_id, payload["threshold"], session, manager)
    confirm_physical_count(_request(), payload["count"], session, manager)
    create_outbound_order(_request(), payload["outbound"], session, manager)
    order = create_order_with_dispatches(_request(), payload["order"], session, manager)
    append_order_dispatches(order.order_id, _request(), payload["append"], session, manager)

    denied_calls = (
        lambda: create_product(_request(), SKUCreate(name="Denied", sku="DENIED", client_id=CLIENT_A, client_name="Verified A", category="electronics", warehouse="LA"), session, user),
        lambda: configure_product_threshold(sku_id, payload["threshold"], session, user),
        lambda: confirm_physical_count(_request(), payload["count"].model_copy(update={"count_id": uuid4()}), session, user),
        lambda: create_inbound_order(_request(), payload["inbound"], session, user),
        lambda: create_outbound_order(_request(), payload["outbound"], session, user),
        lambda: create_order_with_dispatches(_request(), payload["order"].model_copy(update={"order_id": uuid4()}), session, user),
        lambda: append_order_dispatches(order.order_id, _request(), payload["append"].model_copy(update={"order_id": order.order_id}), session, user),
    )
    for call in denied_calls:
        with pytest.raises(HTTPException) as exc_info:
            call()
        assert exc_info.value.status_code == 403


def test_manager_foreign_and_missing_targets_share_404_for_all_mutations(
    secured_inventory: Session,
):
    session = secured_inventory
    foreign = SKU(name="Foreign", sku="SEC-B", client_id=CLIENT_B, client_name="Verified B", category="electronics", warehouse="LA")
    foreign_order = CommercialOrder(order_id=uuid4(), client_id=CLIENT_B, warehouse="LA")
    session.add(foreign); session.add(foreign_order); session.commit(); session.refresh(foreign); session.refresh(foreign_order)
    manager = _user("manager-a", Role.manager)
    missing_order_id = uuid4()
    sku_pairs = (
        lambda sku_id: configure_product_threshold(sku_id, SKUThresholdUpdate(minimum_stock_threshold=1), session, manager),
        lambda sku_id: confirm_physical_count(_request(), InventoryPhysicalCountCreate(count_id=uuid4(), sku_id=sku_id, observed_quantity=0, warehouse="LA"), session, manager),
        lambda sku_id: create_inbound_order(_request(), StockEntryCreate(sku_id=sku_id, quantity=1, reference="HIDDEN", warehouse="LA"), session, manager),
        lambda sku_id: create_outbound_order(_request(), StockExitCreate(sku_id=sku_id, quantity=1, exit_type="dispatch", tracking_number="HIDDEN", warehouse="LA"), session, manager),
    )
    for call in sku_pairs:
        results = []
        for sku_id in (foreign.id, 999999):
            with pytest.raises(HTTPException) as exc_info:
                call(sku_id)
            results.append((exc_info.value.status_code, exc_info.value.detail, exc_info.value.headers))
        assert results == [(404, "SKU not found.", None), (404, "SKU not found.", None)]

    client_results = []
    for client_id in (CLIENT_B, "99999999-9999-4999-8999-999999999999"):
        with pytest.raises(HTTPException) as exc_info:
            create_order_with_dispatches(
                _request(),
                CommercialOrderCreate(order_id=uuid4(), client_id=client_id, warehouse="LA", dispatches=[CommercialOrderDispatchCreate(dispatch_id=uuid4(), sku_id=foreign.id, quantity=1, tracking_number="HIDDEN")]),
                session,
                manager,
            )
        client_results.append((exc_info.value.status_code, exc_info.value.detail))
    assert client_results == [(404, "Client not found."), (404, "Client not found.")]

    product_results = []
    for client_id in (CLIENT_B, "99999999-9999-4999-8999-999999999999"):
        with pytest.raises(HTTPException) as exc_info:
            create_product(_request(), SKUCreate(name="Hidden", sku=f"H-{client_id}", client_id=client_id, client_name="Verified B", category="electronics", warehouse="LA"), session, manager)
        product_results.append((exc_info.value.status_code, exc_info.value.detail))
    assert product_results == [(404, "Client not found."), (404, "Client not found.")]

    order_results = []
    for order_id in (foreign_order.order_id, missing_order_id):
        with pytest.raises(HTTPException) as exc_info:
            append_order_dispatches(order_id, _request(), CommercialOrderDispatchAppend(order_id=order_id, dispatches=[CommercialOrderDispatchCreate(dispatch_id=uuid4(), sku_id=foreign.id, quantity=1, tracking_number="HIDDEN")]), session, manager)
        order_results.append((exc_info.value.status_code, exc_info.value.detail))
    assert order_results == [(404, "Commercial order not found."), (404, "Commercial order not found.")]


def test_unregistered_and_inactive_clients_fail_closed(secured_inventory: Session):
    session = secured_inventory
    inactive_id = "33333333-3333-4333-8333-333333333333"
    session.execute(insert(clients).values(client_id=inactive_id, display_name="Inactive", is_active=False, created_by_user_id="admin")); session.commit()
    admin = _user("admin", Role.admin)
    for client_id, name in ((inactive_id, "Inactive"), ("99999999-9999-4999-8999-999999999999", "Missing")):
        with pytest.raises(HTTPException) as exc_info:
            create_product(_request(), SKUCreate(name="Closed", sku=f"C-{client_id}", client_id=client_id, client_name=name, category="electronics", warehouse="LA"), session, admin)
        assert (exc_info.value.status_code, exc_info.value.detail) == (404, "Client not found.")


def test_access_context_user_never_views_sensitive_data():
    context = AccessContext(
        user_id="user-a",
        role=Role.user,
        allowed_client_ids=frozenset({CLIENT_A}),
        sensitive_client_ids=frozenset({CLIENT_A}),
    )
    assert context.can_view_sensitive(CLIENT_A) is False


def test_tracking_lookup_is_tenant_first_and_noninterfering(
    secured_inventory: Session,
):
    session = secured_inventory
    _sku_a, order_a, _dispatch_a = _add_recorded_dispatch(
        session,
        client_id=CLIENT_A,
        tracking_number="45821",
        suffix="A",
    )
    access_a = AccessContext(
        user_id="manager-a",
        role=Role.manager,
        allowed_client_ids=frozenset({CLIENT_A}),
        sensitive_client_ids=frozenset(),
    )
    baseline = inventory_service.lookup_authorized_tracking(
        session,
        tracking_number="45821",
        access=access_a,
    )
    assert baseline.status.value == "found"
    assert baseline.dispatch_state.value == "recorded_dispatch"
    assert baseline.commercial_order_id == order_a.order_id

    _sku_b, _order_b, dispatch_b = _add_recorded_dispatch(
        session,
        client_id=CLIENT_B,
        tracking_number="45821",
        suffix="B",
    )
    with_foreign_duplicate = inventory_service.lookup_authorized_tracking(
        session,
        tracking_number="45821",
        access=access_a,
    )
    assert with_foreign_duplicate == baseline

    session.delete(dispatch_b)
    session.commit()
    after_foreign_removal = inventory_service.lookup_authorized_tracking(
        session,
        tracking_number="45821",
        access=access_a,
    )
    assert after_foreign_removal == baseline


def test_foreign_only_tracking_is_indistinguishable_from_missing(
    secured_inventory: Session,
):
    session = secured_inventory
    _add_recorded_dispatch(
        session,
        client_id=CLIENT_B,
        tracking_number="FOREIGN-ONLY",
        suffix="FOREIGN",
    )
    access_a = AccessContext(
        user_id="manager-a",
        role=Role.manager,
        allowed_client_ids=frozenset({CLIENT_A}),
        sensitive_client_ids=frozenset(),
    )
    foreign = inventory_service.lookup_authorized_tracking(
        session,
        tracking_number="FOREIGN-ONLY",
        access=access_a,
    )
    missing = inventory_service.lookup_authorized_tracking(
        session,
        tracking_number="MISSING",
        access=access_a,
    )
    assert foreign.status.value == missing.status.value == "not_found"
    assert foreign.dispatch_state is missing.dispatch_state is None
    assert foreign.commercial_order_id is missing.commercial_order_id is None


def test_incompatible_authorized_tracking_duplicates_fail_closed(
    secured_inventory: Session,
):
    session = secured_inventory
    _add_recorded_dispatch(
        session,
        client_id=CLIENT_A,
        tracking_number="DUPLICATE-A",
        suffix="A1",
    )
    _add_recorded_dispatch(
        session,
        client_id=CLIENT_A,
        tracking_number="DUPLICATE-A",
        suffix="A2",
    )
    access_a = AccessContext(
        user_id="manager-a",
        role=Role.manager,
        allowed_client_ids=frozenset({CLIENT_A}),
        sensitive_client_ids=frozenset(),
    )
    result = inventory_service.lookup_authorized_tracking(
        session,
        tracking_number="DUPLICATE-A",
        access=access_a,
    )
    assert result.status.value == "unavailable"
    assert result.dispatch_state is None
    assert result.commercial_order_id is None


def test_multiple_authorized_lines_for_same_order_are_aggregated(
    secured_inventory: Session,
):
    session = secured_inventory
    _sku, order, _dispatch = _add_recorded_dispatch(
        session,
        client_id=CLIENT_A,
        tracking_number="MULTILINE-A",
        suffix="LINE1",
    )
    second_sku = SKU(
        name="Second line",
        sku="SEC-A-LINE2",
        client_id=CLIENT_A,
        client_name="Verified A",
        category="electronics",
        warehouse="LA",
    )
    session.add(second_sku)
    session.commit()
    session.refresh(second_sku)
    session.add(
        StockExit(
            sku_id=second_sku.id,
            quantity=1,
            exit_type="dispatch",
            tracking_number="MULTILINE-A",
            warehouse="LA",
            user_uuid="manager-a",
            commercial_order_id=order.id,
            dispatch_id=uuid4(),
        )
    )
    session.commit()
    result = inventory_service.lookup_authorized_tracking(
        session,
        tracking_number="MULTILINE-A",
        access=AccessContext(
            user_id="manager-a",
            role=Role.manager,
            allowed_client_ids=frozenset({CLIENT_A}),
            sensitive_client_ids=frozenset(),
        ),
    )
    assert result.status.value == "found"
    assert result.commercial_order_id == order.order_id


def test_tracking_endpoint_exposes_no_tenant_identifier(
    secured_inventory: Session,
):
    session = secured_inventory
    _add_recorded_dispatch(
        session,
        client_id=CLIENT_A,
        tracking_number="VISIBLE-A",
        suffix="ENDPOINT",
    )
    response = get_authorized_tracking(
        tracking_number="VISIBLE-A",
        session=session,
        current_user=_user("manager-a", Role.manager),
    )
    assert response.status.value == "found"
    assert "client_id" not in response.model_dump()


def test_cache_database_tokens_are_not_reused():
    first = create_engine("sqlite://")
    second = create_engine("sqlite://")
    with Session(first) as first_session, Session(second) as second_session:
        assert inventory_cache_key("products", first_session) != inventory_cache_key(
            "products", second_session
        )


def test_service_write_facades_require_access_context_and_deny_user(
    secured_inventory: Session,
):
    session = secured_inventory
    sku = SKU(name="Direct", sku="DIRECT-A", client_id=CLIENT_A, client_name="Verified A", category="electronics", warehouse="LA")
    session.add(sku); session.commit(); session.refresh(sku)
    denied = AccessContext(
        user_id="user-a",
        role=Role.user,
        allowed_client_ids=frozenset({CLIENT_A}),
        sensitive_client_ids=frozenset(),
    )
    order_id = uuid4()
    calls = (
        lambda: inventory_service.create_stock_entry(session, StockEntryCreate(sku_id=sku.id, quantity=1, reference="D", warehouse="LA"), "user-a", denied),
        lambda: inventory_service.create_stock_exit(session, StockExitCreate(sku_id=sku.id, quantity=1, exit_type="dispatch", tracking_number="D", warehouse="LA"), "user-a", denied),
        lambda: inventory_service.update_sku_threshold(session, sku_id=sku.id, minimum_stock_threshold=1, access=denied),
        lambda: inventory_service.confirm_inventory_physical_count(session, InventoryPhysicalCountCreate(count_id=uuid4(), sku_id=sku.id, observed_quantity=0, warehouse="LA"), user_uuid="user-a", access=denied),
        lambda: inventory_service.create_commercial_order(session, CommercialOrderCreate(order_id=order_id, client_id=CLIENT_A, warehouse="LA", dispatches=[CommercialOrderDispatchCreate(dispatch_id=uuid4(), sku_id=sku.id, quantity=1, tracking_number="D")]), user_uuid="user-a", access=denied),
        lambda: inventory_service.append_commercial_order_dispatches(session, CommercialOrderDispatchAppend(order_id=order_id, dispatches=[CommercialOrderDispatchCreate(dispatch_id=uuid4(), sku_id=sku.id, quantity=1, tracking_number="D")]), user_uuid="user-a", access=denied),
    )
    for call in calls:
        with pytest.raises(HTTPException) as exc_info:
            call()
        assert exc_info.value.status_code == 403


def test_http_all_inventory_mutations_reject_role_user(secured_inventory: Session):
    api = FastAPI(); api.include_router(inventory_router)
    api.dependency_overrides[get_db] = lambda: secured_inventory
    api.dependency_overrides[get_operational_user] = lambda: _user("user-a", Role.user)
    append_order_id = uuid4()
    requests = [
        ("POST", "/inventory/products", {"name":"X","sku":"X","client_id":CLIENT_A,"client_name":"Verified A","category":"electronics","warehouse":"LA"}),
        ("PATCH", "/inventory/products/999/threshold", {"minimum_stock_threshold":1}),
        ("POST", "/inventory/physical-counts", {"count_id":str(uuid4()),"sku_id":999,"observed_quantity":0,"warehouse":"LA"}),
        ("POST", "/inventory/orders/inbound", {"sku_id":999,"quantity":1,"reference":"X","warehouse":"LA"}),
        ("POST", "/inventory/orders/outbound", {"sku_id":999,"quantity":1,"exit_type":"dispatch","tracking_number":"X","warehouse":"LA"}),
        ("POST", "/inventory/commercial-orders", {"order_id":str(uuid4()),"client_id":CLIENT_A,"warehouse":"LA","dispatches":[{"dispatch_id":str(uuid4()),"sku_id":999,"quantity":1,"tracking_number":"X"}]}),
        ("POST", f"/inventory/commercial-orders/{append_order_id}/dispatches", {"order_id":str(append_order_id),"dispatches":[{"dispatch_id":str(uuid4()),"sku_id":999,"quantity":1,"tracking_number":"X"}]}),
    ]

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            for method, path, body in requests:
                response = await client.request(method, path, json=body)
                assert response.status_code == 403

    asyncio.run(scenario())
