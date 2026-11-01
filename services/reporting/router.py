from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import create_engine

from pipelines.database_identity import (
    DatabaseIsolationError,
    ensure_distinct_postgresql_databases,
    sqlalchemy_postgresql_url,
)

from pipelines.pipeline import (
    parse_closed_week,
)
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.reporting.models import (
    PipelineRunAcceptedResponse,
    PipelineRunRequest,
    PipelineRunResponse,
    WeeklyPerformanceResponse,
)
from services.reporting.store import latest_pipeline_run, weekly_report
from services.task_queue.store import accept_async_task

REPORTING_DATABASE_URL_ENV = "TRACKFLOW_REPORTING_DATABASE_URL"

router = APIRouter(prefix="/reporting", tags=["reporting"])


def get_reporting_engine(request: Request):
    configured = getattr(request.app.state, "reporting_engine", None)
    if configured is not None:
        return configured
    url = os.getenv(REPORTING_DATABASE_URL_ENV)
    if not url or url == os.getenv("DATABASE_URL"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "reporting database unavailable")
    if not url.startswith(("postgresql://", "postgresql+psycopg2://")):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "reporting requires PostgreSQL")
    return create_engine(sqlalchemy_postgresql_url(url), pool_pre_ping=True)


def get_pipeline_source_engine(
    request: Request,
    reporting_engine: Annotated[Any, Depends(get_reporting_engine)],
):
    configured = getattr(request.app.state, "pipeline_source_engine", None)
    if configured is not None:
        source = configured
    else:
        url = os.getenv("DATABASE_URL")
        if not url or not url.startswith(("postgresql://", "postgresql+psycopg2://")):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "pipeline source database unavailable",
            )
        source = create_engine(sqlalchemy_postgresql_url(url), pool_pre_ping=True)
    try:
        ensure_distinct_postgresql_databases(source, reporting_engine)
    except DatabaseIsolationError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            str(exc),
        ) from exc
    return source


def require_reader(user: Annotated[UserInDB, Depends(get_current_user)]) -> UserInDB:
    if user.role not in {Role.admin, Role.manager}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin or manager role required")
    return user


def require_admin(user: Annotated[UserInDB, Depends(get_current_user)]) -> UserInDB:
    if user.role is not Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return user


def _weekly_json_response(model: WeeklyPerformanceResponse) -> Response:
    """Render Decimal as an exact JSON number token instead of a string."""
    encoded_entries: list[str] = []
    for entry in model.entries:
        data = entry.model_dump(exclude={"discrepancy_rate"})
        prefix = json.dumps(data, separators=(",", ":"), sort_keys=True)[:-1]
        encoded_entries.append(
            f'{prefix},"discrepancy_rate":{format(entry.discrepancy_rate, "f")}}}'
        )
    body = (
        '{"week_start":'
        + json.dumps(model.week_start.isoformat())
        + ',"entries":['
        + ",".join(encoded_entries)
        + "]}"
    )
    return Response(body, media_type="application/json")


@router.get(
    "/weekly-warehouse-client-performance",
    response_model=WeeklyPerformanceResponse,
    responses={404: {"description": "No valid publication exists for the selected week."}},
)
def get_weekly_performance(
    _user: Annotated[UserInDB, Depends(require_reader)],
    engine: Annotated[Any, Depends(get_reporting_engine)],
    week_start: str | None = None,
) -> Response:
    selected = None
    if week_start is not None:
        try:
            selected = parse_closed_week(week_start, now=datetime.now(timezone.utc))
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    with engine.connect() as connection:
        report = weekly_report(connection, selected)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "valid weekly publication not found")
    actual_week, entries = report
    return _weekly_json_response(
        WeeklyPerformanceResponse(week_start=actual_week, entries=entries)
    )


@router.get(
    "/pipeline-runs/latest",
    response_model=PipelineRunResponse,
    responses={404: {"description": "No pipeline run exists."}},
)
def get_latest_run(
    _user: Annotated[UserInDB, Depends(require_reader)],
    engine: Annotated[Any, Depends(get_reporting_engine)],
) -> PipelineRunResponse:
    with engine.connect() as connection:
        row = latest_pipeline_run(connection)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pipeline run not found")
    return PipelineRunResponse(**row)


@router.post(
    "/pipeline-runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PipelineRunAcceptedResponse,
    responses={
        409: {"description": "A run for the week is pending or running."},
        422: {"description": "The selected week is invalid or incomplete."},
        503: {"description": "Durable acceptance or scheduling is unavailable."},
    },
)
def request_pipeline_run(
    payload: PipelineRunRequest,
    user: Annotated[UserInDB, Depends(require_admin)],
    engine: Annotated[Any, Depends(get_reporting_engine)],
) -> PipelineRunAcceptedResponse:
    try:
        week_start = parse_closed_week(
            None if payload.week_start is None else payload.week_start.isoformat(),
            now=datetime.now(timezone.utc),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    try:
        with engine.begin() as connection:
            run_id, task_id, created = accept_async_task(
                connection,
                week_start,
                UUID(user.id),
            )
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "durable acceptance unavailable") from exc
    if not created:
        raise HTTPException(status.HTTP_409_CONFLICT, "pipeline run already pending or running")
    return PipelineRunAcceptedResponse(
        task_id=task_id,
        run_id=run_id,
        week_start=week_start,
        status_url=f"/tasks/{task_id}",
    )
