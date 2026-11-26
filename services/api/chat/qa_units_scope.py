"""Explicit QA scope for the synthetic authorized-unit turn."""

from dataclasses import dataclass
import os
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlmodel import Session

from services.api.access_control import AccessContext
from services.api.auth_models import Role
from services.api.chat.models import chat_sessions
from services.incidents.models import clients, user_client_memberships


@dataclass(frozen=True)
class QaUnitScope:
    user_id: str
    client_id: str

    def allows(self, access: AccessContext) -> bool:
        return (
            self.user_id == access.user_id
            and access.role is Role.manager
            and not access.global_access
            and access.allowed_client_ids == frozenset({self.client_id})
        )


def qa_unit_scope() -> QaUnitScope | None:
    if os.getenv("TRACKFLOW_MCP_QA") != "1" or not any(
        os.getenv(flag) == "1" for flag in ("TRACKFLOW_I13_QA_UNITS", "TRACKFLOW_I14_QA_UNITS")
    ):
        return None
    user_id = os.getenv("TRACKFLOW_I13_QA_UNITS_USER_ID", "")
    client_id = os.getenv("TRACKFLOW_I13_QA_UNITS_CLIENT_ID", "")
    try:
        if (str(UUID(user_id)), str(UUID(client_id))) != (user_id, client_id):
            return None
        url = make_url(os.getenv("DATABASE_URL", ""))
    except (ValueError, ArgumentError):
        return None
    if (
        url.get_backend_name() != "postgresql"
        or url.host != "127.0.0.1"
        or url.port != 55440
        or url.database != "trackflow_ws_qa"
        or url.username != "trackflow_ws_qa"
        or url.query
    ):
        return None
    return QaUnitScope(user_id, client_id)


def can_store_qa_unit(db: Session, session_id: str) -> bool:
    """Check the configured QA owner and lock authorization through commit."""
    scope = qa_unit_scope()
    if scope is None:
        return False
    identity = db.execute(text("SELECT current_database(), current_user")).one()
    if tuple(identity) != ("trackflow_ws_qa", "trackflow_ws_qa"):
        return False
    session = db.execute(
        select(chat_sessions.c.session_id)
        .where(
            chat_sessions.c.session_id == session_id,
            chat_sessions.c.user_id == scope.user_id,
            chat_sessions.c.client_id == scope.client_id,
            chat_sessions.c.status != "closed",
        )
        .with_for_update(read=True)
    ).scalar_one_or_none()
    if session is None:
        return False
    client_ids = db.execute(
        select(user_client_memberships.c.client_id)
        .join(clients, clients.c.client_id == user_client_memberships.c.client_id)
        .where(user_client_memberships.c.user_id == scope.user_id, clients.c.is_active.is_(True))
        .with_for_update(read=True)
    ).scalars()
    return frozenset(client_ids) == frozenset({scope.client_id})
