"""Seed disposable TrackFlow API state for opt-in MCP integration QA."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import insert

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


VISIBLE_PRODUCT_ID = 91001
HIDDEN_PRODUCT_ID = 91002
VISIBLE_CLIENT_ID = "7f6e3d21-8c4a-4b92-a1d7-5e9f2c6b1048"
HIDDEN_CLIENT_ID = "2a9b7c41-6d8e-4f03-b5c2-9a1e7d4f8260"


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Required QA environment variable is missing: {name}")
    return value


def main() -> None:
    if os.getenv("TRACKFLOW_MCP_QA") != "1":
        raise SystemExit("Refusing to seed without TRACKFLOW_MCP_QA=1")
    database_url = required("DATABASE_URL")
    parsed = urlparse(database_url.replace("postgresql+psycopg2", "postgresql", 1))
    if parsed.hostname != "127.0.0.1" or parsed.port != 55440:
        raise SystemExit("QA database must be the isolated loopback PostgreSQL instance")
    auth_path = Path(required("TRACKFLOW_AUTH_DATABASE_PATH")).resolve()
    if not auth_path.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise SystemExit("QA TinyDB must live under the operating-system temp directory")

    user_id = required("QA_TRACKFLOW_USER_ID")
    local_password = required("QA_TRACKFLOW_LOCAL_PASSWORD")

    from services.api.auth_database import users
    from services.api.auth_models import Role, UserInDB
    from services.api.auth_security import hash_password
    from services.api.database import create_db_and_tables, open_db_session
    from services.api.inventory_models import SKU, StockEntry
    from services.incidents.models import clients, user_client_memberships

    create_db_and_tables()
    users.insert(
        UserInDB(
            id=user_id,
            email="mcp-qa-user@example.invalid",
            hashed_password=hash_password(local_password),
            is_active=True,
            role=Role.manager,
        ).model_dump(mode="json")
    )

    with open_db_session() as session:
        session.execute(
            insert(clients),
            [
                {
                    "client_id": VISIBLE_CLIENT_ID,
                    "display_name": "PureStep Footwear",
                    "is_active": True,
                    "created_by_user_id": user_id,
                },
                {
                    "client_id": HIDDEN_CLIENT_ID,
                    "display_name": "SoundWave Electronics",
                    "is_active": True,
                    "created_by_user_id": user_id,
                },
            ],
        )
        session.execute(
            insert(user_client_memberships).values(
                user_id=user_id,
                client_id=VISIBLE_CLIENT_ID,
                can_view_sensitive=True,
                assigned_by_user_id=user_id,
            )
        )
        session.add_all(
            [
                SKU(
                    id=VISIBLE_PRODUCT_ID,
                    name="Visible QA Product",
                    sku="MCP-QA-VISIBLE",
                    client_id=VISIBLE_CLIENT_ID,
                    client_name="PureStep Footwear",
                    category="fashion",
                    warehouse="LA",
                    minimum_stock_threshold=2,
                ),
                SKU(
                    id=HIDDEN_PRODUCT_ID,
                    name="Hidden QA Product",
                    sku="MCP-QA-HIDDEN",
                    client_id=HIDDEN_CLIENT_ID,
                    client_name="SoundWave Electronics",
                    category="electronics",
                    warehouse="ZGZ",
                    minimum_stock_threshold=5,
                ),
            ]
        )
        session.add_all(
            [
                StockEntry(
                    sku_id=VISIBLE_PRODUCT_ID,
                    quantity=7,
                    reference="MCP-QA-VISIBLE-STOCK",
                    warehouse="LA",
                    user_uuid=user_id,
                ),
                StockEntry(
                    sku_id=HIDDEN_PRODUCT_ID,
                    quantity=11,
                    reference="MCP-QA-HIDDEN-STOCK",
                    warehouse="ZGZ",
                    user_uuid=user_id,
                ),
            ]
        )
        session.commit()

    print("Disposable TrackFlow QA state seeded.")


if __name__ == "__main__":
    main()
