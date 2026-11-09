"""Controlled administration of verified clients and tenant memberships."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session

from services.api.access_control import (
    AccessControlError,
    assign_user_to_client,
    build_access_context,
    list_accessible_clients,
    register_verified_client,
)
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.api.auth_services import get_user_by_id
from services.api.database import get_db
from services.incidents.contracts import (
    ClientCreate,
    ClientResponse,
    MembershipCreate,
    MembershipResponse,
)

router = APIRouter(prefix="/api/access", tags=["Tenant access"])


def _require_admin(user: UserInDB) -> None:
    if user.role is not Role.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")


@router.get("/clients", response_model=list[ClientResponse])
def get_clients(
    session: Annotated[Session, Depends(get_db)],
    user: Annotated[UserInDB, Depends(get_current_user)],
) -> list[ClientResponse]:
    return [ClientResponse(**row) for row in list_accessible_clients(session, build_access_context(session, user))]


@router.post("/clients", response_model=ClientResponse, status_code=status.HTTP_201_CREATED)
def create_client(
    payload: ClientCreate,
    session: Annotated[Session, Depends(get_db)],
    user: Annotated[UserInDB, Depends(get_current_user)],
) -> ClientResponse:
    _require_admin(user)
    try:
        row = register_verified_client(
            session,
            client_id=payload.client_id,
            display_name=payload.display_name,
            actor_user_id=user.id,
        )
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Client already registered.") from exc
    except SQLAlchemyError as exc:
        session.rollback()
        raise HTTPException(status_code=503, detail="Tenant registry unavailable.") from exc
    return ClientResponse(**row)


@router.post("/memberships", response_model=MembershipResponse, status_code=status.HTTP_201_CREATED)
def create_membership(
    payload: MembershipCreate,
    session: Annotated[Session, Depends(get_db)],
    user: Annotated[UserInDB, Depends(get_current_user)],
) -> MembershipResponse:
    _require_admin(user)
    target = get_user_by_id(payload.user_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.role is Role.user and payload.can_view_sensitive:
        raise HTTPException(
            status_code=422,
            detail="Sensitive-data access cannot be assigned to role user.",
        )
    try:
        assign_user_to_client(
            session,
            user_id=payload.user_id,
            client_id=payload.client_id,
            can_view_sensitive=payload.can_view_sensitive,
            actor_user_id=user.id,
        )
    except AccessControlError as exc:
        session.rollback()
        detail = "Client not found." if str(exc) == "client not found" else "Membership already exists."
        code = 404 if str(exc) == "client not found" else 409
        raise HTTPException(status_code=code, detail=detail) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Membership could not be created.") from exc
    except SQLAlchemyError as exc:
        session.rollback()
        raise HTTPException(status_code=503, detail="Tenant registry unavailable.") from exc
    return MembershipResponse(**payload.model_dump())
