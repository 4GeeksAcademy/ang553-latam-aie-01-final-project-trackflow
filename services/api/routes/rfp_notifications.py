"""Authenticated SSE and the minimum intake read surface for Sales."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
import re
import time
from typing import Annotated
from uuid import UUID

from anyio import CapacityLimiter, to_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from services.api.auth_database import AuthStoreUnavailable
from services.api.auth_models import Role
from services.api.auth_security import ExpiringAccessSession, get_expiring_access_session
from services.api.database import open_db_session
from services.api.rfp_approval_auth import verified_local_context
from services.rfp_approval.identity import IdentityResolutionDenied, resolve_authenticated_identity
from services.rfp_intake.intake_events import EVENT_TYPE, RFPTicketCreated
from services.rfp_intake.notifications import (
    CommercialRFPSnapshot, CommercialRFPTicket, CursorResetRequired, commercial_grant_active,
    commercial_snapshot, commercial_ticket, read_event_batch,
)


router = APIRouter(prefix="/notifications")
AccessSession = Annotated[ExpiringAccessSession, Depends(get_expiring_access_session)]
_POLL_SECONDS = 1.0
_HEARTBEAT_SECONDS = 10.0
_BATCH_LIMIT = 25
_DB_LIMITER = CapacityLimiter(8)
_STREAM_HEADERS = {"Cache-Control": "no-cache, no-store, no-transform", "X-Accel-Buffering": "no"}


@contextmanager
def _authorized_read(access: ExpiringAccessSession) -> Iterator[Session]:
    try:
        user = access.current_user()
        with open_db_session() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '3s'"))
            if user.role is not Role.admin:
                try:
                    identity = resolve_authenticated_identity(session, context=verified_local_context(user))
                except IdentityResolutionDenied:
                    raise HTTPException(status_code=403, detail="RFP commercial read is not authorized") from None
                if not commercial_grant_active(session, identity):
                    raise HTTPException(status_code=403, detail="RFP commercial read is not authorized")
            yield session
    except AuthStoreUnavailable:
        raise HTTPException(status_code=503, detail="Authentication storage is temporarily unavailable") from None
    except (SQLAlchemyError, ValidationError):
        raise HTTPException(status_code=503, detail="RFP notifications are temporarily unavailable") from None


@router.get("/snapshot", response_model=CommercialRFPSnapshot)
def get_notifications_snapshot(
    access: AccessSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CommercialRFPSnapshot:
    with _authorized_read(access) as session:
        return commercial_snapshot(session, limit=limit, offset=offset)


def _parse_cursor(raw: str | None) -> int:
    if raw is None or re.fullmatch(r"0|[1-9][0-9]{0,18}", raw) is None:
        raise CursorResetRequired
    cursor = int(raw)
    if cursor >= 2**63:
        raise CursorResetRequired
    return cursor


def _read_batch(
    access: ExpiringAccessSession, raw_cursor: str | None,
) -> tuple[int, list[tuple[int, RFPTicketCreated]]]:
    with _authorized_read(access) as session:
        cursor = _parse_cursor(raw_cursor)
        return cursor, read_event_batch(session, after=cursor, limit=_BATCH_LIMIT)


async def _stream_frames(
    access: ExpiringAccessSession,
    after: int,
    batch: list[tuple[int, RFPTicketCreated]],
) -> AsyncIterator[str]:
    next_heartbeat = 0.0
    while time.time() < access.expires_at:
        if time.monotonic() >= next_heartbeat:
            yield ": keep-alive\n\n"
            next_heartbeat = time.monotonic() + _HEARTBEAT_SECONDS
        for cursor, payload in batch:
            if time.time() >= access.expires_at:
                return
            yield f"id: {cursor}\nevent: {EVENT_TYPE}\ndata: {payload.model_dump_json()}\n\n"
            after = cursor
        # Drain a full batch immediately. SQL sessions are closed before
        # yielding partial frames or sleeping on an idle connection.
        if len(batch) < _BATCH_LIMIT:
            remaining = access.expires_at - time.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(_POLL_SECONDS, remaining))
        try:
            _, batch = await to_thread.run_sync(_read_batch, access, str(after), limiter=_DB_LIMITER)
        except (HTTPException, CursorResetRequired):
            # Headers have already been sent. Reconnection reports the
            # current 401, 403, 409 or 503 as an ordinary HTTP response.
            return


@router.get(
    "/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def stream_notifications(
    access: AccessSession,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    try:
        cursor, batch = await to_thread.run_sync(_read_batch, access, last_event_id, limiter=_DB_LIMITER)
    except CursorResetRequired:
        raise HTTPException(status_code=409, detail={"code": "rfp_cursor_reset"}) from None
    return StreamingResponse(
        _stream_frames(access, cursor, batch), media_type="text/event-stream", headers=_STREAM_HEADERS,
    )


@router.get("/{ticket_id}", response_model=CommercialRFPTicket)
def get_notifications_ticket(ticket_id: UUID, access: AccessSession) -> CommercialRFPTicket:
    with _authorized_read(access) as session:
        ticket = commercial_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="RFP ticket not found")
    return ticket
