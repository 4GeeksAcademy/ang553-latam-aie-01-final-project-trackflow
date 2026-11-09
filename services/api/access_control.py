"""Shared tenant authorization for HTTP routes and future in-process tools."""

from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import insert, select
from sqlmodel import Session

from services.api.auth_models import Role, UserInDB
from services.incidents.models import clients, user_client_memberships


@dataclass(frozen=True)
class AccessContext:
    user_id: str
    role: Role
    allowed_client_ids: frozenset[str]
    sensitive_client_ids: frozenset[str]
    global_access: bool = False

    @property
    def cache_scope(self) -> str:
        if self.global_access:
            return "global"
        return "clients:" + ",".join(sorted(self.allowed_client_ids))

    def can_access_client(self, client_id: str | None) -> bool:
        return bool(client_id) and (
            self.global_access or client_id in self.allowed_client_ids
        )

    def can_view_sensitive(self, client_id: str) -> bool:
        if self.role is Role.user:
            return False
        return self.global_access or client_id in self.sensitive_client_ids


class AccessControlError(RuntimeError):
    pass


def build_access_context(session: Session, user: UserInDB) -> AccessContext:
    if user.role is Role.admin:
        return AccessContext(
            user_id=user.id,
            role=user.role,
            allowed_client_ids=frozenset(),
            sensitive_client_ids=frozenset(),
            global_access=True,
        )
    rows = session.execute(
        select(
            user_client_memberships.c.client_id,
            user_client_memberships.c.can_view_sensitive,
        )
        .join(clients, clients.c.client_id == user_client_memberships.c.client_id)
        .where(
            user_client_memberships.c.user_id == user.id,
            clients.c.is_active.is_(True),
        )
    ).all()
    allowed = frozenset(str(row.client_id) for row in rows)
    sensitive = frozenset(
        str(row.client_id)
        for row in rows
        if user.role is Role.manager and row.can_view_sensitive
    )
    return AccessContext(user.id, user.role, allowed, sensitive)


def register_verified_client(
    session: Session, *, client_id: str, display_name: str, actor_user_id: str
) -> dict[str, object]:
    session.execute(
        insert(clients).values(
            client_id=client_id,
            display_name=display_name.strip(),
            is_active=True,
            created_by_user_id=actor_user_id,
        )
    )
    session.commit()
    return {"client_id": client_id, "display_name": display_name.strip(), "is_active": True}


def assign_user_to_client(
    session: Session,
    *,
    user_id: str,
    client_id: str,
    can_view_sensitive: bool,
    actor_user_id: str,
) -> None:
    client = session.execute(
        select(clients.c.client_id).where(
            clients.c.client_id == client_id,
            clients.c.is_active.is_(True),
        )
    ).first()
    if client is None:
        raise AccessControlError("client not found")
    existing = session.execute(
        select(user_client_memberships.c.user_id).where(
            user_client_memberships.c.user_id == user_id,
            user_client_memberships.c.client_id == client_id,
        )
    ).first()
    if existing is not None:
        raise AccessControlError("membership already exists")
    session.execute(
        insert(user_client_memberships).values(
            user_id=user_id,
            client_id=client_id,
            can_view_sensitive=can_view_sensitive,
            assigned_by_user_id=actor_user_id,
        )
    )
    session.commit()


def list_accessible_clients(session: Session, access: AccessContext) -> list[dict[str, object]]:
    statement = select(clients.c.client_id, clients.c.display_name).where(
        clients.c.is_active.is_(True)
    )
    if not access.global_access:
        if not access.allowed_client_ids:
            return []
        statement = statement.where(clients.c.client_id.in_(access.allowed_client_ids))
    rows = session.execute(statement.order_by(clients.c.display_name)).all()
    return [
        {"client_id": str(row.client_id), "display_name": row.display_name}
        for row in rows
    ]
