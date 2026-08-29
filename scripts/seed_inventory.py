#!/usr/bin/env python3
"""
TrackFlow Inventory Seed — PostgreSQL/Supabase.

Loads the minimum required data set for TrackFlow:

    * 6 SKU
    * 4 StockEntry
    * 3 StockExit

Idempotent — safe to run multiple times without duplicating data.
Uses the real DATABASE_URL from the environment.
"""

from __future__ import annotations

import sys

from sqlmodel import Session, select

# These imports register the models in SQLModel.metadata so that the
# engine can be created lazily — no DATABASE_URL needed at import time.
from services.api.database import _get_engine
from services.api.inventory_models import SKU, StockEntry, StockExit
from services.api.inventory_service import get_current_stocks

# ── Constants ────────────────────────────────────────────────────────────────

SEED_USER_UUID = "seed-system-user"

# ── Seed data ────────────────────────────────────────────────────────────────

SKUS = [
    {
        "name": "Zapatilla blanca clásica - Talla 42",
        "sku": "CLT-SNK-W-42",
        "client_name": "PureStep Footwear",
        "category": "fashion",
        "warehouse": "LA",
    },
    {
        "name": "Zapatilla blanca clásica - Talla 42",
        "sku": "CLT-SNK-W-42-Z",
        "client_name": "PureStep Footwear",
        "category": "fashion",
        "warehouse": "ZGZ",
    },
    {
        "name": "Auriculares inalámbricos Pro",
        "sku": "TEC-EAR-001",
        "client_name": "SoundWave Electronics",
        "category": "electronics",
        "warehouse": "LA",
    },
    {
        "name": "Sérum facial hidratante 30ml",
        "sku": "CSM-SRM-030",
        "client_name": "GlowLab Cosmetics",
        "category": "cosmetics",
        "warehouse": "ZGZ",
    },
    {
        "name": "Chino slim fit - marino 32/32",
        "sku": "CLT-CHN-N-32",
        "client_name": "UrbanThread",
        "category": "fashion",
        "warehouse": "LA",
    },
    {
        "name": "Cargador rápido USB-C 65W",
        "sku": "TEC-CHG-065",
        "client_name": "SoundWave Electronics",
        "category": "electronics",
        "warehouse": "ZGZ",
    },
]

# StockEntry data — references are keyed by a logical id for idempotency checks.
# The 'sku_key' references the SKU code (resolved to id dynamically).
STOCK_ENTRIES = [
    {
        "sku_key": "CLT-SNK-W-42",
        "quantity": 20,
        "reference": "PO-2024-0098",
        "warehouse": "LA",
    },
    {
        "sku_key": "CLT-SNK-W-42",
        "quantity": 15,
        "reference": "GR-LA-0234",
        "warehouse": "LA",
    },
    {
        "sku_key": "CLT-SNK-W-42-Z",
        "quantity": 30,
        "reference": "PO-ZGZ-0042",
        "warehouse": "ZGZ",
    },
    {
        "sku_key": "TEC-EAR-001",
        "quantity": 12,
        "reference": "PO-2024-0101",
        "warehouse": "LA",
    },
]

# StockExit data — similarly keyed by sku_key.
STOCK_EXITS = [
    {
        "sku_key": "CLT-SNK-W-42",
        "quantity": 5,
        "exit_type": "dispatch",
        "tracking_number": "1Z999AA10123456784",
        "warehouse": "LA",
    },
    {
        "sku_key": "TEC-EAR-001",
        "quantity": 2,
        "exit_type": "loss",
        "tracking_number": None,
        "warehouse": "LA",
    },
    {
        "sku_key": "CLT-SNK-W-42-Z",
        "quantity": 3,
        "exit_type": "dispatch",
        "tracking_number": "1Z999AA10123456785",
        "warehouse": "ZGZ",
    },
]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _resolve_sku_map(session: Session) -> dict[str, int]:
    """Build a {sku_code: id} mapping from the database.

    Raises ``SystemExit`` if any required SKU is missing.
    """
    all_skus = session.exec(select(SKU)).all()
    sku_map: dict[str, int] = {s.sku: s.id for s in all_skus}  # type: ignore[assignment]

    for sku_data in SKUS:
        code = sku_data["sku"]
        if code not in sku_map:
            print(f"ERROR: SKU '{code}' not found in database after seeding.")
            sys.exit(1)

    return sku_map


# ── Seeding functions ────────────────────────────────────────────────────────


def _seed_skus(session: Session) -> int:
    """Insert SKU records that do not already exist.

    Returns the number of newly created SKUs.
    """
    inserted = 0

    for sku_data in SKUS:
        code = sku_data["sku"]
        existing = session.exec(select(SKU).where(SKU.sku == code)).first()
        if existing is not None:
            continue

        sku = SKU(**sku_data)
        session.add(sku)
        session.commit()
        inserted += 1

    return inserted


def _seed_stock_entries(session: Session, sku_map: dict[str, int]) -> int:
    """Insert StockEntry records that do not already exist.

    Idempotency is achieved by checking the combination of
    ``(sku_id, quantity, reference, warehouse)`` which is expected to be
    logically unique for seed data.

    Returns the number of newly created entries.
    """
    inserted = 0

    for entry_data in STOCK_ENTRIES:
        sku_id = sku_map[entry_data["sku_key"]]
        qty = entry_data["quantity"]
        ref = entry_data["reference"]
        wh = entry_data["warehouse"]

        # Check if this exact entry already exists using the logical
        # combination that makes it unique for seed purposes.
        existing = session.exec(
            select(StockEntry).where(
                StockEntry.sku_id == sku_id,
                StockEntry.quantity == qty,
                StockEntry.reference == ref,
                StockEntry.warehouse == wh,
            )
        ).first()

        if existing is not None:
            continue

        entry = StockEntry(
            sku_id=sku_id,
            quantity=qty,
            reference=ref,
            warehouse=wh,
            user_uuid=SEED_USER_UUID,
        )
        session.add(entry)
        session.commit()
        inserted += 1

    return inserted


def _seed_stock_exits(session: Session, sku_map: dict[str, int]) -> int:
    """Insert StockExit records that do not already exist.

    Idempotency is achieved by checking the combination of
    ``(sku_id, quantity, exit_type, tracking_number, warehouse)``.

    Returns the number of newly created exits.
    """
    inserted = 0

    for exit_data in STOCK_EXITS:
        sku_id = sku_map[exit_data["sku_key"]]
        qty = exit_data["quantity"]
        etype = exit_data["exit_type"]
        tkn = exit_data["tracking_number"]
        wh = exit_data["warehouse"]

        existing = session.exec(
            select(StockExit).where(
                StockExit.sku_id == sku_id,
                StockExit.quantity == qty,
                StockExit.exit_type == etype,
                StockExit.tracking_number == tkn,
                StockExit.warehouse == wh,
            )
        ).first()

        if existing is not None:
            continue

        exit_record = StockExit(
            sku_id=sku_id,
            quantity=qty,
            exit_type=etype,
            tracking_number=tkn,
            warehouse=wh,
            user_uuid=SEED_USER_UUID,
        )
        session.add(exit_record)
        session.commit()
        inserted += 1

    return inserted


# ── Verification ─────────────────────────────────────────────────────────────


def _verify(
    session: Session,
    sku_map: dict[str, int],
    *,
    phase: str,
) -> None:
    """Print a verification report after seeding."""
    all_skus = session.exec(select(SKU)).all()
    all_entries = session.exec(select(StockEntry)).all()
    all_exits = session.exec(select(StockExit)).all()

    print(f"\n{'═' * 60}")
    print(f"  VERIFICATION — {phase}")
    print(f"{'═' * 60}")

    # ── Counts ──────────────────────────────────────────────────────────
    print(f"\n  SKU:        {len(all_skus)}")
    print(f"  StockEntry: {len(all_entries)}")
    print(f"  StockExit:  {len(all_exits)}")

    # ── SKU list ─────────────────────────────────────────────────────────
    print(f"\n  ── SKU list ────────────────────────")
    for s in all_skus:
        print(f"    ID={s.id:<3}  {s.sku:<20s}  {s.name:<35s}  [{s.warehouse}]")

    # ── Entry list ───────────────────────────────────────────────────────
    print(f"\n  ── StockEntry list ─────────────────")
    for e in all_entries:
        print(
            f"    ID={e.id:<3}  SKU_ID={e.sku_id:<3}  "
            f"qty={e.quantity:<4}  ref={e.reference:<20s}  [{e.warehouse}]"
        )

    # ── Exit list ────────────────────────────────────────────────────────
    print(f"\n  ── StockExit list ──────────────────")
    for x in all_exits:
        tkn_display = x.tracking_number if x.tracking_number else "None"
        print(
            f"    ID={x.id:<3}  SKU_ID={x.sku_id:<3}  "
            f"qty={x.quantity:<4}  type={x.exit_type:<10s}  "
            f"tracking={tkn_display:<25s}  [{x.warehouse}]"
        )

    # ── Current stock ────────────────────────────────────────────────────
    stocks = get_current_stocks(session)
    print(f"\n  ── Current stock ────────────────────")
    id_to_sku = {s.id: s.sku for s in all_skus}
    id_to_sku_name = {s.id: s.name for s in all_skus}
    all_non_negative = True
    # Iterate over ALL existing SKUs (not just pairs with movements),
    # defaulting to 0 for SKU+warehouse pairs without any movements.
    for s in sorted(all_skus, key=lambda sku: sku.sku):
        stock = stocks.get((s.id, s.warehouse), 0)
        label = f"{s.sku} @ {s.warehouse}"
        print(f"    {label:<35s}  stock={stock}")
        if stock < 0:
            all_non_negative = False

    if all_non_negative:
        print(f"\n  ✅ All stock >= 0")
    else:
        print(f"\n  ❌ NEGATIVE STOCK DETECTED!")

    # ── Verify dispatch ──────────────────────────────────────────────────
    dispatch_found = False
    for x in all_exits:
        if x.tracking_number == "1Z999AA10123456784":
            dispatch_found = True
            print(
                f"  ✅ Dispatch found: "
                f"exit_type={x.exit_type}, "
                f"tracking={x.tracking_number}, "
                f"SKU={id_to_sku.get(x.sku_id, '?')}"
            )
            break
    if not dispatch_found:
        print(f"  ❌ Dispatch with tracking 1Z999AA10123456784 NOT found!")

    # ── Verify loss ──────────────────────────────────────────────────────
    loss_found = False
    for x in all_exits:
        if x.exit_type == "loss" and x.tracking_number is None:
            loss_found = True
            print(
                f"  ✅ Loss found: "
                f"exit_type={x.exit_type}, "
                f"tracking={x.tracking_number}, "
                f"SKU={id_to_sku.get(x.sku_id, '?')}, "
                f"qty={x.quantity}"
            )
            break
    if not loss_found:
        print(f"  ❌ Loss (exit_type=loss, tracking=None) NOT found!")

    # ── Verify FK ────────────────────────────────────────────────────────
    fk_ok = True
    for e in all_entries:
        if e.sku_id not in id_to_sku:
            print(f"  ❌ StockEntry ID={e.id} references non-existent SKU ID={e.sku_id}")
            fk_ok = False
    for x in all_exits:
        if x.sku_id not in id_to_sku:
            print(f"  ❌ StockExit ID={x.id} references non-existent SKU ID={x.sku_id}")
            fk_ok = False
    if fk_ok:
        print(f"  ✅ All FK references are valid")
    else:
        print(f"  ❌ Some FK references are broken!")

    print(f"{'═' * 60}\n")


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    engine = _get_engine()

    with Session(engine) as session:
        # 1. Seed SKUs
        skus_inserted = _seed_skus(session)
        print(f"SKU inserted: {skus_inserted}")

        # 2. Build SKU map
        sku_map = _resolve_sku_map(session)

        # 3. Seed StockEntry
        entries_inserted = _seed_stock_entries(session, sku_map)
        print(f"StockEntry inserted: {entries_inserted}")

        # 4. Seed StockExit
        exits_inserted = _seed_stock_exits(session, sku_map)
        print(f"StockExit inserted: {exits_inserted}")

        # 5. Verify
        _verify(session, sku_map, phase="AFTER SEED")


if __name__ == "__main__":
    main()