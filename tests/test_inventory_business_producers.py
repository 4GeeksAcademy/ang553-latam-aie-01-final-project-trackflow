"""Focused transactional tests for authentic inventory business producers."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

import services.api.inventory_service as inventory_service
from services.api.inventory_clients import CLIENT_IDS_BY_NAME
from services.api.access_control import AccessContext
from services.api.auth_models import Role
from services.api.inventory_models import (
    CommercialOrder,
    InventoryPhysicalCount,
    InventoryThresholdEvaluation,
    SKU,
    StockEntry,
    StockExit,
)
from services.api.inventory_outbox import (
    inventory_event_outbox,
    inventory_outbox_metadata,
)
from services.api.inventory_schemas import (
    CommercialOrderCreate,
    CommercialOrderDispatchAppend,
    InventoryPhysicalCountCreate,
    StockEntryCreate,
    StockExitCreate,
)
from services.incidents.models import clients, operational_security_metadata


CLIENT_ID = CLIENT_IDS_BY_NAME["PureStep Footwear"]
_ADMIN_ACCESS = AccessContext(
    user_id="producer-test-admin",
    role=Role.admin,
    allowed_client_ids=frozenset(),
    sensitive_client_ids=frozenset(),
    global_access=True,
)
_ORIGINAL_SERVICE_WRITES = {
    name: getattr(inventory_service, name)
    for name in (
        "create_stock_entry",
        "create_stock_exit",
        "update_sku_threshold",
        "confirm_inventory_physical_count",
        "create_commercial_order",
        "append_commercial_order_dispatches",
    )
}


@pytest.fixture(autouse=True)
def authorized_service_writes(monkeypatch):
    for name, original in _ORIGINAL_SERVICE_WRITES.items():
        def wrapper(*args, _original=original, **kwargs):
            kwargs["access"] = _ADMIN_ACCESS
            return _original(*args, **kwargs)
        monkeypatch.setattr(inventory_service, name, wrapper)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    inventory_outbox_metadata.create_all(engine)
    operational_security_metadata.create_all(engine)
    with Session(engine) as value:
        value.execute(insert(clients).values(
            client_id=CLIENT_ID,
            display_name="PureStep Footwear",
            is_active=True,
            created_by_user_id="producer-test-admin",
        ))
        value.commit()
        yield value


def _sku(session: Session, code: str, *, threshold=None) -> SKU:
    sku = SKU(
        name=code,
        sku=code,
        client_id=CLIENT_ID,
        client_name="PureStep Footwear",
        category="fashion",
        warehouse="LA",
        minimum_stock_threshold=threshold,
    )
    session.add(sku)
    session.commit()
    session.refresh(sku)
    return sku


def _outbox(session: Session, event_type: str) -> list[dict]:
    return [
        dict(row)
        for row in session.execute(
            select(inventory_event_outbox)
            .where(inventory_event_outbox.c.event_type == event_type)
            .order_by(inventory_event_outbox.c.occurred_at)
        ).mappings()
    ]


def _entry(session: Session, sku: SKU, quantity: int):
    return inventory_service.create_stock_entry(
        session,
        StockEntryCreate(
            sku_id=sku.id,
            quantity=quantity,
            reference=f"IN-{uuid4()}",
            warehouse="LA",
        ),
        user_uuid="user",
    )


def test_standalone_dispatch_retry_is_opt_in_and_replays_before_stock_check(
    session,
):
    sku = _sku(session, "STANDALONE-IDEMPOTENCY")
    _entry(session, sku, 10)
    payload = StockExitCreate(
        sku_id=sku.id,
        quantity=2,
        exit_type="dispatch",
        tracking_number="SYNTHETIC-RETRY",
        warehouse="LA",
    )

    first_ordinary = inventory_service.create_stock_exit(
        session, payload, user_uuid="operator-a"
    )
    second_ordinary = inventory_service.create_stock_exit(
        session, payload, user_uuid="operator-a"
    )
    assert first_ordinary.id != second_ordinary.id
    assert inventory_service.get_current_stock(session, sku.id, "LA") == 6

    key = uuid4()
    keyed_payload = payload.model_copy(update={"quantity": 6})
    first_keyed = inventory_service.create_stock_exit(
        session,
        keyed_payload,
        user_uuid="operator-a",
        idempotency_key=key,
    )
    assert inventory_service.get_current_stock(session, sku.id, "LA") == 0

    replay = inventory_service.create_stock_exit(
        session,
        keyed_payload,
        user_uuid="operator-a",
        idempotency_key=key,
    )
    assert replay.id == first_keyed.id
    assert inventory_service.get_current_stock(session, sku.id, "LA") == 0
    assert len(session.exec(select(StockExit)).all()) == 3


def test_standalone_dispatch_key_binds_actor_scope_content_and_command_kind(
    session,
):
    sku = _sku(session, "STANDALONE-KEY-SCOPE")
    _entry(session, sku, 5)
    key = uuid4()
    payload = StockExitCreate(
        sku_id=sku.id,
        quantity=1,
        exit_type="dispatch",
        tracking_number="SYNTHETIC-SCOPE",
        warehouse="LA",
    )
    inventory_service.create_stock_exit(
        session, payload, user_uuid="operator-a", idempotency_key=key
    )

    with pytest.raises(HTTPException) as actor_conflict:
        inventory_service.create_stock_exit(
            session, payload, user_uuid="operator-b", idempotency_key=key
        )
    assert actor_conflict.value.status_code == 409

    with pytest.raises(HTTPException) as content_conflict:
        inventory_service.create_stock_exit(
            session,
            payload.model_copy(update={"quantity": 2}),
            user_uuid="operator-a",
            idempotency_key=key,
        )
    assert content_conflict.value.status_code == 409

    other_client_id = "99999999-9999-4999-8999-999999999999"
    session.execute(insert(clients).values(
        client_id=other_client_id,
        display_name="Synthetic other client",
        is_active=True,
        created_by_user_id="producer-test-admin",
    ))
    session.commit()
    foreign_scope = AccessContext(
        user_id="operator-a",
        role=Role.manager,
        allowed_client_ids=frozenset({other_client_id}),
        sensitive_client_ids=frozenset(),
    )
    with pytest.raises(HTTPException) as scope_rejection:
        _ORIGINAL_SERVICE_WRITES["create_stock_exit"](
            session,
            payload,
            user_uuid="operator-a",
            access=foreign_scope,
            idempotency_key=key,
        )
    assert scope_rejection.value.status_code == 404

    with pytest.raises(HTTPException) as loss_rejection:
        inventory_service.create_stock_exit(
            session,
            StockExitCreate(
                sku_id=sku.id,
                quantity=1,
                exit_type="loss",
                tracking_number=None,
                warehouse="LA",
            ),
            user_uuid="operator-a",
            idempotency_key=uuid4(),
        )
    assert loss_rejection.value.status_code == 422

    order = CommercialOrder(
        order_id=uuid4(), client_id=CLIENT_ID, warehouse="LA"
    )
    session.add(order)
    session.commit()
    session.refresh(order)
    commercial_key = uuid4()
    session.add(StockExit(
        sku_id=sku.id,
        quantity=1,
        exit_type="dispatch",
        tracking_number="COMMERCIAL-SYNTHETIC",
        warehouse="LA",
        user_uuid="operator-a",
        commercial_order_id=order.id,
        dispatch_id=commercial_key,
    ))
    session.commit()
    with pytest.raises(HTTPException) as commercial_conflict:
        inventory_service.create_stock_exit(
            session,
            payload.model_copy(update={"tracking_number": "COMMERCIAL-SYNTHETIC"}),
            user_uuid="operator-a",
            idempotency_key=commercial_key,
        )
    assert commercial_conflict.value.status_code == 409


def test_threshold_crosses_once_rearms_and_configuration_emits_nothing(
    session, monkeypatch
):
    sku = _sku(session, "THRESHOLD", threshold=5)
    _entry(session, sku, 10)
    inventory_service.create_stock_exit(
        session,
        StockExitCreate(
            sku_id=sku.id,
            quantity=6,
            exit_type="dispatch",
            tracking_number="T-1",
            warehouse="LA",
        ),
        user_uuid="user",
    )
    inventory_service.create_stock_exit(
        session,
        StockExitCreate(
            sku_id=sku.id,
            quantity=1,
            exit_type="dispatch",
            tracking_number="T-2",
            warehouse="LA",
        ),
        user_uuid="user",
    )
    assert len(_outbox(session, "stock_threshold_triggered")) == 1

    _entry(session, sku, 10)
    inventory_service.create_stock_exit(
        session,
        StockExitCreate(
            sku_id=sku.id,
            quantity=8,
            exit_type="dispatch",
            tracking_number="T-3",
            warehouse="LA",
        ),
        user_uuid="user",
    )
    threshold_rows = _outbox(session, "stock_threshold_triggered")
    assert len(threshold_rows) == 2
    assert [row["tags"]["quantity"] for row in threshold_rows] == [4, 5]
    assert all(row["tags"]["threshold_quantity"] == 5 for row in threshold_rows)
    evaluations = session.exec(
        select(InventoryThresholdEvaluation).order_by(InventoryThresholdEvaluation.id)
    ).all()
    assert len(evaluations) == 5
    crossed = [item for item in evaluations if item.crossed]
    assert len(crossed) == 2
    assert {item.event_id for item in crossed} == {
        row["event_id"] for row in threshold_rows
    }
    assert all(item.event_id is None for item in evaluations if not item.crossed)
    for evaluation in evaluations:
        if evaluation.source_table == "stockexit":
            movement = session.get(StockExit, evaluation.source_pk)
        else:
            movement = session.get(StockEntry, evaluation.source_pk)
        assert movement is not None
        assert evaluation.evaluated_at == movement.created_at
    assert _outbox(session, "outbound_order_created") == []

    before = len(session.execute(select(inventory_event_outbox)).all())
    invalidations = []
    monkeypatch.setattr(
        inventory_service,
        "invalidate_inventory_cache",
        lambda value: invalidations.append(value),
    )
    updated = inventory_service.update_sku_threshold(
        session,
        sku_id=sku.id,
        minimum_stock_threshold=None,
    )
    assert updated.minimum_stock_threshold is None
    assert len(session.execute(select(inventory_event_outbox)).all()) == before
    assert invalidations == [session]


def test_threshold_crossing_rolls_back_when_required_outbox_fails(
    session, monkeypatch
):
    sku = _sku(session, "THRESHOLD-ROLLBACK", threshold=5)
    _entry(session, sku, 10)
    before_exits = len(session.exec(select(StockExit)).all())
    monkeypatch.setattr(
        inventory_service,
        "enqueue_inventory_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("outbox")),
    )
    with pytest.raises(RuntimeError, match="outbox"):
        inventory_service.create_stock_exit(
            session,
            StockExitCreate(
                sku_id=sku.id,
                quantity=6,
                exit_type="dispatch",
                tracking_number="ROLLBACK",
                warehouse="LA",
            ),
            user_uuid="user",
        )
    assert len(session.exec(select(StockExit)).all()) == before_exits
    assert _outbox(session, "stock_threshold_triggered") == []


def test_physical_count_is_idempotent_exact_once_and_never_adjusts_stock(session):
    sku = _sku(session, "COUNT")
    _entry(session, sku, 5)
    matching_id = uuid4()
    matching = InventoryPhysicalCountCreate(
        count_id=matching_id,
        sku_id=sku.id,
        observed_quantity=5,
        warehouse="LA",
    )
    first = inventory_service.confirm_inventory_physical_count(
        session, matching, user_uuid="counter"
    )
    retry = inventory_service.confirm_inventory_physical_count(
        session, matching, user_uuid="counter"
    )
    assert retry.id == first.id
    assert _outbox(session, "inventory_discrepancy_detected") == []

    with pytest.raises(HTTPException) as conflict:
        inventory_service.confirm_inventory_physical_count(
            session,
            matching.model_copy(update={"observed_quantity": 4}),
            user_uuid="counter",
        )
    assert conflict.value.status_code == 409

    discrepancy = InventoryPhysicalCountCreate(
        count_id=uuid4(),
        sku_id=sku.id,
        observed_quantity=3,
        warehouse="LA",
    )
    observed = inventory_service.confirm_inventory_physical_count(
        session, discrepancy, user_uuid="counter"
    )
    retried = inventory_service.confirm_inventory_physical_count(
        session, discrepancy, user_uuid="counter"
    )
    assert retried.id == observed.id
    rows = _outbox(session, "inventory_discrepancy_detected")
    assert len(rows) == 1
    assert rows[0]["tags"] == {
        "warehouse": "los_angeles",
        "client_id": CLIENT_ID,
        "product_id": str(sku.id),
        "product_category": "fashion",
        "quantity": 2,
        "system_quantity": 5,
        "counted_quantity": 3,
        "discrepancy_delta": -2,
        "discrepancy_type": "shortage",
        "count_id": str(discrepancy.count_id),
    }
    assert inventory_service.get_current_stock(session, sku.id, "LA") == 5


def test_physical_count_rolls_back_when_required_outbox_fails(session, monkeypatch):
    sku = _sku(session, "COUNT-ROLLBACK")
    _entry(session, sku, 5)
    count_id = uuid4()
    monkeypatch.setattr(
        inventory_service,
        "enqueue_inventory_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("outbox")),
    )
    with pytest.raises(RuntimeError, match="outbox"):
        inventory_service.confirm_inventory_physical_count(
            session,
            InventoryPhysicalCountCreate(
                count_id=count_id,
                sku_id=sku.id,
                observed_quantity=1,
                warehouse="LA",
            ),
            user_uuid="counter",
        )
    assert session.exec(
        select(InventoryPhysicalCount).where(
            InventoryPhysicalCount.count_id == count_id
        )
    ).first() is None


def _order_payload(sku: SKU, *, order_id=None, dispatches=None):
    return CommercialOrderCreate(
        order_id=order_id or uuid4(),
        client_id=CLIENT_ID,
        warehouse="LA",
        dispatches=dispatches
        or [
            {
                "dispatch_id": uuid4(),
                "sku_id": sku.id,
                "quantity": 2,
                "tracking_number": "ORDER-1",
            }
        ],
    )


def test_order_retry_conflict_and_partial_append_are_exact_once(session, monkeypatch):
    sku = _sku(session, "ORDER")
    _entry(session, sku, 10)
    invalidations = []
    monkeypatch.setattr(
        inventory_service,
        "invalidate_inventory_cache",
        lambda value: invalidations.append(value),
    )
    payload = _order_payload(sku)
    order, dispatches = inventory_service.create_commercial_order(
        session, payload, user_uuid="user"
    )
    retry_order, retry_dispatches = inventory_service.create_commercial_order(
        session, payload, user_uuid="user"
    )
    assert retry_order.id == order.id
    assert len(dispatches) == len(retry_dispatches) == 1
    assert inventory_service.get_current_stock(session, sku.id, "LA") == 8
    assert invalidations == [session]
    outbound = _outbox(session, "outbound_order_created")
    assert len(outbound) == 1
    assert outbound[0]["tags"] == {
        "order_id": str(payload.order_id),
        "warehouse": "los_angeles",
        "client_id": CLIENT_ID,
    }

    conflicting = payload.model_copy(
        update={
            "dispatches": [
                payload.dispatches[0].model_copy(update={"quantity": 3})
            ]
        }
    )
    with pytest.raises(HTTPException) as conflict:
        inventory_service.create_commercial_order(
            session, conflicting, user_uuid="user"
        )
    assert conflict.value.status_code == 409

    new_dispatch_id = uuid4()
    sibling_dispatch_id = uuid4()
    append = CommercialOrderDispatchAppend(
        order_id=payload.order_id,
        dispatches=[
            {
                "dispatch_id": new_dispatch_id,
                "sku_id": sku.id,
                "quantity": 2,
                "tracking_number": "ORDER-2",
            },
            {
                "dispatch_id": sibling_dispatch_id,
                "sku_id": sku.id,
                "quantity": 1,
                "tracking_number": "ORDER-3",
            },
        ],
    )
    _order, appended = inventory_service.append_commercial_order_dispatches(
        session, append, user_uuid="user"
    )
    assert inventory_service.get_current_stock(session, sku.id, "LA") == 5
    assert invalidations == [session, session]
    _order, retried = inventory_service.append_commercial_order_dispatches(
        session, append, user_uuid="user"
    )
    assert inventory_service.get_current_stock(session, sku.id, "LA") == 5
    assert invalidations == [session, session]
    assert {item.dispatch_id for item in appended} == {
        payload.dispatches[0].dispatch_id,
        new_dispatch_id,
        sibling_dispatch_id,
    }
    assert len(retried) == 3
    assert len(_outbox(session, "outbound_order_created")) == 1

    replayed_order, original_response = inventory_service.create_commercial_order(
        session, payload, user_uuid="user"
    )
    assert replayed_order.id == order.id
    assert [item.dispatch_id for item in original_response] == [
        payload.dispatches[0].dispatch_id
    ]

    with pytest.raises(HTTPException) as create_with_append:
        inventory_service.create_commercial_order(
            session,
            payload.model_copy(update={"dispatches": [append.dispatches[0]]}),
            user_uuid="user",
        )
    assert create_with_append.value.status_code == 409

    with pytest.raises(HTTPException) as append_with_create_ids:
        inventory_service.append_commercial_order_dispatches(
            session,
            CommercialOrderDispatchAppend(
                order_id=payload.order_id,
                dispatches=payload.dispatches,
            ),
            user_uuid="user",
        )
    assert append_with_create_ids.value.status_code == 409

    with pytest.raises(HTTPException) as subset_retry:
        inventory_service.append_commercial_order_dispatches(
            session,
            append.model_copy(update={"dispatches": [append.dispatches[0]]}),
            user_uuid="user",
        )
    assert subset_retry.value.status_code == 409

    later = CommercialOrderDispatchAppend(
        order_id=payload.order_id,
        dispatches=[{
            "dispatch_id": uuid4(),
            "sku_id": sku.id,
            "quantity": 1,
            "tracking_number": "ORDER-LATER",
        }],
    )
    inventory_service.append_commercial_order_dispatches(
        session, later, user_uuid="user"
    )
    _order, replay_after_later = inventory_service.append_commercial_order_dispatches(
        session, append, user_uuid="user"
    )
    assert [item.dispatch_id for item in replay_after_later] == [
        item.dispatch_id for item in retried
    ]
    with pytest.raises(HTTPException) as mixed_batches:
        inventory_service.append_commercial_order_dispatches(
            session,
            append.model_copy(update={
                "dispatches": [append.dispatches[0], later.dispatches[0]]
            }),
            user_uuid="user",
        )
    assert mixed_batches.value.status_code == 409

    bad_append = append.model_copy(
        update={
            "dispatches": [
                append.dispatches[0].model_copy(update={"quantity": 4})
            ]
        }
    )
    with pytest.raises(HTTPException) as dispatch_conflict:
        inventory_service.append_commercial_order_dispatches(
            session, bad_append, user_uuid="user"
        )
    assert dispatch_conflict.value.status_code == 409

    duplicate_create = _order_payload(sku).model_copy(
        update={"dispatches": [payload.dispatches[0], payload.dispatches[0]]}
    )
    with pytest.raises(HTTPException) as duplicate_create_conflict:
        inventory_service.create_commercial_order(
            session, duplicate_create, user_uuid="user"
        )
    assert duplicate_create_conflict.value.status_code == 409

    duplicate_append = append.model_copy(
        update={"dispatches": [append.dispatches[1], append.dispatches[1]]}
    )
    with pytest.raises(HTTPException) as duplicate_append_conflict:
        inventory_service.append_commercial_order_dispatches(
            session, duplicate_append, user_uuid="user"
        )
    assert duplicate_append_conflict.value.status_code == 409


def test_order_and_dispatches_roll_back_when_outbox_fails(session, monkeypatch):
    sku = _sku(session, "ORDER-ROLLBACK")
    _entry(session, sku, 10)
    payload = _order_payload(sku)
    monkeypatch.setattr(
        inventory_service,
        "enqueue_inventory_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("outbox")),
    )
    with pytest.raises(RuntimeError, match="outbox"):
        inventory_service.create_commercial_order(
            session, payload, user_uuid="user"
        )
    assert session.exec(
        select(CommercialOrder).where(CommercialOrder.order_id == payload.order_id)
    ).first() is None
    assert session.exec(
        select(StockExit).where(
            StockExit.dispatch_id == payload.dispatches[0].dispatch_id
        )
    ).first() is None


def test_global_order_uuid_collision_is_opaque_and_rolls_back(session):
    first_sku = _sku(session, "ORDER-GLOBAL-FIRST")
    _entry(session, first_sku, 10)
    shared_order_id = uuid4()
    first = _order_payload(first_sku, order_id=shared_order_id)
    inventory_service.create_commercial_order(session, first, user_uuid="first-user")

    second_client_id = "99999999-9999-4999-8999-999999999999"
    session.execute(insert(clients).values(
        client_id=second_client_id,
        display_name="Synthetic Second Client",
        is_active=True,
        created_by_user_id="producer-test-admin",
    ))
    second_sku = SKU(
        name="ORDER-GLOBAL-SECOND",
        sku="ORDER-GLOBAL-SECOND",
        client_id=second_client_id,
        client_name="Synthetic Second Client",
        category="fashion",
        warehouse="LA",
    )
    session.add(second_sku)
    session.commit()
    session.refresh(second_sku)
    inventory_service.create_stock_entry(
        session,
        StockEntryCreate(
            sku_id=second_sku.id,
            quantity=10,
            reference=f"IN-{uuid4()}",
            warehouse="LA",
        ),
        user_uuid="second-user",
    )
    second_dispatch_id = uuid4()
    second = CommercialOrderCreate(
        order_id=shared_order_id,
        client_id=second_client_id,
        warehouse="LA",
        dispatches=[{
            "dispatch_id": second_dispatch_id,
            "sku_id": second_sku.id,
            "quantity": 3,
            "tracking_number": "ORDER-SECOND",
        }],
    )

    with pytest.raises(HTTPException) as collision:
        inventory_service.create_commercial_order(
            session, second, user_uuid="second-user"
        )
    assert collision.value.status_code == 409
    assert collision.value.detail == "Commercial order command conflicts."
    assert session.exec(
        select(StockExit).where(StockExit.dispatch_id == second_dispatch_id)
    ).first() is None
    assert inventory_service.get_current_stock(session, second_sku.id, "LA") == 10
    assert len(session.exec(select(CommercialOrder)).all()) == 1
    assert len(_outbox(session, "outbound_order_created")) == 1


def test_unrelated_parent_flush_integrity_error_is_not_mapped(session, monkeypatch):
    sku = _sku(session, "ORDER-UNRELATED-INTEGRITY")
    _entry(session, sku, 10)
    payload = _order_payload(sku)
    original_flush = session.flush

    def unrelated_flush(*_args, **_kwargs):
        raise IntegrityError(
            "INSERT synthetic",
            {},
            Exception("UNIQUE constraint failed: unrelated.value"),
        )

    monkeypatch.setattr(session, "flush", unrelated_flush)
    with pytest.raises(IntegrityError):
        inventory_service.create_commercial_order(
            session, payload, user_uuid="user"
        )
    monkeypatch.setattr(session, "flush", original_flush)
    assert session.exec(
        select(StockExit).where(
            StockExit.dispatch_id == payload.dispatches[0].dispatch_id
        )
    ).first() is None
