#!/usr/bin/env python3
"""Reproducible, isolated benchmark for TrackFlow cache candidates.

This development-only harness never uses the configured database or the
repository's TinyDB files.  Each process creates temporary SQLite and TinyDB
stores, seeds one deterministic profile, performs read-only HTTP requests,
and prints structured JSON to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Make ``python scripts/benchmark_cache_candidates.py`` work from the repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The application requires this at import time.  This value is process-local,
# deliberately fake, and is never printed or written to the repository.
os.environ.setdefault("JWT_SECRET_KEY", "benchmark-only-secret")

from httpx import ASGITransport, AsyncClient
from sqlmodel import Session, SQLModel, create_engine
from tinydb import TinyDB

import services.api.auth_security as auth_security
import services.api.auth_services as auth_services
import services.api.database as database
import services.api.routes.suppliers as suppliers_route
from services.api.auth_models import UserInDB
from services.api.auth_security import hash_password
from services.api.inventory_models import SKU, StockEntry, StockExit
from services.api.main import app
from services.api.seed import SUPPLIERS_SEED

PROFILES: dict[str, dict[str, int]] = {
    "base": {"skus": 6, "stock_entries": 4, "stock_exits": 3, "suppliers": 15},
    "medium": {"skus": 100, "stock_entries": 500, "stock_exits": 500, "suppliers": 200},
    "large": {"skus": 500, "stock_entries": 2500, "stock_exits": 2500, "suppliers": 1000},
}

CATEGORIES = ("fashion", "electronics", "cosmetics")
WAREHOUSES = ("LA", "ZGZ")
EXIT_TYPES = ("dispatch", "loss")


class TimingCapture(logging.Handler):
    """Capture middleware messages without printing request details."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "api.timing":
            self.messages.append(record.getMessage())


def _timing_ms(message: str) -> float:
    return float(message.rsplit("|", 1)[1].strip()[:-2])


def _percentile95(values: list[float]) -> float:
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def _stats(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 3),
        "avg": round(statistics.mean(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": round(_percentile95(values), 3),
        "max": round(max(values), 3),
    }


def _supplier_rows(count: int) -> list[dict[str, Any]]:
    """Expand the validated seed templates deterministically."""
    rows: list[dict[str, Any]] = []
    for index in range(count):
        template = dict(SUPPLIERS_SEED[index % len(SUPPLIERS_SEED)])
        template["name"] = f"{template['name']} Benchmark {index + 1:04d}"
        template["rate_per_shipment"] = round(
            float(template["rate_per_shipment"]) * (1 + (index % 17) / 100), 2
        )
        template["updated_at"] = "2026-01-01T00:00:00+00:00"
        rows.append(template)
    return rows


def _seed_inventory(engine: Any, counts: dict[str, int], user_uuid: str) -> int:
    rng = random.Random(42)
    with Session(engine) as session:
        skus = [
            SKU(
                name=f"Benchmark Product {index + 1:04d}",
                sku=f"BM-{index + 1:05d}",
                client_name=f"Benchmark Client {(index % 19) + 1:02d}",
                category=CATEGORIES[index % len(CATEGORIES)],
                warehouse=WAREHOUSES[index % len(WAREHOUSES)],
            )
            for index in range(counts["skus"])
        ]
        session.add_all(skus)
        session.commit()
        for sku in skus:
            session.refresh(sku)

        entries: list[StockEntry] = []
        for index in range(counts["stock_entries"]):
            sku = skus[index % len(skus)]
            entries.append(
                StockEntry(
                    sku_id=sku.id,
                    quantity=20 + rng.randrange(1, 81),
                    reference=f"BENCH-IN-{index + 1:05d}",
                    warehouse=sku.warehouse,
                    user_uuid=user_uuid,
                )
            )
        session.add_all(entries)
        session.commit()

        exits: list[StockExit] = []
        for index in range(counts["stock_exits"]):
            sku = skus[index % len(skus)]
            exit_type = EXIT_TYPES[index % len(EXIT_TYPES)]
            exits.append(
                StockExit(
                    sku_id=sku.id,
                    quantity=1 + rng.randrange(1, 11),
                    exit_type=exit_type,
                    tracking_number=(f"BENCH-TR-{index + 1:05d}" if exit_type == "dispatch" else None),
                    warehouse=sku.warehouse,
                    user_uuid=user_uuid,
                )
            )
        session.add_all(exits)
        product_id = int(skus[0].id)
        session.commit()
    return product_id


def _install_isolated_stores(
    root: Path, counts: dict[str, int]
) -> tuple[TinyDB, TinyDB, Any, int]:
    supplier_db = TinyDB(str(root / "suppliers.json"))
    supplier_table = supplier_db.table("suppliers")
    supplier_table.insert_multiple(_supplier_rows(counts["suppliers"]))

    auth_db = TinyDB(str(root / "auth.json"))
    users_table = auth_db.table("users")
    user = UserInDB(
        email="benchmark@example.test",
        hashed_password=hash_password("benchmark-password"),
    )
    users_table.insert(user.model_dump(mode="json"))

    # Replace only module references used by the already-imported application.
    auth_services.users = users_table
    auth_security._users_db = users_table
    suppliers_route.suppliers = supplier_table

    engine = create_engine(
        f"sqlite:///{root / 'inventory.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    product_id = _seed_inventory(engine, counts, user.id)
    def isolated_get_db():
        yield from _session_dependency(engine)

    app.dependency_overrides[database.get_db] = isolated_get_db
    return supplier_db, auth_db, engine, product_id


def _session_dependency(engine: Any):
    with Session(engine) as session:
        yield session


async def _measure(counts: dict[str, int], product_id: int) -> list[dict[str, Any]]:
    capture = TimingCapture()
    timing_logger = logging.getLogger("api.timing")
    timing_logger.addHandler(capture)
    timing_logger.setLevel(logging.INFO)
    variants = [
        ("products", "/inventory/products"),
        ("product_detail", f"/inventory/products/{product_id}"),
        ("orders", "/inventory/orders"),
        ("suppliers", "/suppliers"),
        ("suppliers_filtered", "/suppliers?country=USA"),
        ("suppliers_combined", "/suppliers?country=USA&category=carrier_last_mile"),
    ]
    results: list[dict[str, Any]] = []
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://benchmark.test"
        ) as client:
            login = await client.post(
                "/auth/login",
                data={
                    "username": "benchmark@example.test",
                    "password": "benchmark-password",
                },
            )
            login.raise_for_status()
            token = login.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            for variant, path in variants:
                capture.messages.clear()
                start = time.perf_counter()
                warmup = await client.get(path, headers=headers)
                warmup_wall = (time.perf_counter() - start) * 1000
                warmup.raise_for_status()
                warmup_api = _timing_ms(capture.messages[-1])
                api_values: list[float] = []
                wall_values: list[float] = []
                response_bytes = 0
                for _ in range(10):
                    start = time.perf_counter()
                    response = await client.get(path, headers=headers)
                    wall_values.append((time.perf_counter() - start) * 1000)
                    response.raise_for_status()
                    response_bytes = len(response.content)
                    api_values.append(_timing_ms(capture.messages[-1]))
                results.append(
                    {
                        "endpoint": variant,
                        "path": path.split("?", 1)[0],
                        "response_bytes": response_bytes,
                        "warmup_api_ms": round(warmup_api, 3),
                        "warmup_wall_ms": round(warmup_wall, 3),
                        "api": _stats(api_values),
                        "wall": _stats(wall_values),
                    }
                )
    finally:
        timing_logger.removeHandler(capture)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    counts = PROFILES[args.profile]
    with tempfile.TemporaryDirectory(prefix="trackflow-benchmark-") as temporary_dir:
        try:
            supplier_db, auth_db, engine, product_id = _install_isolated_stores(
                Path(temporary_dir), counts
            )
            try:
                results = asyncio.run(_measure(counts, product_id))
            finally:
                app.dependency_overrides.clear()
                engine.dispose()
                supplier_db.close()
                auth_db.close()
        finally:
            app.dependency_overrides.clear()
    print(
        json.dumps(
            {
                "profile": args.profile,
                "dataset": {
                    **counts,
                    "movements": counts["stock_entries"] + counts["stock_exits"],
                },
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
