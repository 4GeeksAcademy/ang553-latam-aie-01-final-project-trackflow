"""Authenticated HTTP session surface and native FastAPI WebSocket chat."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID
from weakref import WeakValueDictionary

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from services.api.access_control import AccessContext, build_access_context
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import (
    ExpiringAccessSession,
    get_current_user,
    get_expiring_access_session,
)
from services.api.chat.contracts import (
    AuthenticateEvent,
    CLIENT_EVENT_ADAPTER,
    ChatSessionResponse,
    CoverageUpdatedEvent,
    CreateChatSessionRequest,
    ErrorData,
    ErrorEvent,
    GenerationProgressEvent,
    InterruptRequestedEvent,
    SessionSnapshotEvent,
    TokenChunkEvent,
    UserMessageEvent,
)
from services.api.chat.coordinator import ChatCoordinator, SQLGenerationRepository
from services.api.chat.producer import authentic_support_producer, unavailable_producer
from services.api.chat.pubsub import SessionPubSub, SlowSubscriber, Subscription
from services.api.chat.qa_units_scope import qa_unit_scope
from services.api.chat.store import (
    ChatForbidden,
    ChatNotFound,
    chat_snapshot,
    create_chat_session,
    get_chat_session,
    qa_coverage_is_current,
    qa_unit_is_current,
    redact_revoked_units,
)
from services.api.database import get_db, open_db_session
from services.api.agent_memory.service import MemoryError, MemoryStore
from services.api.support_oidc import (
    SupportIdentity,
    authenticate_support_identity,
    require_support_reader,
)


router = APIRouter(prefix="/chat", tags=["Customer Support Chat"])
_PUBSUB = SessionPubSub(queue_size=64)
_COORDINATORS: WeakValueDictionary[tuple[str, str], ChatCoordinator] = WeakValueDictionary()
_COORDINATORS_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class _LocalSocketIdentity:
    session: ExpiringAccessSession
    access: AccessContext

    @property
    def user_id(self) -> str:
        return self.session.user_id

    def revalidate(self, session_id: str) -> AccessContext:
        user = self.session.current_user()
        if user.role is not self.access.role:
            raise HTTPException(status_code=401)
        with open_db_session() as db:
            refreshed = build_access_context(db, user)
            get_chat_session(db, refreshed, session_id)
        return refreshed


def _local_identity(token: str) -> _LocalSocketIdentity:
    session = get_expiring_access_session(token)
    user = session.current_user()
    if user.role not in {Role.admin, Role.manager}:
        raise HTTPException(status_code=403)
    with open_db_session() as db:
        access = build_access_context(db, user)
    return _LocalSocketIdentity(session, access)


def _support_identity(token: str, local_user_id: str) -> SupportIdentity:
    identity = authenticate_support_identity(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    )
    require_support_reader(identity)
    if identity.user.id != local_user_id:
        raise HTTPException(status_code=403)
    return identity


def _http_access(db: Session, user: UserInDB) -> AccessContext:
    if user.role not in {Role.admin, Role.manager}:
        raise HTTPException(status_code=403, detail="admin or manager role required")
    return build_access_context(db, user)


@router.post("/sessions", response_model=ChatSessionResponse, status_code=201)
def create_session(
    payload: CreateChatSessionRequest,
    user: Annotated[UserInDB, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ChatSessionResponse:
    try:
        access = _http_access(db, user)
        created = create_chat_session(
            db, access, client_id=payload.client_id, commit=False
        )
        MemoryStore().bind_conversation(
            db, UUID(created.session_id), access, create=True, commit=False
        )
        db.commit()
        return created
    except ChatForbidden:
        raise HTTPException(status_code=403, detail="chat client access is not authorized") from None
    except (MemoryError, SQLAlchemyError, ValueError):
        db.rollback()
        raise HTTPException(status_code=503, detail="chat service unavailable") from None


@router.get("/sessions/{session_id}", response_model=SessionSnapshotEvent)
def get_session_snapshot(
    session_id: str,
    user: Annotated[UserInDB, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SessionSnapshotEvent:
    try:
        return SessionSnapshotEvent(data=chat_snapshot(db, _http_access(db, user), session_id))
    except ChatNotFound:
        raise HTTPException(status_code=404, detail="chat session not found") from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="chat service unavailable") from None


async def _coordinator(access: AccessContext, session_id: str) -> ChatCoordinator:
    key = (access.user_id, session_id)
    async with _COORDINATORS_LOCK:
        coordinator = _COORDINATORS.get(key)
        if coordinator is None:
            coordinator = ChatCoordinator(SQLGenerationRepository(access), _PUBSUB)
            _COORDINATORS[key] = coordinator
        return coordinator


async def _send_error(websocket: WebSocket, session_id: str | None, code: str) -> None:
    await websocket.send_json(
        ErrorEvent(data=ErrorData(session_id=session_id, code=code)).model_dump(mode="json")
    )


async def _has_current_support_identity(
    local: _LocalSocketIdentity, support_token: str | None,
) -> bool:
    if support_token is None:
        return False
    try:
        await asyncio.to_thread(_support_identity, support_token, local.user_id)
    except Exception:
        return False
    return True


def _is_single_client_manager(access: AccessContext) -> bool:
    # The caller already confirmed that the owner can access this session's client.
    return (
        access.role is Role.manager
        and not access.global_access
        and len(access.allowed_client_ids) == 1
    )


async def _sender(
    websocket: WebSocket,
    subscription: Subscription,
    *,
    after_cursor: int,
    local: _LocalSocketIdentity,
    session_id: str,
    support_token: str | None = None,
    progress_coordinator: ChatCoordinator | None = None,
    after_progress: int = 0,
) -> None:
    while True:
        event = await subscription.receive()
        if isinstance(event, GenerationProgressEvent):
            if progress_coordinator is None or event.data.cursor <= after_progress:
                continue
            await asyncio.to_thread(local.revalidate, session_id)
            if event.data != await progress_coordinator.current_progress(session_id):
                continue
            after_progress = event.data.cursor
            await websocket.send_json(event.model_dump(mode="json"))
            continue
        cursor = getattr(event.data, "cursor", None)
        if cursor is not None and cursor <= after_cursor:
            continue
        if cursor is not None:
            after_cursor = cursor
        # Events may contain support history or generated content. Refresh the
        # local token, active-user status and tenant membership immediately
        # before each disclosure, including for otherwise passive observers.
        has_knowledge = isinstance(event, TokenChunkEvent) and event.data.knowledge is not None
        has_qa_unit = isinstance(event, TokenChunkEvent) and event.data.publication_kind == "authorized_unit"
        has_coverage = isinstance(event, CoverageUpdatedEvent) or (
            isinstance(event, TokenChunkEvent) and event.data.unit_coverage is not None
        )
        support_authorized = (has_knowledge or has_qa_unit or has_coverage) and await _has_current_support_identity(
            local, support_token,
        )
        access = await asyncio.to_thread(local.revalidate, session_id)
        if has_qa_unit or has_coverage:
            scope = qa_unit_scope()
            if not (support_authorized and scope is not None and scope.allows(access)):
                raise HTTPException(status_code=403)
        if has_coverage:
            with open_db_session() as db:
                if not qa_coverage_is_current(db, access, session_id, event.data.message_id, event.data.cursor):
                    raise HTTPException(status_code=403)
        if has_qa_unit:
            if (ref := event.data.unit_ref) is not None:
                with open_db_session() as db:
                    if not qa_unit_is_current(
                        db, access, session_id, ref, event.data.token, event.data.sequence,
                    ):
                        raise HTTPException(status_code=403)
        payload = event.model_dump(mode="json")
        if has_knowledge and not (support_authorized and _is_single_client_manager(access)):
            payload["data"].pop("knowledge", None)
        await websocket.send_json(payload)


@router.websocket("/ws")
async def chat_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    subscription: Subscription | None = None
    try:
        try:
            raw_auth = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            auth = AuthenticateEvent.model_validate(raw_auth)
        except (asyncio.TimeoutError, ValidationError, ValueError, TypeError):
            await websocket.close(code=4401, reason="authentication required")
            return
        try:
            local = await asyncio.to_thread(_local_identity, auth.data.access_token)
        except HTTPException as exc:
            await websocket.close(
                code=4403 if exc.status_code == 403 else 4401,
                reason="support access denied" if exc.status_code == 403 else "authentication required",
            )
            return

        support_token = auth.data.support_token
        if support_token is not None:
            try:
                await asyncio.to_thread(
                    _support_identity, support_token, local.user_id
                )
            except Exception:
                await websocket.close(code=4403, reason="support authorization denied")
                return

        try:
            # Subscribe first. The snapshot watermark then lets the sender
            # discard queued events already represented by that snapshot.
            subscription = await _PUBSUB.subscribe(auth.data.session_id)
            refreshed_access = await asyncio.to_thread(
                local.revalidate, auth.data.session_id
            )
            coordinator = await _coordinator(refreshed_access, auth.data.session_id)
            await coordinator.reconcile_after_restart(auth.data.session_id)
            with open_db_session() as db:
                snapshot = chat_snapshot(
                    db, refreshed_access, auth.data.session_id,
                    include_knowledge=support_token is not None,
                    include_authorized_units=support_token is not None,
                )
            if auth.data.progress_events:
                snapshot.progress = await coordinator.current_progress(
                    auth.data.session_id,
                    streaming_messages={
                        message.message_id for message in snapshot.messages if message.status == "streaming"
                    },
                )
        except ChatNotFound:
            await websocket.close(code=4404, reason="chat session not found")
            return
        except HTTPException as exc:
            await websocket.close(
                code=4403 if exc.status_code == 403 else 4401,
                reason="support access denied" if exc.status_code == 403 else "authentication expired",
            )
            return
        except SQLAlchemyError:
            await websocket.close(code=1013, reason="chat service unavailable")
            return

        if any(
            message.knowledge is not None or message.publication_kind or message.unit_coverage is not None
            for message in snapshot.messages
        ):
            support_authorized = await _has_current_support_identity(local, support_token)
            try:
                refreshed_access = await asyncio.to_thread(local.revalidate, auth.data.session_id)
            except (HTTPException, ChatNotFound):
                await websocket.close(code=4401, reason="authentication expired")
                return
            if not (support_authorized and _is_single_client_manager(refreshed_access)):
                for message in snapshot.messages:
                    message.knowledge = None
            scope = qa_unit_scope()
            if not (support_authorized and scope is not None and scope.allows(refreshed_access)):
                for message in snapshot.messages:
                    message.unit_coverage = None
                    if message.publication_kind == "authorized_unit":
                        message.content = ""
                        message.knowledge = None
                        message.publication_kind = None
            else:
                try:
                    with open_db_session() as db:
                        redact_revoked_units(db, refreshed_access, snapshot)
                except SQLAlchemyError:
                    await websocket.close(code=1013, reason="chat service unavailable")
                    return
        await websocket.send_json(SessionSnapshotEvent(data=snapshot).model_dump(mode="json"))
        sender = asyncio.create_task(
            _sender(
                websocket,
                subscription,
                after_cursor=snapshot.cursor,
                progress_coordinator=coordinator if auth.data.progress_events else None,
                after_progress=snapshot.progress.cursor if snapshot.progress is not None else 0,
                local=local,
                session_id=auth.data.session_id,
                support_token=support_token,
            )
        )
        incoming: asyncio.Task | None = None
        try:
            while True:
                incoming = asyncio.create_task(websocket.receive_json())
                done, _ = await asyncio.wait(
                    {incoming, sender}, return_when=asyncio.FIRST_COMPLETED
                )
                if sender in done:
                    incoming.cancel()
                    await asyncio.gather(incoming, return_exceptions=True)
                    try:
                        sender.result()
                    except SlowSubscriber:
                        await websocket.close(code=1013, reason="subscriber too slow")
                    except (HTTPException, ChatNotFound):
                        await websocket.close(code=4401, reason="authentication expired")
                    except SQLAlchemyError:
                        await websocket.close(code=1013, reason="chat service unavailable")
                    return
                try:
                    raw = incoming.result()
                except (ValueError, TypeError):
                    await _send_error(websocket, auth.data.session_id, "invalid_event")
                    continue
                try:
                    refreshed_access = await asyncio.to_thread(
                        local.revalidate, auth.data.session_id
                    )
                    event = CLIENT_EVENT_ADAPTER.validate_python(raw)
                except (HTTPException, ChatNotFound):
                    await websocket.close(code=4401, reason="authentication expired")
                    return
                except ValidationError:
                    await _send_error(websocket, auth.data.session_id, "invalid_event")
                    continue
                if isinstance(event, AuthenticateEvent) or event.data.session_id != auth.data.session_id:
                    await _send_error(websocket, auth.data.session_id, "invalid_event")
                    continue
                if isinstance(event, UserMessageEvent):
                    if support_token is None:
                        await _send_error(
                            websocket, auth.data.session_id, "support_authorization_required"
                        )
                        continue
                    try:
                        support_identity = await asyncio.to_thread(
                            _support_identity, support_token, local.user_id
                        )
                    except Exception:
                        await _send_error(
                            websocket, auth.data.session_id, "support_authorization_required"
                        )
                        continue
                    coordinator = await _coordinator(
                        refreshed_access, auth.data.session_id
                    )
                    started = await coordinator.start(
                        auth.data.session_id,
                        event.data.text,
                        authentic_support_producer(support_identity),
                    )
                    if not started:
                        await _send_error(websocket, auth.data.session_id, "generation_active")
                elif isinstance(event, InterruptRequestedEvent):
                    redirect = event.data.new_input
                    redirect_authorized = redirect is None
                    if redirect is not None and support_token is not None:
                        try:
                            support_identity = await asyncio.to_thread(
                                _support_identity, support_token, local.user_id
                            )
                            redirect_authorized = True
                        except Exception:
                            redirect_authorized = False
                    coordinator = await _coordinator(
                        refreshed_access, auth.data.session_id
                    )
                    if redirect is not None and not redirect_authorized:
                        interrupted = await coordinator.interrupt(
                            auth.data.session_id, None, unavailable_producer
                        )
                        await _send_error(
                            websocket, auth.data.session_id, "support_authorization_required"
                        )
                    else:
                        redirect_producer = (
                            authentic_support_producer(support_identity)
                            if redirect is not None and redirect_authorized
                            else unavailable_producer
                        )
                        interrupted = await coordinator.interrupt(
                            auth.data.session_id, redirect, redirect_producer
                        )
                    if not interrupted:
                        # Duplicate and late interrupts are idempotent no-ops.
                        continue
        finally:
            # Disconnect and ASGI cancellation can arrive together. Finish
            # draining the connection's tasks without affecting its producer.
            with CancelScope(shield=True):
                sender.cancel()
                if incoming is None:
                    await asyncio.gather(sender, return_exceptions=True)
                else:
                    incoming.cancel()
                    await asyncio.gather(sender, incoming, return_exceptions=True)
    except (WebSocketDisconnect, SlowSubscriber, asyncio.CancelledError):
        return
    finally:
        if subscription is not None:
            with CancelScope(shield=True):
                await _PUBSUB.unsubscribe(subscription)
