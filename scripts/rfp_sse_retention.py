"""Remove a bounded prefix of expired TrackFlow SSE events."""

from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine
from sqlmodel import Session

from pipelines.database_identity import sqlalchemy_postgresql_url
from services.rfp_intake.notifications import purge_event_prefix


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply to the verified DATABASE_URL target")
    parser.add_argument("--days", type=int, default=os.getenv("RFP_SSE_RETENTION_DAYS", "7"))
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    if not args.apply:
        parser.error("verify DATABASE_URL and pass --apply to purge events")
    url = os.getenv("DATABASE_URL", "")
    if not url.startswith(("postgresql://", "postgresql+psycopg2://")):
        parser.error("DATABASE_URL must point to the verified PostgreSQL target")
    engine = create_engine(sqlalchemy_postgresql_url(url), hide_parameters=True)
    try:
        with Session(engine) as session:
            purged = purge_event_prefix(session, days=args.days, limit=args.batch_size)
            session.commit()
    finally:
        engine.dispose()
    print(f"RFP SSE events purged: {purged}")


if __name__ == "__main__":
    main()
