"""Isolated QA app for consent-bound external tracking. Never mounted by the API."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from contextlib import AbstractContextManager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
import jwt
from anyio import CancelScope
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Column, DateTime, MetaData, String, Table, func, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from agents.customer_support.external_graph import route_support_query
from agents.customer_support.guardrail_contracts import DispatchState
from agents.customer_support.tools import ToolStatus, TrackingLookupResult
from services.api.access_control import AccessContext
from services.api.auth_models import Role
from services.api.chat.contracts import (
    CLIENT_EVENT_ADAPTER,
    AuthenticateEvent,
    ErrorData,
    ErrorEvent,
    InterruptRequestedEvent,
    SessionSnapshotEvent,
    UserMessageEvent,
)
from services.api.chat.coordinator import ChatCoordinator, SQLGenerationRepository
from services.api.chat.external_agent_qa import external_tracking_answer
from services.api.chat.models import chat_generations, chat_messages
from services.api.chat.pubsub import SessionPubSub, SlowSubscriber
from services.api.chat.store import (
    ChatForbidden,
    ChatNotFound,
    GenerationConflict,
    append_chunk,
    begin_generation,
    chat_snapshot,
    complete_generation,
    create_chat_session,
    fail_generation,
    get_chat_session,
    interrupt_generation,
    reconcile_orphaned_generation,
)
from services.api.inventory_models import CommercialOrder, StockExit
from services.api.inventory_service import lookup_authorized_tracking
from services.incidents.models import clients


QA_AUDIENCE = "trackflow-i13-tracking-qa"
QA_PURPOSE = "i13-external-tracking"
QA_SCOPE = "tracking:read"
QA_REALM = "/realms/trackflow-i13-external"
MCP_TOOL = "get_authorized_tracking"
MCP_ORIGIN = "http://127.0.0.1:18080"
STANDARD_SCOPES = frozenset({"openid", "profile", "email", "basic"})
DENIED = "No encontré un despacho autorizado."
FOUND = "Hay un despacho registrado en TrackFlow."

external_tracking_qa_metadata = MetaData()
tracking_grants = Table(
    "qa_i13_tracking_grant", external_tracking_qa_metadata,
    Column("issuer", String, primary_key=True),
    Column("subject", String, primary_key=True),
    Column("owner_id", String(64), nullable=False, unique=True),
    Column("kind", String(16), nullable=False),
    Column("client_id", String(36), nullable=False),
    Column("dispatch_id", StockExit.__table__.c.dispatch_id.type),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
)
tracking_sessions = Table(
    "qa_i13_tracking_session", external_tracking_qa_metadata,
    Column("session_id", String(64), primary_key=True),
    Column("issuer", String, nullable=False),
    Column("subject", String, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("dispatch_id", StockExit.__table__.c.dispatch_id.type),
)


@dataclass(frozen=True)
class TrackingQASettings:
    issuer: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.issuer)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.path != QA_REALM
            or parsed.query or parsed.fragment or parsed.username or parsed.password
        ):
            raise ValueError("external tracking requires a dedicated loopback QA realm")


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str


class ScopeDenied(RuntimeError):
    pass


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrackingProjection(BaseModel):
    status: Literal["found", "not_found"]
    dispatch_state: Literal["recorded_dispatch"] | None


def _authenticate(authorization, settings, resolve_key) -> Principal:
    if not authorization or not re.fullmatch(r"Bearer [^\s]+", authorization):
        raise ScopeDenied
    token = authorization.split()[1]
    if len(token) > 8192:
        raise ScopeDenied
    try:
        if jwt.get_unverified_header(token).get("alg") != "RS256":
            raise ScopeDenied
        claims = jwt.decode(
            token, resolve_key(token), algorithms=["RS256"], audience=QA_AUDIENCE,
            issuer=settings.issuer,
            options={"require": ["iat", "exp", "sub", "iss", "aud", "azp", "scope", "typ", "trackflow_purpose"]},
        )
    except jwt.PyJWTError:
        raise ScopeDenied from None
    scope = claims["scope"]
    audience = claims["aud"]
    if (
        not isinstance(scope, str) or frozenset(scope.split()) - STANDARD_SCOPES != {QA_SCOPE}
        or audience not in (QA_AUDIENCE, [QA_AUDIENCE])
        or claims["azp"] != QA_AUDIENCE or claims["typ"] != "Bearer"
        or claims["trackflow_purpose"] != QA_PURPOSE
        or not isinstance(claims["sub"], str) or not claims["sub"].strip()
        or len(claims["sub"]) > 255
    ):
        raise ScopeDenied
    return Principal(settings.issuer, claims["sub"])


def _resolve_grant(db, principal: Principal, session_id=None, *, lock=False):
    grants = tracking_grants.c
    statement = select(tracking_grants).join(clients, clients.c.client_id == grants.client_id).where(
        grants.issuer == principal.issuer,
        grants.subject == principal.subject,
        grants.revoked_at.is_(None),
        grants.expires_at > func.clock_timestamp(),
        clients.c.is_active.is_(True),
    )
    if lock:
        statement = statement.with_for_update(of=(tracking_grants, clients))
    grant = db.execute(statement).mappings().one_or_none()
    if grant is None or grant.expires_at <= datetime.now(timezone.utc):
        raise ScopeDenied
    if grant.kind == "recipient" and grant.dispatch_id is not None:
        dispatch_lookup = (
            select(StockExit.id).join(CommercialOrder, StockExit.commercial_order_id == CommercialOrder.id)
            .where(StockExit.dispatch_id == grant.dispatch_id, StockExit.exit_type == "dispatch",
                   CommercialOrder.client_id == grant.client_id)
        )
        if lock:
            dispatch_lookup = dispatch_lookup.with_for_update(of=(StockExit, CommercialOrder))
        dispatch = db.execute(dispatch_lookup).scalar_one_or_none()
        if dispatch is None:
            raise ScopeDenied
    elif grant.kind != "business" or grant.dispatch_id is not None:
        raise ScopeDenied
    access = AccessContext(grant.owner_id, Role.user, frozenset({grant.client_id}), frozenset())
    if session_id is not None:
        binding = db.execute(select(tracking_sessions).where(
            tracking_sessions.c.session_id == session_id,
            tracking_sessions.c.issuer == principal.issuer,
            tracking_sessions.c.subject == principal.subject,
            tracking_sessions.c.kind == grant.kind,
            tracking_sessions.c.dispatch_id.is_not_distinct_from(grant.dispatch_id),
        )).first()
        if binding is None:
            raise ScopeDenied
        try:
            get_chat_session(db, access, session_id)
        except ChatNotFound:
            raise ScopeDenied from None
    return access, grant


def _tracking_result(db, access, grant, tracking_number, *, lock=False):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}", tracking_number):
        return TrackingLookupResult(status=ToolStatus.not_found)
    statement = (
        select(CommercialOrder.order_id)
        .select_from(StockExit)
        .join(CommercialOrder, StockExit.commercial_order_id == CommercialOrder.id)
        .where(StockExit.exit_type == "dispatch", StockExit.tracking_number == tracking_number,
               StockExit.dispatch_id.is_not(None), CommercialOrder.client_id == grant.client_id)
    )
    if grant.kind == "business":
        if lock:
            db.execute(statement.with_for_update(of=(StockExit, CommercialOrder))).all()
        authorized = lookup_authorized_tracking(
            db, tracking_number=tracking_number, access=access,
        )
        order_id = authorized.commercial_order_id if authorized.status.value == "found" else None
    else:
        # The recipient predicate is applied before hit counting. A duplicate
        # belonging to another recipient never changes the authorized projection.
        statement = statement.where(StockExit.dispatch_id == grant.dispatch_id)
        if lock:
            statement = statement.with_for_update(of=(StockExit, CommercialOrder))
        rows = db.execute(statement).scalars().all()
        order_id = rows[0] if len(rows) == 1 else None
    if order_id is None:
        return TrackingLookupResult(status=ToolStatus.not_found)
    return TrackingLookupResult(
        status=ToolStatus.ok, tracking_number=tracking_number,
        dispatch_state=DispatchState.recorded_dispatch, commercial_order_id=order_id,
    )


def _lookup(db, access, grant, tracking_number, *, lock=False):
    return _tracking_result(db, access, grant, tracking_number, lock=lock).status is ToolStatus.ok


def _require_current_dispatch(db, access, grant, number, *, lock=False):
    if number is None or not _lookup(db, access, grant, number, lock=lock):
        raise ScopeDenied


def _validate_turn(db, access, grant, session_id, generation_id, token=None, *, lock=False):
    messages = chat_messages.alias("external_qa_user_message")
    row = db.execute(
        select(chat_messages.c.content, messages.c.content.label("question"))
        .select_from(chat_generations.join(chat_messages, chat_generations.c.assistant_message_id == chat_messages.c.message_id)
                     .join(messages, chat_generations.c.user_message_id == messages.c.message_id))
        .where(chat_generations.c.session_id == session_id, chat_generations.c.generation_id == generation_id)
    ).one()
    if (token if token is not None else row.content) == FOUND:
        route = route_support_query(row.question)
        if route.route != "tracking":
            raise ScopeDenied
        _require_current_dispatch(db, access, grant, route.tracking_number, lock=lock)


def _authorized_snapshot(db, access, grant, session_id):
    snapshot = chat_snapshot(db, access, session_id)
    users = {message.sequence: message.content for message in snapshot.messages if message.role == "user"}
    for message in snapshot.messages:
        if message.role != "assistant":
            continue
        if message.content not in ("", FOUND, DENIED) or message.knowledge or message.publication_kind:
            raise ScopeDenied
        if message.content == FOUND:
            route = route_support_query(users.get(message.sequence - 1, ""))
            if route.route != "tracking":
                raise ScopeDenied
            _require_current_dispatch(db, access, grant, route.tracking_number)
    return snapshot


def create_external_tracking_qa_app(
    settings: TrackingQASettings,
    database: Callable[[], AbstractContextManager[Session]],
    *,
    key_resolver=None,
) -> FastAPI:
    """Build an explicit, standalone QA deployment with no generic routes."""
    if key_resolver is None:
        jwks = jwt.PyJWKClient(settings.issuer + "/protocol/openid-connect/certs", timeout=3)
        key_resolver = lambda token: jwks.get_signing_key_from_jwt(token).key

    def authenticate(authorization):
        return _authenticate(authorization, settings, key_resolver)

    def scoped(authorization, session_id, action, *, lock=False):
        principal = authenticate(authorization)
        with database() as db:
            access, grant = _resolve_grant(db, principal, session_id, lock=lock)
            return action(db, access, grant)

    mcp = FastMCP("TrackFlow tracking QA", stateless_http=True, json_response=True)

    @mcp.tool(name=MCP_TOOL)
    async def scoped_tracking(session_id: str, tracking_number: str, ctx: Context) -> TrackingProjection:
        """Return only the registered dispatch state under a QA grant."""
        request = ctx.request_context.request
        authorization = request.headers.get("authorization") if request is not None else None
        try:
            found = await asyncio.to_thread(
                scoped, authorization, session_id,
                lambda db, access, grant: _lookup(db, access, grant, tracking_number, lock=True), lock=True,
            )
        except (ScopeDenied, SQLAlchemyError):
            raise ToolError("tracking unavailable") from None
        if found:
            return TrackingProjection(status="found", dispatch_state="recorded_dispatch")
        return TrackingProjection(status="not_found", dispatch_state=None)

    mcp_app = mcp.streamable_http_app()

    async def tracking_mcp(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/mcp":
            from starlette.requests import Request
            from starlette.responses import Response

            authorization = Request(scope).headers.get("authorization")
            try:
                await asyncio.to_thread(authenticate, authorization)
            except ScopeDenied:
                await Response(status_code=401)(scope, receive, send)
                return
        await mcp_app(scope, receive, send)

    # Each coordinator task inherits its initiating request's credential. A
    # reconnect has its own credential and cannot extend the earlier task's TTL.
    credential = ContextVar("external_tracking_qa_credential")
    pubsub = SessionPubSub()

    class ScopedRepository(SQLGenerationRepository):
        async def begin(self, session_id, text):
            return await self._run(begin_generation, session_id, text=text)

        async def reconcile(self, session_id):
            return await self._run(reconcile_orphaned_generation, session_id)

        async def interrupt(self, generation):
            try:
                return await self._run(interrupt_generation, generation)
            except ScopeDenied:
                # The producer is already stopped. Close its durable record.
                await self.fail(generation)
                raise

        async def _run(self, operation, *args, **kwargs):
            item = args[0]
            if operation is fail_generation:
                # Internal id-only cleanup when the client is no longer
                # authorized. It creates no event and exposes no content.
                def execute():
                    with database() as db:
                        fail_generation(db, item)
            else:
                session_id = item if isinstance(item, str) else item.session_id

                def apply(db, access, grant):
                    if operation in (begin_generation, reconcile_orphaned_generation):
                        return operation(db, access, *args, **kwargs)
                    if operation is append_chunk:
                        if kwargs or args[1] not in (FOUND, DENIED):
                            raise ScopeDenied
                        _validate_turn(db, access, grant, session_id, item.generation_id, args[1], lock=True)
                    if operation in (complete_generation, interrupt_generation):
                        _validate_turn(db, access, grant, session_id, item.generation_id, lock=True)
                    return operation(db, *args, **kwargs)

                authorization = credential.get()

                def execute():
                    return scoped(authorization, session_id, apply, lock=True)

            inflight = asyncio.create_task(asyncio.to_thread(execute))
            try:
                return await asyncio.shield(inflight)
            except asyncio.CancelledError:
                await asyncio.gather(inflight, return_exceptions=True)
                raise

    coordinator = ChatCoordinator(ScopedRepository(None), pubsub)

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                await coordinator.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def call_tracking(authorization, session_id, number):
        # Only the process-local dedicated MCP transport receives this bearer.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=tracking_mcp),
            headers={"Authorization": authorization},
        ) as client:
            async with streamable_http_client(f"{MCP_ORIGIN}/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    reply = await session.call_tool(MCP_TOOL, {"session_id": session_id, "tracking_number": number})
                    if reply.isError:
                        raise ScopeDenied
                    if reply.structuredContent == {"status": "not_found", "dispatch_state": None}:
                        return TrackingLookupResult(status=ToolStatus.not_found)
                    if reply.structuredContent != {"status": "found", "dispatch_state": "recorded_dispatch"}:
                        raise ScopeDenied
        # The existing MCP protocol returns only an existence finding. Fetch
        # the typed, still-authorized result for LangGraph without widening it.
        return await asyncio.to_thread(
            scoped, authorization, session_id,
            lambda db, access, grant: _tracking_result(db, access, grant, number, lock=True), lock=True,
        )

    @app.post("/chat/sessions", status_code=201)
    def create_session(_payload: NewSession, authorization: Annotated[str | None, Header()] = None):
        try:
            principal = authenticate(authorization)
            with database() as db:
                access, grant = _resolve_grant(db, principal, lock=True)
                created = create_chat_session(db, access, client_id=grant.client_id, commit=False)
                db.execute(insert(tracking_sessions).values(
                    session_id=created.session_id, issuer=principal.issuer, subject=principal.subject,
                    kind=grant.kind, dispatch_id=grant.dispatch_id,
                ))
                db.commit()
                return created
        except (ScopeDenied, ChatForbidden):
            raise HTTPException(status_code=403, detail="tracking unavailable") from None

    @app.websocket("/chat/ws")
    async def websocket_chat(websocket: WebSocket):
        await websocket.accept()
        subscription = None
        incoming = sender = None
        request_credential = None
        try:
            handshake = AuthenticateEvent.model_validate(await asyncio.wait_for(websocket.receive_json(), timeout=5))
            if handshake.data.support_token is not None:
                raise ScopeDenied
            authorization = "Bearer " + handshake.data.access_token
            session_id = handshake.data.session_id
            request_credential = credential.set(authorization)

            def read_access(_db, access, _grant):
                return access

            def read_snapshot(db, access, grant):
                return _authorized_snapshot(db, access, grant, session_id)

            await asyncio.to_thread(scoped, authorization, session_id, read_access)
            subscription = await pubsub.subscribe(session_id)
            await coordinator.reconcile_after_restart(session_id)
            snapshot = await asyncio.to_thread(
                scoped, authorization, session_id, read_snapshot,
            )
            await websocket.send_json(SessionSnapshotEvent(data=snapshot).model_dump(mode="json"))

            async def send_events():
                cursor = snapshot.cursor
                while True:
                    event = await subscription.receive()
                    next_cursor = getattr(event.data, "cursor", None)
                    if next_cursor is not None and next_cursor <= cursor:
                        continue

                    def validate(db, access, grant):
                        generation_id = getattr(event.data, "generation_id", None)
                        if generation_id is not None:
                            _validate_turn(db, access, grant, session_id, generation_id)

                    await asyncio.to_thread(scoped, authorization, session_id, validate)
                    await websocket.send_json(event.model_dump(mode="json"))
                    if next_cursor is not None:
                        cursor = next_cursor

            async def produce(current_session_id, question):
                access = await asyncio.to_thread(scoped, authorization, current_session_id, read_access)

                async def lookup(number):
                    return await call_tracking(authorization, current_session_id, number)

                found = await external_tracking_answer(
                    question, access=access, database=database, lookup=lookup,
                )
                yield FOUND if found else DENIED

            sender = asyncio.create_task(send_events())
            while True:
                incoming = asyncio.create_task(websocket.receive_json())
                done, _ = await asyncio.wait((sender, incoming), return_when=asyncio.FIRST_COMPLETED)
                if sender in done:
                    sender.result()
                    return
                message = CLIENT_EVENT_ADAPTER.validate_python(incoming.result())
                if message.data.session_id != session_id:
                    raise ScopeDenied
                await asyncio.to_thread(scoped, authorization, session_id, read_access)
                if isinstance(message, UserMessageEvent):
                    if not await coordinator.start(session_id, message.data.text, produce):
                        await pubsub.publish(session_id, ErrorEvent(data=ErrorData(
                            session_id=session_id, code="generation_active",
                        )))
                elif isinstance(message, InterruptRequestedEvent):
                    await coordinator.interrupt(session_id, message.data.new_input, produce)
                else:
                    raise ScopeDenied
        except WebSocketDisconnect:
            pass
        except (ScopeDenied, SlowSubscriber, ValidationError, TimeoutError, GenerationConflict, SQLAlchemyError, ExceptionGroup):
            await websocket.close(code=4403, reason="tracking unavailable")
        finally:
            with CancelScope(shield=True):
                tasks = [task for task in (incoming, sender) if task is not None]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if subscription is not None:
                    await pubsub.unsubscribe(subscription)
            if request_credential is not None:
                credential.reset(request_credential)

    return app
