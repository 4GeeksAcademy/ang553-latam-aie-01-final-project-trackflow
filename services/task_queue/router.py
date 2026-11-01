"""Public durable task status API for DEV-55."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.reporting.models import AsyncTaskResponse
from services.reporting.router import get_reporting_engine
from services.task_queue.store import get_async_task


router = APIRouter(prefix="/tasks", tags=["tasks"])

_PENDING_STATES = frozenset({"accepted", "queued", "retry_wait"})
_STARTED_STATES = frozenset({"claimed", "running"})
_SUCCESS_STATES = frozenset({"succeeded"})


def require_task_admin(
    user: Annotated[UserInDB, Depends(get_current_user)],
) -> UserInDB:
    if user.role is not Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return user


def public_task_status(execution_state: str) -> str:
    """Project internal state onto the four stable HTTP states."""
    if execution_state in _PENDING_STATES:
        return "pending"
    if execution_state in _STARTED_STATES:
        return "started"
    if execution_state in _SUCCESS_STATES:
        return "success"
    return "failure"


@router.get(
    "/{task_id}",
    response_model=AsyncTaskResponse,
    responses={
        401: {"description": "Authentication required."},
        403: {"description": "Administrator role required."},
        404: {"description": "Durable task not found."},
    },
)
def get_task(
    task_id: UUID,
    _user: Annotated[UserInDB, Depends(require_task_admin)],
    engine: Annotated[Any, Depends(get_reporting_engine)],
) -> AsyncTaskResponse:
    with engine.connect() as connection:
        row = get_async_task(connection, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return AsyncTaskResponse(
        task_id=row["task_id"],
        run_id=row["run_id"],
        task_name=row["task_name"],
        week_start=row["week_start"],
        status=public_task_status(row["execution_state"]),
        celery_state=row["celery_state"],
        execution_state=row["execution_state"],
        dispatch_status=row["dispatch_status"],
        pipeline_status=row["pipeline_status"],
        attempt_count=row["attempt_count"],
        result=row["result_summary"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        published=row["published"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
