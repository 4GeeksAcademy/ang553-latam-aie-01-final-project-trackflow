"""PostgreSQL/SQLAlchemy persistence operations for incidents."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlmodel import Session

from services.api.access_control import AccessContext
from services.incidents.models import incident_history, incidents


def _visible_clause(access: AccessContext):
    if access.global_access:
        return None
    if not access.allowed_client_ids:
        return incidents.c.client_id.in_([])
    return incidents.c.client_id.in_(access.allowed_client_ids)


def insert_incident(session: Session, values: dict[str, object]) -> dict[str, object]:
    row = session.execute(
        insert(incidents).values(**values).returning(incidents)
    ).mappings().one()
    return dict(row)


def insert_history(session: Session, values: dict[str, object]) -> None:
    session.execute(insert(incident_history).values(**values))


def get_incident(
    session: Session,
    incident_id: str,
    access: AccessContext,
    *,
    for_update: bool = False,
) -> dict[str, object] | None:
    statement = select(incidents).where(incidents.c.incident_id == incident_id)
    visible = _visible_clause(access)
    if visible is not None:
        statement = statement.where(visible)
    if for_update:
        statement = statement.with_for_update()
    row = session.execute(statement).mappings().first()
    return None if row is None else dict(row)


def get_history(session: Session, incident_pk: int) -> list[dict[str, object]]:
    rows = session.execute(
        select(incident_history)
        .where(incident_history.c.incident_pk == incident_pk)
        .order_by(incident_history.c.version)
    ).mappings().all()
    return [dict(row) for row in rows]


def update_incident_status(
    session: Session,
    *,
    incident_pk: int,
    expected_version: int,
    status: str,
    satisfaction_score: int | None,
    actor_user_id: str,
) -> bool:
    now = datetime.now(timezone.utc)
    result = session.execute(
        update(incidents)
        .where(
            incidents.c.id == incident_pk,
            incidents.c.version == expected_version,
        )
        .values(
            status=status,
            satisfaction_score=satisfaction_score,
            updated_by_user_id=actor_user_id,
            updated_at=now,
            version=expected_version + 1,
        )
    )
    return result.rowcount == 1
