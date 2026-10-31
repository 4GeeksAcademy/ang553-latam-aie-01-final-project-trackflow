"""Focused schema and constraint contracts for inventory business events."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid1, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from services.api.inventory_models import (
    CommercialOrder,
    InventoryPhysicalCount,
    SKU,
    StockExit,
)
from services.api.inventory_outbox import (
    OUTBOX_SOURCE_EVENT_TYPES,
    enqueue_inventory_event,
    inventory_event_outbox,
    inventory_outbox_metadata,
)
from services.api.inventory_schemas import (
    CommercialOrderCreate,
    CommercialOrderDispatchAppend,
    InventoryPhysicalCountCreate,
    SKUCreate,
    SKUThresholdUpdate,
)
from services.api.inventory_clients import CLIENT_IDS_BY_NAME


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    inventory_outbox_metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def _sku(session: Session, *, threshold: int | None = None) -> SKU:
    sku = SKU(
        name="Business-event SKU",
        sku=f"BUSINESS-{uuid4()}",
        client_name="PureStep Footwear",
        client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
        category="fashion",
        warehouse="LA",
        minimum_stock_threshold=threshold,
    )
    session.add(sku)
    session.commit()
    session.refresh(sku)
    return sku


def test_threshold_is_nullable_or_nonnegative_and_schema_rejects_negative(session):
    disabled = _sku(session)
    enabled = _sku(session, threshold=0)
    assert disabled.minimum_stock_threshold is None
    assert enabled.minimum_stock_threshold == 0

    with pytest.raises(ValidationError):
        SKUCreate(
            name="Invalid threshold",
            sku="INVALID-THRESHOLD",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="fashion",
            warehouse="LA",
            minimum_stock_threshold=-1,
        )
    with pytest.raises(ValidationError):
        SKUCreate(
            name="Boolean threshold",
            sku="BOOLEAN-THRESHOLD",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="fashion",
            warehouse="LA",
            minimum_stock_threshold=True,
        )

    invalid = SKU(
        name="Database constraint",
        sku="INVALID-THRESHOLD-DB",
        client_name="Client",
        client_id="client",
        category="fashion",
        warehouse="LA",
        minimum_stock_threshold=-1,
    )
    session.add(invalid)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_threshold_update_is_required_nullable_strict_and_closed():
    assert SKUThresholdUpdate(minimum_stock_threshold=None).minimum_stock_threshold is None
    assert SKUThresholdUpdate(minimum_stock_threshold=0).minimum_stock_threshold == 0
    for payload in (
        {},
        {"minimum_stock_threshold": True},
        {"minimum_stock_threshold": -1},
        {"minimum_stock_threshold": None, "extra": "forbidden"},
    ):
        with pytest.raises(ValidationError):
            SKUThresholdUpdate.model_validate(payload)


def test_idempotent_order_commands_require_uuid_and_nonempty_dispatches():
    order_id = uuid4()
    payload = {
        "order_id": str(order_id),
        "client_id": "client",
        "warehouse": "LA",
        "dispatches": [
            {
                "dispatch_id": str(uuid4()),
                "sku_id": 1,
                "quantity": 2,
                "tracking_number": "TRACK",
            }
        ],
    }
    first = CommercialOrderCreate.model_validate(payload)
    retry = CommercialOrderCreate.model_validate(payload)
    assert first.order_id == retry.order_id == order_id
    append = CommercialOrderDispatchAppend(
        order_id=order_id,
        dispatches=[
            {
                "dispatch_id": uuid4(),
                "sku_id": 1,
                "quantity": 1,
                "tracking_number": "PARTIAL",
            }
        ],
    )
    assert append.order_id == order_id

    invalid_payloads = (
        {**payload, "order_id": "not-a-uuid"},
        {**payload, "order_id": str(uuid1())},
        {**payload, "dispatches": []},
        {
            **payload,
            "dispatches": [
                {
                    "dispatch_id": str(uuid4()),
                    "sku_id": True,
                    "quantity": 1,
                    "tracking_number": "TRACK",
                }
            ],
        },
        {
            **payload,
            "dispatches": [
                {
                    "dispatch_id": str(uuid4()),
                    "sku_id": 1,
                    "quantity": False,
                    "tracking_number": "TRACK",
                }
            ],
        },
        {**payload, "extra": "forbidden"},
    )
    for invalid in invalid_payloads:
        with pytest.raises(ValidationError):
            CommercialOrderCreate.model_validate(invalid)
    with pytest.raises(ValidationError):
        CommercialOrderDispatchAppend(order_id=order_id, dispatches=[])

    duplicate = [payload["dispatches"][0], payload["dispatches"][0]]
    with pytest.raises(ValidationError) as create_duplicate:
        CommercialOrderCreate.model_validate({**payload, "dispatches": duplicate})
    assert "dispatch_id values must be unique" in str(create_duplicate.value)
    with pytest.raises(ValidationError) as append_duplicate:
        CommercialOrderDispatchAppend.model_validate(
            {"order_id": str(order_id), "dispatches": duplicate}
        )
    assert "dispatch_id values must be unique" in str(append_duplicate.value)


def test_commercial_order_supports_multiple_dispatches_and_rejects_loss_link(session):
    sku = _sku(session)
    order = CommercialOrder(
        client_id=sku.client_id,
        warehouse="LA",
    )
    session.add(order)
    session.commit()
    session.refresh(order)
    first = StockExit(
        dispatch_id=uuid4(),
        sku_id=sku.id,
        quantity=1,
        exit_type="dispatch",
        tracking_number="TRACK-1",
        warehouse="LA",
        user_uuid="user",
        commercial_order_id=order.id,
    )
    second = StockExit(
        dispatch_id=uuid4(),
        sku_id=sku.id,
        quantity=2,
        exit_type="dispatch",
        tracking_number="TRACK-2",
        warehouse="LA",
        user_uuid="user",
        commercial_order_id=order.id,
    )
    session.add(first)
    session.add(second)
    session.commit()
    assert len(
        session.exec(
            select(StockExit).where(StockExit.commercial_order_id == order.id)
        ).all()
    ) == 2

    loss = StockExit(
        dispatch_id=uuid4(),
        sku_id=sku.id,
        quantity=1,
        exit_type="loss",
        warehouse="LA",
        user_uuid="user",
        commercial_order_id=order.id,
    )
    session.add(loss)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_stable_order_and_count_uuids_are_unique(session):
    sku = _sku(session)
    order_id = uuid4()
    session.add(
        CommercialOrder(
            order_id=order_id,
            client_id=sku.client_id,
            warehouse="LA",
        )
    )
    session.commit()
    session.add(
        CommercialOrder(
            order_id=order_id,
            client_id=sku.client_id,
            warehouse="LA",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    count_id = uuid4()
    session.add(
        InventoryPhysicalCount(
            count_id=count_id,
            sku_id=sku.id,
            client_id=sku.client_id,
            warehouse="LA",
            expected_quantity=5,
            observed_quantity=4,
            user_uuid="counter",
        )
    )
    session.commit()
    session.add(
        InventoryPhysicalCount(
            count_id=count_id,
            sku_id=sku.id,
            client_id=sku.client_id,
            warehouse="LA",
            expected_quantity=5,
            observed_quantity=5,
            user_uuid="counter",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_physical_count_quantities_are_nonnegative_without_stock_mutation(session):
    sku = _sku(session)
    count = InventoryPhysicalCount(
        sku_id=sku.id,
        client_id=sku.client_id,
        warehouse="LA",
        expected_quantity=8,
        observed_quantity=6,
        user_uuid="counter",
    )
    session.add(count)
    session.commit()
    assert session.exec(select(StockExit)).all() == []

    with pytest.raises(ValidationError):
        InventoryPhysicalCountCreate(
            count_id=uuid4(),
            sku_id=sku.id,
            observed_quantity=-1,
            warehouse="LA",
        )
    with pytest.raises(ValidationError):
        InventoryPhysicalCountCreate(
            count_id=uuid4(),
            sku_id=sku.id,
            observed_quantity=False,
            warehouse="LA",
        )
    invalid = InventoryPhysicalCount(
        sku_id=sku.id,
        client_id=sku.client_id,
        warehouse="LA",
        expected_quantity=0,
        observed_quantity=-1,
        user_uuid="counter",
    )
    session.add(invalid)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_physical_count_command_requires_stable_uuid_and_forbids_extra():
    valid_id = uuid4()
    command = InventoryPhysicalCountCreate(
        count_id=valid_id,
        sku_id=1,
        observed_quantity=0,
        warehouse="LA",
    )
    assert command.count_id == valid_id
    for payload in (
        {"sku_id": 1, "observed_quantity": 0, "warehouse": "LA"},
        {
            "count_id": "not-a-uuid",
            "sku_id": 1,
            "observed_quantity": 0,
            "warehouse": "LA",
        },
        {
            "count_id": str(uuid1()),
            "sku_id": 1,
            "observed_quantity": 0,
            "warehouse": "LA",
        },
        {
            "count_id": str(valid_id),
            "sku_id": 1,
            "observed_quantity": 0,
            "warehouse": "LA",
            "extra": "forbidden",
        },
    ):
        with pytest.raises(ValidationError):
            InventoryPhysicalCountCreate.model_validate(payload)


def test_outbox_accepts_only_authorized_source_event_pairs(session):
    now = datetime.now(timezone.utc)
    for index, (source, event_type) in enumerate(
        sorted(OUTBOX_SOURCE_EVENT_TYPES),
        start=1,
    ):
        enqueue_inventory_event(
            session,
            event_id=uuid4(),
            occurred_at=now,
            source_table=source,
            source_pk=index,
            event_type=event_type,
            tags={"warehouse": "los_angeles", "client_id": "client"},
        )
    session.commit()
    assert len(session.execute(select(inventory_event_outbox)).all()) == 4

    with pytest.raises(ValueError, match="authorized pair"):
        enqueue_inventory_event(
            session,
            event_id=uuid4(),
            occurred_at=now,
            source_table="stockexit",
            source_pk=9,
            event_type="outbound_order_created",
            tags={},
        )


def test_migration_requires_existing_clean_outbox_before_constraint_replacement():
    migration = Path("infra/sql/inventory_business_event_contract.sql").read_text(
        encoding="utf-8"
    )
    baseline_guard = migration.index("to_regclass('public.inventory_event_outbox')")
    legacy_guard = migration.index("legacy inventory outbox rows")
    constraint_drop = migration.index("DROP CONSTRAINT")
    assert baseline_guard < legacy_guard < constraint_drop
