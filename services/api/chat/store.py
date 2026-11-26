"""Transactional PostgreSQL store for chat history and durable event cursors."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from services.api.access_control import AccessContext
from services.api.chat.contracts import (
    AnswerKind,
    ChatKnowledge,
    ChatMessage,
    ChatSessionResponse,
    PublicationKind,
    SessionSnapshotData,
    UnitCoverage,
    UnitReference,
)
from services.api.chat.qa_unit_catalog import CatalogUnit, authorized_unit
from services.api.chat.qa_units_scope import can_store_qa_unit, qa_unit_scope
from services.api.chat.models import (
    chat_event_cursors,
    chat_events,
    chat_generations,
    chat_messages,
    chat_sessions,
)
from services.incidents.models import clients
from services.rag.knowledge_coverage import INSUFFICIENT_KNOWLEDGE
from services.rag.service import INSUFFICIENT_CONTEXT_SENTINEL


class ChatNotFound(RuntimeError):
    pass


class ChatForbidden(RuntimeError):
    pass


class GenerationConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class GenerationRecord:
    session_id: str
    generation_id: str
    user_message_id: str
    assistant_message_id: str


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _authorize_row(row, access: AccessContext) -> None:
    if row.user_id != access.user_id or not access.can_access_client(row.client_id):
        raise ChatNotFound("chat session not found")


def create_chat_session(
    db: Session, access: AccessContext, *, client_id: str, commit: bool = True
) -> ChatSessionResponse:
    if not access.can_access_client(client_id):
        raise ChatForbidden("client access is not authorized")
    active_client = db.execute(
        select(clients.c.client_id).where(
            clients.c.client_id == client_id,
            clients.c.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if active_client is None:
        raise ChatForbidden("client access is not authorized")
    session_id = str(uuid4())
    row = {
        "session_id": session_id,
        "agent_id": "first_line_cx",
        "user_id": access.user_id,
        "client_id": client_id,
        "status": "active",
    }
    db.execute(insert(chat_sessions).values(**row))
    db.execute(insert(chat_event_cursors).values(session_id=session_id, last_cursor=0))
    if commit:
        db.commit()
    stored = db.execute(
        select(chat_sessions).where(chat_sessions.c.session_id == session_id)
    ).mappings().one()
    return ChatSessionResponse(
        session_id=stored.session_id,
        agent_id=stored.agent_id,
        user_id=stored.user_id,
        client_id=stored.client_id,
        status=stored.status,
        created_at=stored.created_at,
    )


@dataclass(frozen=True)
class ChatSupportContext:
    client_id: str
    history_json: str | None


def chat_support_context(
    db: Session,
    access: AccessContext,
    session_id: str,
    *,
    max_messages: int = 8,
    max_characters: int = 6000,
) -> ChatSupportContext:
    """Render bounded, completed user/assistant pairs as untrusted JSON data."""
    row = get_chat_session(db, access, session_id)
    user_message = chat_messages.alias("history_user_message")
    assistant_message = chat_messages.alias("history_assistant_message")
    generation_rows = list(
        db.execute(
            select(
                user_message.c.role.label("user_role"),
                user_message.c.content.label("user_content"),
                user_message.c.status.label("user_status"),
                assistant_message.c.role.label("assistant_role"),
                assistant_message.c.content.label("assistant_content"),
                assistant_message.c.status.label("assistant_status"),
            )
            .select_from(chat_generations)
            .join(
                user_message,
                user_message.c.message_id == chat_generations.c.user_message_id,
            )
            .join(
                assistant_message,
                assistant_message.c.message_id == chat_generations.c.assistant_message_id,
            )
            .where(
                chat_generations.c.session_id == session_id,
                chat_generations.c.status == "completed",
                user_message.c.status == "completed",
                assistant_message.c.status == "completed",
                ~select(chat_events.c.cursor).where(
                    chat_events.c.session_id == chat_generations.c.session_id,
                    chat_events.c.payload["message_id"].astext == assistant_message.c.message_id,
                    or_(
                        chat_events.c.event_type == "coverage_updated",
                        and_(chat_events.c.event_type == "token_chunk", or_(
                            chat_events.c.payload["publication_kind"].astext == "authorized_unit",
                            chat_events.c.payload.has_key("unit_ref"),
                        )),
                    ),
                ).exists(),
            )
            .order_by(chat_generations.c.started_at.desc())
            .limit(max_messages // 2)
        ).mappings()
    )
    generation_rows.reverse()
    rendered = []
    for generation in generation_rows:
        rendered.extend((
            {"role": generation.user_role, "status": generation.user_status,
             "content": generation.user_content[:1000]},
            {"role": generation.assistant_role, "status": generation.assistant_status,
             "content": generation.assistant_content[:1000]},
        ))
    while rendered:
        envelope = json.dumps(
            {
                "trust": "untrusted_data_only",
                "source": "chat_history",
                "messages": rendered,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(envelope) <= max_characters:
            return ChatSupportContext(row.client_id, envelope)
        del rendered[:2]
    return ChatSupportContext(row.client_id, None)


def get_chat_session(
    db: Session, access: AccessContext, session_id: str, *, lock: bool = False
):
    statement = select(chat_sessions).where(chat_sessions.c.session_id == session_id)
    if lock:
        statement = statement.with_for_update()
    row = db.execute(statement).mappings().one_or_none()
    if row is None:
        raise ChatNotFound("chat session not found")
    _authorize_row(row, access)
    return row


def chat_snapshot(
    db: Session, access: AccessContext, session_id: str, *, include_knowledge: bool = False,
    include_authorized_units: bool = False,
) -> SessionSnapshotData:
    get_chat_session(db, access, session_id)
    # Derive metadata from the same event snapshot as the message and cursor.
    token_events = (
        select()
        .where(
            chat_events.c.session_id == chat_messages.c.session_id,
            chat_events.c.event_type == "token_chunk",
            chat_events.c.payload["message_id"].astext == chat_messages.c.message_id,
        )
        .correlate(chat_messages)
        .order_by(chat_events.c.cursor)
        .limit(1)
    )
    event_answer_kind = chat_events.c.payload["answer_kind"].astext
    answer_kind = token_events.add_columns(event_answer_kind).where(
        event_answer_kind.in_(("insufficient_context", "clarification")),
    ).scalar_subquery()
    event_knowledge = chat_events.c.payload["knowledge"]
    knowledge_column = (
        token_events.add_columns(event_knowledge).where(event_knowledge.is_not(None))
        .scalar_subquery().label("knowledge")
    )
    event_publication_kind = chat_events.c.payload["publication_kind"].astext
    publication_kind = token_events.add_columns(event_publication_kind).where(
        event_publication_kind == "authorized_unit",
    ).scalar_subquery().label("publication_kind")
    has_unit_ref = token_events.add_columns(chat_events.c.cursor).where(
        chat_events.c.payload.has_key("unit_ref"),
    ).scalar_subquery().is_not(None).label("has_unit_ref")
    coverage_column = (
        select(func.coalesce(chat_events.c.payload["unit_coverage"], chat_events.c.payload["coverage"]))
        .where(
            chat_events.c.session_id == chat_messages.c.session_id,
            chat_events.c.payload["message_id"].astext == chat_messages.c.message_id,
            or_(
                chat_events.c.event_type == "coverage_updated",
                and_(chat_events.c.event_type == "token_chunk", chat_events.c.payload.has_key("unit_coverage")),
            ),
        )
        .correlate(chat_messages)
        .order_by(chat_events.c.cursor.desc())
        .limit(1)
        .scalar_subquery().label("unit_coverage")
    )
    snapshot_rows = list(db.execute(
        select(
            chat_event_cursors.c.last_cursor,
            chat_messages.c.message_id,
            chat_messages.c.role,
            chat_messages.c.content,
            chat_messages.c.status,
            chat_messages.c.sequence,
            chat_messages.c.created_at,
            answer_kind.label("answer_kind"),
            publication_kind,
            has_unit_ref,
            *([knowledge_column] if include_knowledge else []),
            *([coverage_column] if include_authorized_units and os.getenv("TRACKFLOW_I14_QA_UNITS") == "1" else []),
        )
        .select_from(
            chat_event_cursors.outerjoin(
                chat_messages,
                chat_messages.c.session_id == chat_event_cursors.c.session_id,
            )
        )
        .where(chat_event_cursors.c.session_id == session_id)
        .order_by(chat_messages.c.sequence)
    ).mappings())
    if not snapshot_rows:
        raise ChatNotFound("chat session not found")
    cursor = snapshot_rows[0].last_cursor
    snapshot = SessionSnapshotData(
        session_id=session_id,
        messages=[
            _snapshot_message(row, include_authorized_units=include_authorized_units)
            for row in snapshot_rows
            if row.message_id is not None
        ],
        cursor=cursor,
    )
    if include_authorized_units:
        redact_revoked_units(db, access, snapshot)
    return snapshot


def _snapshot_message(row, *, include_authorized_units: bool = False) -> ChatMessage:
    content = row.content
    answer_kind = row.answer_kind
    knowledge = None
    unit_coverage = None
    publication_kind = row.get("publication_kind")
    is_unit_message = publication_kind == "authorized_unit" or row.get("has_unit_ref")
    if is_unit_message:
        answer_kind = None
        if include_authorized_units:
            publication_kind = "authorized_unit"
        else:
            content = ""
            publication_kind = None
    if row.role == "assistant":
        if include_authorized_units and (is_unit_message or not content) and row.get("unit_coverage") is not None:
            try:
                unit_coverage = UnitCoverage.model_validate(row.unit_coverage)
            except ValidationError:
                pass
        if INSUFFICIENT_CONTEXT_SENTINEL in content.upper():
            # Older messages may predate the output rule. Preserve the durable
            # record while withholding the complete ambiguous response.
            content = INSUFFICIENT_KNOWLEDGE
            answer_kind = "insufficient_context"
        elif row.get("knowledge") is not None and not is_unit_message:
            try:
                knowledge = ChatKnowledge.model_validate(row.knowledge)
            except ValidationError:
                # A malformed or retired reference cannot leave storage.
                pass
    return ChatMessage(
        message_id=row.message_id,
        role=row.role,
        content=content,
        status=row.status,
        sequence=row.sequence,
        created_at=row.created_at,
        answer_kind=answer_kind,
        knowledge=knowledge,
        publication_kind=publication_kind,
        unit_coverage=unit_coverage,
    )


def _published_unit(db: Session, client_id: str, payload: dict, *, lock: bool = False) -> CatalogUnit | None:
    if os.getenv("TRACKFLOW_I14_QA_UNITS") != "1" or payload.get("publication_kind") != "authorized_unit":
        return None
    try:
        ref = UnitReference.model_validate(payload.get("unit_ref"))
    except ValidationError:
        return None
    return authorized_unit(db, client_id, ref.unit_id, ref.version, lock=lock)


def qa_unit_is_current(
    db: Session, access: AccessContext, session_id: str, ref: UnitReference, token: str, sequence: int,
) -> bool:
    scope = qa_unit_scope()
    if scope is None or not scope.allows(access):
        return False
    session = get_chat_session(db, access, session_id)
    unit = _published_unit(db, session.client_id, {
        "publication_kind": "authorized_unit",
        "unit_ref": ref.model_dump(),
    })
    return unit is not None and token == ("\n\n" if sequence > 1 else "") + unit.render


def _authorized_units_render(
    db: Session, client_id: str, events: list[dict], *, lock: bool = False,
) -> str | None:
    seen = set()
    chunks = []
    composition_group = None
    for index, event in enumerate(events):
        # Invalid data and a mix of ordinary, legacy and dynamic chunks are not replayable.
        unit = _published_unit(db, client_id, event, lock=lock)
        if unit is None or (
            composition_group is not None and unit.composition_group != composition_group
        ):
            return None
        if unit.id in seen or event.get("token") != ("\n\n" if index else "") + unit.render:
            return None
        composition_group = unit.composition_group
        seen.add(unit.id)
        chunks.append(unit.render)
    return "\n\n".join(chunks)


def _message_token_payloads(
    db: Session, session_id: str, message_id: str, *, through_cursor: int | None = None,
) -> list[dict]:
    statement = select(chat_events.c.payload).where(
        chat_events.c.session_id == session_id,
        chat_events.c.event_type == "token_chunk",
        chat_events.c.payload["message_id"].astext == message_id,
    )
    if through_cursor is not None:
        statement = statement.where(chat_events.c.cursor <= through_cursor)
    return list(db.execute(statement.order_by(chat_events.c.cursor)).scalars())


def qa_coverage_is_current(
    db: Session, access: AccessContext, session_id: str, message_id: str, cursor: int,
) -> bool:
    scope = qa_unit_scope()
    if os.getenv("TRACKFLOW_I14_QA_UNITS") != "1" or scope is None or not scope.allows(access):
        return False
    get_chat_session(db, access, session_id)
    chunks = _message_token_payloads(db, session_id, message_id, through_cursor=cursor)
    return _authorized_units_render(db, scope.client_id, chunks) is not None


def redact_revoked_units(db: Session, access: AccessContext, snapshot: SessionSnapshotData) -> None:
    """Recheck exact catalog versions for dynamic chunks included at the snapshot watermark."""
    messages = {
        message.message_id: message for message in snapshot.messages
        if message.publication_kind or message.unit_coverage is not None
    }
    if not messages:
        return
    message_id = chat_events.c.payload["message_id"].astext
    events = db.execute(
        select(message_id, chat_events.c.payload).where(
            chat_events.c.session_id == snapshot.session_id,
            chat_events.c.event_type == "token_chunk",
            chat_events.c.cursor <= snapshot.cursor,
            message_id.in_(messages),
        ).order_by(chat_events.c.cursor)
    )
    grouped: dict[str, list[dict]] = {}
    for current_message, payload in events:
        grouped.setdefault(current_message, []).append(payload)
    scope = qa_unit_scope()
    for identifier, message in messages.items():
        chunks = grouped.get(identifier, [])
        has_dynamic_refs = any("unit_ref" in payload for payload in chunks)
        rendered = (
            _authorized_units_render(db, scope.client_id, chunks)
            if (has_dynamic_refs or message.unit_coverage is not None) and scope is not None and scope.allows(access)
            else None
        )
        if message.unit_coverage is not None and (
            os.getenv("TRACKFLOW_I14_QA_UNITS") != "1" or rendered != message.content
        ):
            message.unit_coverage = None
        if has_dynamic_refs and rendered != message.content:
            message.content = ""
            message.knowledge = None
            message.publication_kind = None


def begin_generation(
    db: Session, access: AccessContext, session_id: str, text: str
) -> GenerationRecord:
    session_row = get_chat_session(db, access, session_id, lock=True)
    if session_row.status == "closed":
        raise GenerationConflict("chat session is closed")
    active = db.execute(
        select(chat_generations.c.generation_id).where(
            chat_generations.c.session_id == session_id,
            chat_generations.c.status == "active",
        )
    ).scalar_one_or_none()
    if active is not None:
        raise GenerationConflict("a generation is already active")
    last_sequence = db.execute(
        select(func.coalesce(func.max(chat_messages.c.sequence), 0)).where(
            chat_messages.c.session_id == session_id
        )
    ).scalar_one()
    user_message_id = _identifier("msg")
    assistant_message_id = _identifier("msg")
    generation_id = _identifier("gen")
    db.execute(
        insert(chat_messages),
        [
            {
                "message_id": user_message_id,
                "session_id": session_id,
                "role": "user",
                "content": text,
                "status": "completed",
                "sequence": last_sequence + 1,
            },
            {
                "message_id": assistant_message_id,
                "session_id": session_id,
                "role": "assistant",
                "content": "",
                "status": "streaming",
                "sequence": last_sequence + 2,
            },
        ],
    )
    try:
        db.execute(
            insert(chat_generations).values(
                generation_id=generation_id,
                session_id=session_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                status="active",
            )
        )
        db.execute(
            update(chat_sessions)
            .where(chat_sessions.c.session_id == session_id)
            .values(status="active", updated_at=func.clock_timestamp())
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise GenerationConflict("a generation is already active") from exc
    return GenerationRecord(
        session_id, generation_id, user_message_id, assistant_message_id
    )


def _active_generation(db: Session, generation: GenerationRecord):
    row = db.execute(
        select(chat_generations)
        .where(
            chat_generations.c.generation_id == generation.generation_id,
            chat_generations.c.session_id == generation.session_id,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if row is None or row.status != "active":
        raise GenerationConflict("generation is no longer active")
    return row


def _next_cursor(db: Session, session_id: str) -> int:
    current = db.execute(
        select(chat_event_cursors.c.last_cursor)
        .where(chat_event_cursors.c.session_id == session_id)
        .with_for_update()
    ).scalar_one()
    cursor = current + 1
    db.execute(
        update(chat_event_cursors)
        .where(chat_event_cursors.c.session_id == session_id)
        .values(last_cursor=cursor)
    )
    return cursor


def append_chunk(
    db: Session, generation: GenerationRecord, token: str, *,
    answer_kind: AnswerKind | None = None, knowledge: ChatKnowledge | None = None,
    publication_kind: PublicationKind | None = None,
    unit_ref: UnitReference | None = None,
    unit_coverage: UnitCoverage | None = None,
) -> dict:
    if unit_coverage is not None and unit_ref is None:
        raise GenerationConflict("unit coverage requires a current dynamic QA unit")
    validated_coverage = (
        UnitCoverage.model_validate(unit_coverage).model_dump(mode="json")
        if unit_coverage is not None else None
    )
    if unit_ref is not None and publication_kind != "authorized_unit":
        raise GenerationConflict("dynamic QA units require authorized publication")
    if publication_kind is not None and (
        publication_kind != "authorized_unit"
        or (unit_ref is None and os.getenv("TRACKFLOW_I13_QA_UNITS") != "1")
        or answer_kind is not None
        or knowledge is not None
        or not can_store_qa_unit(db, generation.session_id)
    ):
        raise GenerationConflict("QA unit publication is not authorized")
    validated_knowledge = (
        ChatKnowledge.model_validate(knowledge).model_dump(mode="json")
        if knowledge is not None
        else None
    )
    generation_row = _active_generation(db, generation)
    if validated_knowledge is not None and generation_row.chunk_sequence != 0:
        raise GenerationConflict("knowledge metadata belongs to a complete response")
    message = db.execute(
        select(chat_messages.c.content)
        .where(chat_messages.c.message_id == generation.assistant_message_id)
        .with_for_update()
    ).scalar_one()
    combined = message + token
    if unit_ref is not None or (generation_row.chunk_sequence and qa_unit_scope() is not None):
        previous_events = _message_token_payloads(db, generation.session_id, generation.assistant_message_id)
        if unit_ref is not None:
            scope = qa_unit_scope()
            previous_events.append({
                "publication_kind": publication_kind,
                "unit_ref": unit_ref.model_dump(),
                "token": token,
            })
            if scope is None or _authorized_units_render(
                db, scope.client_id, previous_events, lock=True,
            ) != combined:
                raise GenerationConflict("dynamic QA unit or its composition is no longer authorized")
        elif any("unit_ref" in event for event in previous_events):
            raise GenerationConflict("dynamic QA units cannot be mixed with other chunks")
    if unit_ref is None and qa_unit_scope() is not None and db.execute(
        select(chat_events.c.cursor).where(
            chat_events.c.session_id == generation.session_id,
            chat_events.c.event_type == "coverage_updated",
            chat_events.c.payload["message_id"].astext == generation.assistant_message_id,
        ).limit(1)
    ).first() is not None:
        raise GenerationConflict("QA coverage cannot be mixed with ordinary chunks")
    if len(combined) > 16_000:
        raise GenerationConflict("assistant message exceeds the supported limit")
    db.execute(
        update(chat_generations)
        .where(chat_generations.c.generation_id == generation.generation_id)
        .values(chunk_sequence=generation_row.chunk_sequence + 1)
    )
    db.execute(
        update(chat_messages)
        .where(chat_messages.c.message_id == generation.assistant_message_id)
        .values(content=combined, updated_at=func.clock_timestamp())
    )
    cursor = _next_cursor(db, generation.session_id)
    payload = {
        "session_id": generation.session_id,
        "token": token,
        "sequence": generation_row.chunk_sequence + 1,
        "message_id": generation.assistant_message_id,
        "generation_id": generation.generation_id,
        "cursor": cursor,
    }
    if answer_kind is not None:
        payload["answer_kind"] = answer_kind
    if validated_knowledge is not None:
        payload["knowledge"] = validated_knowledge
    if publication_kind is not None:
        payload["publication_kind"] = publication_kind
    if unit_ref is not None:
        payload["unit_ref"] = unit_ref.model_dump()
    if validated_coverage is not None:
        payload["unit_coverage"] = validated_coverage
    db.execute(
        insert(chat_events).values(
            session_id=generation.session_id,
            cursor=cursor,
            event_type="token_chunk",
            payload=payload,
        )
    )
    db.commit()
    return payload


def record_unit_coverage(db: Session, generation: GenerationRecord, coverage: UnitCoverage) -> dict:
    scope = qa_unit_scope()
    if os.getenv("TRACKFLOW_I14_QA_UNITS") != "1" or scope is None or not can_store_qa_unit(db, generation.session_id):
        raise GenerationConflict("QA coverage publication is not authorized")
    _active_generation(db, generation)
    content = db.execute(
        select(chat_messages.c.content)
        .where(chat_messages.c.message_id == generation.assistant_message_id)
        .with_for_update()
    ).scalar_one()
    chunks = _message_token_payloads(db, generation.session_id, generation.assistant_message_id)
    if _authorized_units_render(db, scope.client_id, chunks, lock=True) != content:
        raise GenerationConflict("coverage evidence is no longer authorized")
    if not chunks and coverage.status in {"complete", "partial"}:
        raise GenerationConflict("covered topics require published units")
    cursor = _next_cursor(db, generation.session_id)
    payload = {
        "session_id": generation.session_id,
        "message_id": generation.assistant_message_id,
        "generation_id": generation.generation_id,
        "cursor": cursor,
        "coverage": UnitCoverage.model_validate(coverage).model_dump(mode="json"),
    }
    db.execute(insert(chat_events).values(
        session_id=generation.session_id, cursor=cursor, event_type="coverage_updated", payload=payload,
    ))
    db.commit()
    return payload


def complete_generation(db: Session, generation: GenerationRecord) -> dict:
    _active_generation(db, generation)
    db.execute(
        update(chat_generations)
        .where(chat_generations.c.generation_id == generation.generation_id)
        .values(status="completed", ended_at=func.clock_timestamp())
    )
    db.execute(
        update(chat_messages)
        .where(chat_messages.c.message_id == generation.assistant_message_id)
        .values(status="completed", updated_at=func.clock_timestamp())
    )
    cursor = _next_cursor(db, generation.session_id)
    payload = {
        "session_id": generation.session_id,
        "message_id": generation.assistant_message_id,
        "generation_id": generation.generation_id,
        "cursor": cursor,
    }
    db.execute(insert(chat_events).values(
        session_id=generation.session_id, cursor=cursor,
        event_type="generation_completed", payload=payload,
    ))
    db.commit()
    return payload


def interrupt_generation(db: Session, generation: GenerationRecord) -> dict:
    _active_generation(db, generation)
    db.execute(update(chat_generations).where(
        chat_generations.c.generation_id == generation.generation_id
    ).values(status="interrupted", ended_at=func.clock_timestamp()))
    db.execute(update(chat_messages).where(
        chat_messages.c.message_id == generation.assistant_message_id
    ).values(status="interrupted", updated_at=func.clock_timestamp()))
    db.execute(update(chat_sessions).where(
        chat_sessions.c.session_id == generation.session_id
    ).values(status="interrupted", updated_at=func.clock_timestamp()))
    cursor = _next_cursor(db, generation.session_id)
    payload = {
        "session_id": generation.session_id,
        "message_id": generation.assistant_message_id,
        "status": "interrupted",
        "generation_id": generation.generation_id,
        "cursor": cursor,
    }
    db.execute(insert(chat_events).values(
        session_id=generation.session_id, cursor=cursor,
        event_type="generation_interrupted", payload=payload,
    ))
    db.commit()
    return payload


def reconcile_orphaned_generation(
    db: Session, access: AccessContext, session_id: str
) -> dict | None:
    """Interrupt one durable active generation after a single-process restart.

    The in-process coordinator must establish that no task is active before
    calling this transaction. Locking the session and generation makes repeated
    reconnects idempotent.
    """
    get_chat_session(db, access, session_id, lock=True)
    row = db.execute(
        select(chat_generations)
        .where(
            chat_generations.c.session_id == session_id,
            chat_generations.c.status == "active",
        )
        .with_for_update()
    ).mappings().one_or_none()
    if row is None:
        db.rollback()
        return None
    return interrupt_generation(
        db,
        GenerationRecord(
            session_id=session_id,
            generation_id=row.generation_id,
            user_message_id=row.user_message_id,
            assistant_message_id=row.assistant_message_id,
        ),
    )


def fail_generation(db: Session, generation: GenerationRecord) -> None:
    try:
        _active_generation(db, generation)
    except GenerationConflict:
        db.rollback()
        return
    db.execute(update(chat_generations).where(
        chat_generations.c.generation_id == generation.generation_id
    ).values(status="failed", ended_at=func.clock_timestamp()))
    db.execute(update(chat_messages).where(
        chat_messages.c.message_id == generation.assistant_message_id
    ).values(status="failed", updated_at=func.clock_timestamp()))
    db.commit()
