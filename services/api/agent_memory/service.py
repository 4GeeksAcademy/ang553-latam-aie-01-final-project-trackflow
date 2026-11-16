"""Transactional interface for consent-gated TrackFlow agent memory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session

from services.api.access_control import AccessContext
from services.api.agent_memory.contracts import (
    EntryStatus,
    MemoryCategory,
    MemoryDecisionRecord,
    MemoryEntryRecord,
    MemoryProposalCreate,
    MemoryProposalRecord,
    MemoryScope,
    ProposalOutcome,
    ProposalStatus,
    ResolutionResult,
)
from services.api.agent_memory.models import (
    memory_conversations,
    memory_decisions,
    memory_entries,
    memory_proposals,
)
from services.api.auth_models import Role
from services.incidents.models import clients


PROPOSAL_TTL = timedelta(hours=24)
ENTRY_TTLS = {
    MemoryCategory.carrier_coverage: timedelta(days=30),
    MemoryCategory.recurring_incident: timedelta(days=14),
    MemoryCategory.b2b_report_preference: timedelta(days=180),
}
DEFAULT_MAX_ACTIVE_PER_SCOPE_CATEGORY = 100

_SECRET = re.compile(
    r"(?i)(bearer\s+\S+|password|passwd|api[_ -]?key|access[_ -]?token|"
    r"refresh[_ -]?token|client[_ -]?secret|credential|jwt\b|sk-[a-z0-9]+|"
    r"(?:postgres(?:ql)?|redis|mongodb(?:\+srv)?)://)"
)
_ADDRESS = re.compile(
    r"(?i)(\b\d{1,6}\s+[\w.'-]+(?:\s+[\w.'-]+){0,4}\s+"
    r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|calle|avenida)\b|"
    r"(?:\b(?:calle|avenida|avda\.?|paseo|plaza|carretera)\b|c/)"
    r"\s+[\w.'-]+(?:\s+[\w.'-]+){0,4}\s+\d{1,6}\b|"
    r"\bdirecci[oó]n\s+(?:de\s+)?(?:entrega|postal|f[ií]sica|b2b|b2c)\b|"
    r"\b(?:latitude|longitude|coordinates?|postal code|zip code)\b)"
)
_WAREHOUSE_ROUTE = re.compile(
    r"(?i)\b(?:internal warehouse route|warehouse route|aisle|rack|shelf|bin|"
    r"loading dock|security gate)\b"
)
_NEGOTIATION = re.compile(
    r"(?i)\b(?:contract negotiation|active negotiation|negotiated price|"
    r"commercial terms|renewal offer|pricing offer)\b"
)
_SINGLE_PACKAGE = re.compile(
    r"(?i)\b(?:tracking|parcel|package|shipment)\s*(?:number|id|#)?\s*"
    r"(?=[A-Z0-9-]*\d)[A-Z0-9-]{6,}\b"
)
_COUNTRIES = frozenset({"US", "ES"})


class MemoryError(RuntimeError):
    pass


class MemoryPolicyViolation(MemoryError):
    pass


class MemoryForbidden(MemoryError):
    pass


class MemoryConflict(MemoryError):
    pass


class MemoryNotFound(MemoryError):
    pass


class MemoryPersistenceError(MemoryError):
    pass


class MemoryStore:
    """Explicit read/write boundary for structured episodic memory."""

    def bind_conversation(
        self,
        session: Session,
        conversation_id: UUID,
        access: AccessContext,
        *,
        create: bool,
        now: datetime | None = None,
        commit: bool = True,
    ) -> UUID:
        """Create or verify a content-free conversation ownership binding."""
        if access.role not in {Role.admin, Role.manager}:
            raise MemoryNotFound("conversation not found")
        moment = _utc(now)
        row = session.execute(
            select(memory_conversations).where(
                memory_conversations.c.id == str(conversation_id)
            )
        ).mappings().first()
        if row is not None:
            if str(row["user_id"]) != access.user_id:
                raise MemoryNotFound("conversation not found")
            try:
                session.execute(
                    update(memory_conversations)
                    .where(
                        memory_conversations.c.id == str(conversation_id),
                        memory_conversations.c.user_id == access.user_id,
                    )
                    .values(last_seen_at=moment)
                )
                if commit:
                    session.commit()
            except SQLAlchemyError as exc:
                session.rollback()
                raise MemoryPersistenceError("conversation persistence failed") from exc
            return conversation_id
        if not create:
            raise MemoryNotFound("conversation not found")
        try:
            session.execute(
                insert(memory_conversations).values(
                    id=str(conversation_id),
                    user_id=access.user_id,
                    created_at=moment,
                    last_seen_at=moment,
                )
            )
            if commit:
                session.commit()
        except IntegrityError as exc:
            session.rollback()
            owner = session.execute(
                select(memory_conversations.c.user_id).where(
                    memory_conversations.c.id == str(conversation_id)
                )
            ).scalar_one_or_none()
            if owner != access.user_id:
                raise MemoryNotFound("conversation not found") from exc
        except SQLAlchemyError as exc:
            session.rollback()
            raise MemoryPersistenceError("conversation persistence failed") from exc
        return conversation_id

    def create_proposal(
        self,
        session: Session,
        payload: MemoryProposalCreate,
        access: AccessContext,
        *,
        now: datetime | None = None,
        parent_proposal_id: UUID | None = None,
    ) -> MemoryProposalRecord:
        moment = _utc(now)
        _authorize_scope(session, payload, access)
        content = _validated_content(payload.category, payload.content)
        expected_key = canonical_key_for(payload.category, content)
        if payload.canonical_key.casefold() != expected_key:
            raise MemoryPolicyViolation("canonical key does not match structured content")
        existing = session.execute(
            select(memory_proposals).where(
                memory_proposals.c.user_id == access.user_id,
                memory_proposals.c.conversation_id == str(payload.conversation_id),
                memory_proposals.c.origin_interaction_id == payload.origin_interaction_id,
            )
        ).mappings().first()
        if existing is not None:
            if _same_proposal(existing, payload, content, expected_key):
                return _proposal(existing)
            raise MemoryConflict("origin interaction was already used")

        pending = session.execute(
            select(memory_proposals.c.id).where(
                memory_proposals.c.user_id == access.user_id,
                memory_proposals.c.conversation_id == str(payload.conversation_id),
                memory_proposals.c.status == ProposalStatus.pending.value,
            )
        ).first()
        if pending is not None:
            raise MemoryConflict("conversation already has a pending proposal")

        self.bind_conversation(
            session,
            payload.conversation_id,
            access,
            create=True,
            now=moment,
            commit=False,
        )

        proposal_id = uuid4()
        values = {
            "id": str(proposal_id),
            "conversation_id": str(payload.conversation_id),
            "user_id": access.user_id,
            "scope": payload.scope.value,
            "client_id": payload.client_id,
            "category": payload.category.value,
            "content": content,
            "canonical_key": expected_key,
            "origin_interaction_id": payload.origin_interaction_id,
            "origin_message_sha256": payload.origin_message_sha256,
            "status": ProposalStatus.pending.value,
            "parent_proposal_id": None if parent_proposal_id is None else str(parent_proposal_id),
            "valid_until": _entry_expiry(payload.category, payload.valid_until, moment),
            "expires_at": moment + PROPOSAL_TTL,
            "created_at": moment,
            "resolved_at": None,
            "version": 1,
        }
        try:
            row = session.execute(
                insert(memory_proposals).values(**values).returning(memory_proposals)
            ).mappings().one()
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise MemoryConflict("proposal conflicts with existing state") from exc
        except SQLAlchemyError as exc:
            session.rollback()
            raise MemoryPersistenceError("proposal persistence failed") from exc
        return _proposal(row)

    def get_pending(
        self,
        session: Session,
        conversation_id: UUID,
        access: AccessContext,
    ) -> MemoryProposalRecord | None:
        row = session.execute(
            select(memory_proposals).where(
                memory_proposals.c.user_id == access.user_id,
                memory_proposals.c.conversation_id == str(conversation_id),
                memory_proposals.c.status == ProposalStatus.pending.value,
            )
        ).mappings().first()
        if row is None:
            return None
        _authorize_row_scope(session, row, access)
        return _proposal(row)

    def get_proposal(
        self,
        session: Session,
        proposal_id: UUID,
        access: AccessContext,
    ) -> MemoryProposalRecord:
        """Return an owned proposal, including resolved proposals for safe retries."""
        row = session.execute(
            select(memory_proposals).where(
                memory_proposals.c.id == str(proposal_id),
                memory_proposals.c.user_id == access.user_id,
            )
        ).mappings().first()
        if row is None:
            raise MemoryNotFound("proposal not found")
        _authorize_row_scope(session, row, access)
        return _proposal(row)

    def resolve_proposal(
        self,
        session: Session,
        proposal_id: UUID,
        outcome: ProposalOutcome,
        idempotency_key: str,
        access: AccessContext,
        *,
        now: datetime | None = None,
        edited_payload: MemoryProposalCreate | None = None,
    ) -> ResolutionResult:
        key = _idempotency_key(idempotency_key)
        moment = _utc(now)
        prior = session.execute(
            select(memory_decisions).where(
                memory_decisions.c.actor_user_id == access.user_id,
                memory_decisions.c.idempotency_key == key,
            )
        ).mappings().first()
        if prior is not None:
            if str(prior["proposal_id"]) != str(proposal_id) or prior["outcome"] != outcome.value:
                raise MemoryConflict("idempotency key was used for another decision")
            return self._resolution_for_decision(session, prior, access)

        proposal = session.execute(
            select(memory_proposals)
            .where(memory_proposals.c.id == str(proposal_id))
            .with_for_update()
        ).mappings().first()
        if proposal is None or str(proposal["user_id"]) != access.user_id:
            raise MemoryNotFound("proposal not found")
        _authorize_row_scope(session, proposal, access)
        if proposal["status"] != ProposalStatus.pending.value:
            existing_decision = session.execute(
                select(memory_decisions).where(
                    memory_decisions.c.proposal_id == str(proposal_id)
                )
            ).mappings().first()
            if (
                existing_decision is not None
                and existing_decision["outcome"] == ProposalOutcome.expired.value
            ):
                return self._resolution_for_decision(session, existing_decision, access)
            raise MemoryConflict("proposal is no longer pending")

        effective_outcome = outcome
        decision_key = key
        if (
            _db_time(proposal["expires_at"]) <= moment
            or _db_time(proposal["valid_until"]) <= moment
        ):
            effective_outcome = ProposalOutcome.expired
            decision_key = f"expiry:{proposal_id}"
        if effective_outcome is ProposalOutcome.edited:
            if edited_payload is None:
                raise MemoryPolicyViolation("edited outcome requires replacement proposal")
            if edited_payload.conversation_id != UUID(str(proposal["conversation_id"])):
                raise MemoryPolicyViolation("edited proposal must remain in the same conversation")
            _authorize_scope(session, edited_payload, access)
            edited_content = _validated_content(edited_payload.category, edited_payload.content)
            if edited_payload.canonical_key.casefold() != canonical_key_for(
                edited_payload.category, edited_content
            ):
                raise MemoryPolicyViolation("edited canonical key is invalid")
        elif effective_outcome is ProposalOutcome.approved:
            if edited_payload is not None:
                raise MemoryPolicyViolation(
                    "replacement proposal is valid only for edited outcome"
                )
            approved_category = MemoryCategory(str(proposal["category"]))
            approved_content = _validated_content(
                approved_category, dict(proposal["content"])
            )
            if str(proposal["canonical_key"]).casefold() != canonical_key_for(
                approved_category, approved_content
            ):
                raise MemoryPolicyViolation("stored proposal is no longer valid")
        elif edited_payload is not None:
            raise MemoryPolicyViolation("replacement proposal is valid only for edited outcome")

        decision_id = uuid4()
        try:
            changed = session.execute(
                update(memory_proposals)
                .where(
                    memory_proposals.c.id == str(proposal_id),
                    memory_proposals.c.status == ProposalStatus.pending.value,
                    memory_proposals.c.version == int(proposal["version"]),
                )
                .values(
                    status=effective_outcome.value,
                    resolved_at=moment,
                    version=int(proposal["version"]) + 1,
                )
            )
            if changed.rowcount != 1:
                raise MemoryConflict("proposal resolution lost a concurrent race")
            decision_row = session.execute(
                insert(memory_decisions)
                .values(
                    id=str(decision_id),
                    proposal_id=str(proposal_id),
                    outcome=effective_outcome.value,
                    actor_user_id=access.user_id,
                    idempotency_key=decision_key,
                    decided_at=moment,
                )
                .returning(memory_decisions)
            ).mappings().one()
            entry_row = None
            replacement_row = None
            if effective_outcome is ProposalOutcome.approved:
                entry_row = self._consolidate(
                    session, proposal, decision_id, access.user_id, moment
                )
            elif effective_outcome is ProposalOutcome.edited:
                replacement_row = self._insert_replacement(
                    session,
                    edited_payload,
                    access,
                    proposal_id,
                    moment,
                )
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise MemoryConflict("decision conflicts with existing state") from exc
        except SQLAlchemyError as exc:
            session.rollback()
            raise MemoryPersistenceError("proposal resolution failed") from exc
        except Exception:
            session.rollback()
            raise

        refreshed = session.execute(
            select(memory_proposals).where(memory_proposals.c.id == str(proposal_id))
        ).mappings().one()
        return ResolutionResult(
            proposal=_proposal(refreshed),
            decision=_decision(decision_row),
            entry=None if entry_row is None else _entry(entry_row),
            replacement_proposal=(
                None if replacement_row is None else _proposal(replacement_row)
            ),
        )

    def list_active(
        self,
        session: Session,
        access: AccessContext,
        *,
        category: MemoryCategory | None = None,
        now: datetime | None = None,
    ) -> list[MemoryEntryRecord]:
        if access.role not in {Role.admin, Role.manager}:
            raise MemoryForbidden("support memory requires admin or manager")
        moment = _utc(now)
        visibility = memory_entries.c.scope == MemoryScope.global_.value
        if access.global_access:
            visibility = or_(visibility, memory_entries.c.scope == MemoryScope.client.value)
        elif access.allowed_client_ids:
            visibility = or_(
                visibility,
                and_(
                    memory_entries.c.scope == MemoryScope.client.value,
                    memory_entries.c.client_id.in_(access.allowed_client_ids),
                ),
            )
        statement = select(memory_entries).where(
            memory_entries.c.status == EntryStatus.active.value,
            memory_entries.c.expires_at > moment,
            visibility,
        )
        if category is not None:
            statement = statement.where(memory_entries.c.category == category.value)
        rows = session.execute(
            statement.order_by(memory_entries.c.category, memory_entries.c.canonical_key)
        ).mappings().all()
        return [_entry(row) for row in rows]

    def expire_and_clean(
        self,
        session: Session,
        *,
        now: datetime | None = None,
        max_active_per_scope_category: int = DEFAULT_MAX_ACTIVE_PER_SCOPE_CATEGORY,
    ) -> tuple[int, int]:
        """Audit proposal expiry and bound active entries without deleting history."""
        if max_active_per_scope_category <= 0:
            raise MemoryPolicyViolation("memory capacity must be positive")
        moment = _utc(now)
        proposal_count = 0
        try:
            expired_proposals = session.execute(
                select(memory_proposals)
                .where(
                    memory_proposals.c.status == ProposalStatus.pending.value,
                    or_(
                        memory_proposals.c.expires_at <= moment,
                        memory_proposals.c.valid_until <= moment,
                    ),
                )
                .with_for_update()
            ).mappings().all()
            for proposal in expired_proposals:
                decision_id = uuid4()
                result = session.execute(
                    update(memory_proposals)
                    .where(
                        memory_proposals.c.id == proposal["id"],
                        memory_proposals.c.status == ProposalStatus.pending.value,
                        memory_proposals.c.version == proposal["version"],
                    )
                    .values(
                        status=ProposalStatus.expired.value,
                        resolved_at=moment,
                        version=int(proposal["version"]) + 1,
                    )
                )
                if result.rowcount != 1:
                    continue
                session.execute(
                    insert(memory_decisions).values(
                        id=str(decision_id),
                        proposal_id=proposal["id"],
                        outcome=ProposalOutcome.expired.value,
                        actor_user_id="system:expiry",
                        idempotency_key=f"expiry:{proposal['id']}",
                        decided_at=moment,
                    )
                )
                proposal_count += 1
            entry_result = session.execute(
                update(memory_entries)
                .where(
                    memory_entries.c.status == EntryStatus.active.value,
                    memory_entries.c.expires_at <= moment,
                )
                .values(status=EntryStatus.expired.value, updated_at=moment)
            )
            capacity_rows = session.execute(
                select(memory_entries)
                .where(memory_entries.c.status == EntryStatus.active.value)
                .order_by(
                    memory_entries.c.updated_at.desc(),
                    memory_entries.c.created_at.desc(),
                    memory_entries.c.id.desc(),
                )
                .with_for_update()
            ).mappings().all()
            bucket_counts: dict[tuple[str, str | None, str], int] = {}
            capacity_ids: list[str] = []
            for entry in capacity_rows:
                bucket = (
                    str(entry["scope"]),
                    None if entry["client_id"] is None else str(entry["client_id"]),
                    str(entry["category"]),
                )
                bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
                if bucket_counts[bucket] > max_active_per_scope_category:
                    capacity_ids.append(str(entry["id"]))
            capacity_count = 0
            if capacity_ids:
                capacity_result = session.execute(
                    update(memory_entries)
                    .where(
                        memory_entries.c.id.in_(capacity_ids),
                        memory_entries.c.status == EntryStatus.active.value,
                    )
                    .values(status=EntryStatus.expired.value, updated_at=moment)
                )
                capacity_count = int(capacity_result.rowcount or 0)
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            raise MemoryPersistenceError("memory cleanup failed") from exc
        except Exception:
            session.rollback()
            raise
        return proposal_count, int(entry_result.rowcount or 0) + capacity_count

    def _consolidate(
        self,
        session: Session,
        proposal: Any,
        decision_id: UUID,
        actor_user_id: str,
        moment: datetime,
    ) -> Any:
        active = session.execute(
            select(memory_entries)
            .where(
                memory_entries.c.scope == proposal["scope"],
                memory_entries.c.client_id.is_(None)
                if proposal["client_id"] is None
                else memory_entries.c.client_id == proposal["client_id"],
                memory_entries.c.category == proposal["category"],
                memory_entries.c.canonical_key == proposal["canonical_key"],
                memory_entries.c.status == EntryStatus.active.value,
            )
            .with_for_update()
        ).mappings().first()
        if active is not None and active["content"] == proposal["content"]:
            return active
        version = 1 if active is None else int(active["version"]) + 1
        if active is not None:
            changed = session.execute(
                update(memory_entries)
                .where(
                    memory_entries.c.id == active["id"],
                    memory_entries.c.status == EntryStatus.active.value,
                    memory_entries.c.version == active["version"],
                )
                .values(status=EntryStatus.superseded.value, updated_at=moment)
            )
            if changed.rowcount != 1:
                raise MemoryConflict("memory consolidation lost a concurrent race")
        return session.execute(
            insert(memory_entries)
            .values(
                id=str(uuid4()),
                scope=proposal["scope"],
                client_id=proposal["client_id"],
                category=proposal["category"],
                content=proposal["content"],
                canonical_key=proposal["canonical_key"],
                verification_status="user_reported",
                authorizing_decision_id=str(decision_id),
                supersedes_entry_id=None if active is None else active["id"],
                status=EntryStatus.active.value,
                version=version,
                valid_from=moment,
                expires_at=proposal["valid_until"],
                created_by_user_id=actor_user_id,
                created_at=moment,
                updated_at=moment,
            )
            .returning(memory_entries)
        ).mappings().one()

    def _insert_replacement(
        self,
        session: Session,
        payload: MemoryProposalCreate | None,
        access: AccessContext,
        parent_proposal_id: UUID,
        moment: datetime,
    ) -> Any:
        assert payload is not None
        content = _validated_content(payload.category, payload.content)
        return session.execute(
            insert(memory_proposals)
            .values(
                id=str(uuid4()),
                conversation_id=str(payload.conversation_id),
                user_id=access.user_id,
                scope=payload.scope.value,
                client_id=payload.client_id,
                category=payload.category.value,
                content=content,
                canonical_key=canonical_key_for(payload.category, content),
                origin_interaction_id=payload.origin_interaction_id,
                origin_message_sha256=payload.origin_message_sha256,
                status=ProposalStatus.pending.value,
                parent_proposal_id=str(parent_proposal_id),
                valid_until=_entry_expiry(payload.category, payload.valid_until, moment),
                expires_at=moment + PROPOSAL_TTL,
                created_at=moment,
                resolved_at=None,
                version=1,
            )
            .returning(memory_proposals)
        ).mappings().one()

    def _resolution_for_decision(
        self, session: Session, decision: Any, access: AccessContext
    ) -> ResolutionResult:
        proposal = session.execute(
            select(memory_proposals).where(
                memory_proposals.c.id == decision["proposal_id"],
                memory_proposals.c.user_id == access.user_id,
            )
        ).mappings().first()
        if proposal is None:
            raise MemoryNotFound("proposal not found")
        _authorize_row_scope(session, proposal, access)
        entry = session.execute(
            select(memory_entries).where(
                memory_entries.c.authorizing_decision_id == decision["id"]
            )
        ).mappings().first()
        if entry is None and decision["outcome"] == ProposalOutcome.approved.value:
            entry = session.execute(
                select(memory_entries).where(
                    memory_entries.c.scope == proposal["scope"],
                    memory_entries.c.client_id.is_(None)
                    if proposal["client_id"] is None
                    else memory_entries.c.client_id == proposal["client_id"],
                    memory_entries.c.category == proposal["category"],
                    memory_entries.c.canonical_key == proposal["canonical_key"],
                    memory_entries.c.status == EntryStatus.active.value,
                )
            ).mappings().first()
        replacement = session.execute(
            select(memory_proposals).where(
                memory_proposals.c.parent_proposal_id == proposal["id"]
            )
        ).mappings().first()
        return ResolutionResult(
            proposal=_proposal(proposal),
            decision=_decision(decision),
            entry=None if entry is None else _entry(entry),
            replacement_proposal=None if replacement is None else _proposal(replacement),
        )


def canonical_key_for(category: MemoryCategory, content: dict[str, Any]) -> str:
    normalized = _validated_content(category, content)
    if category is MemoryCategory.carrier_coverage:
        return f"{normalized['country'].lower()}:{normalized['carrier'].casefold()}"
    if category is MemoryCategory.recurring_incident:
        digest = hashlib.sha256(normalized["pattern"].casefold().encode()).hexdigest()[:24]
        return f"{normalized['country'].lower()}:{digest}"
    return str(normalized["preference_type"]).casefold()


def _validated_content(category: MemoryCategory, content: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(content, dict):
        raise MemoryPolicyViolation("memory content must be a structured object")
    schemas = {
        MemoryCategory.carrier_coverage: {
            "carrier": str,
            "country": str,
            "coverage_rule": str,
        },
        MemoryCategory.recurring_incident: {
            "country": str,
            "pattern": str,
            "operational_context": str,
            "recurrence_count": int,
        },
        MemoryCategory.b2b_report_preference: {
            "preference_type": str,
            "preference": str,
        },
    }
    schema = schemas[category]
    if set(content) != set(schema):
        raise MemoryPolicyViolation("memory content fields do not match category")
    normalized: dict[str, Any] = {}
    for field, expected in schema.items():
        value = content[field]
        if expected is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise MemoryPolicyViolation(f"{field} must be an integer")
            normalized[field] = value
            continue
        if not isinstance(value, str):
            raise MemoryPolicyViolation(f"{field} must be text")
        text_value = " ".join(value.split())
        if not text_value or len(text_value) > 500:
            raise MemoryPolicyViolation(f"{field} has invalid length")
        normalized[field] = text_value
    if "country" in normalized:
        normalized["country"] = normalized["country"].upper()
        if normalized["country"] not in _COUNTRIES:
            raise MemoryPolicyViolation("country must be US or ES")
    if category is MemoryCategory.recurring_incident and normalized["recurrence_count"] < 2:
        raise MemoryPolicyViolation("recurring incident requires at least two occurrences")
    if category is MemoryCategory.b2b_report_preference:
        normalized["preference_type"] = normalized["preference_type"].casefold()
        if normalized["preference_type"] not in {"format", "metric_priority"}:
            raise MemoryPolicyViolation("unsupported B2B preference type")
    serialized = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    for pattern in (_SECRET, _ADDRESS, _WAREHOUSE_ROUTE, _NEGOTIATION, _SINGLE_PACKAGE):
        if pattern.search(serialized):
            raise MemoryPolicyViolation("memory content violates TrackFlow retention policy")
    return normalized


def _authorize_scope(
    session: Session, payload: MemoryProposalCreate, access: AccessContext
) -> None:
    if access.role is Role.user:
        raise MemoryForbidden("support memory requires admin or manager")
    if payload.scope is MemoryScope.global_:
        if access.role is not Role.admin or not access.global_access:
            raise MemoryForbidden("global memory requires admin authorization")
        return
    if payload.client_id is None or not access.can_access_client(payload.client_id):
        raise MemoryForbidden("client memory is not authorized")
    active = session.execute(
        select(clients.c.client_id).where(
            clients.c.client_id == payload.client_id,
            clients.c.is_active.is_(True),
        )
    ).first()
    if active is None:
        raise MemoryForbidden("client memory is not authorized")


def _authorize_row_scope(session: Session, row: Any, access: AccessContext) -> None:
    if access.role not in {Role.admin, Role.manager}:
        raise MemoryNotFound("proposal not found")
    if row["scope"] == MemoryScope.global_.value:
        if access.role is not Role.admin or not access.global_access:
            raise MemoryNotFound("proposal not found")
        return
    if not access.can_access_client(str(row["client_id"])):
        raise MemoryNotFound("proposal not found")


def _entry_expiry(
    category: MemoryCategory, requested: datetime | None, now: datetime
) -> datetime:
    maximum = now + ENTRY_TTLS[category]
    if requested is None:
        return maximum
    selected = _utc(requested)
    if selected <= now:
        raise MemoryPolicyViolation("memory validity must be in the future")
    return min(selected, maximum)


def _same_proposal(
    row: Any,
    payload: MemoryProposalCreate,
    content: dict[str, Any],
    canonical_key: str,
) -> bool:
    return (
        row["scope"] == payload.scope.value
        and row["client_id"] == payload.client_id
        and row["category"] == payload.category.value
        and row["content"] == content
        and row["canonical_key"] == canonical_key
        and row["origin_message_sha256"] == payload.origin_message_sha256
    )


def _idempotency_key(value: str) -> str:
    if not isinstance(value, str):
        raise MemoryPolicyViolation("idempotency key must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        raise MemoryPolicyViolation("idempotency key has invalid length")
    return normalized


def _utc(value: datetime | None) -> datetime:
    moment = datetime.now(timezone.utc) if value is None else value
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise MemoryPolicyViolation("timestamp must be timezone-aware")
    return moment.astimezone(timezone.utc)


def _db_time(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise MemoryPersistenceError("database returned invalid timestamp")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _proposal(row: Any) -> MemoryProposalRecord:
    return MemoryProposalRecord(
        id=UUID(str(row["id"])),
        conversation_id=UUID(str(row["conversation_id"])),
        user_id=str(row["user_id"]),
        scope=row["scope"],
        client_id=row["client_id"],
        category=row["category"],
        content=dict(row["content"]),
        canonical_key=str(row["canonical_key"]),
        origin_interaction_id=str(row["origin_interaction_id"]),
        origin_message_sha256=str(row["origin_message_sha256"]),
        status=row["status"],
        parent_proposal_id=(
            None if row["parent_proposal_id"] is None else UUID(str(row["parent_proposal_id"]))
        ),
        valid_until=None if row["valid_until"] is None else _db_time(row["valid_until"]),
        expires_at=_db_time(row["expires_at"]),
        created_at=_db_time(row["created_at"]),
        resolved_at=None if row["resolved_at"] is None else _db_time(row["resolved_at"]),
        version=int(row["version"]),
    )


def _decision(row: Any) -> MemoryDecisionRecord:
    return MemoryDecisionRecord(
        id=UUID(str(row["id"])),
        proposal_id=UUID(str(row["proposal_id"])),
        outcome=row["outcome"],
        actor_user_id=str(row["actor_user_id"]),
        idempotency_key=str(row["idempotency_key"]),
        decided_at=_db_time(row["decided_at"]),
    )


def _entry(row: Any) -> MemoryEntryRecord:
    return MemoryEntryRecord(
        id=UUID(str(row["id"])),
        scope=row["scope"],
        client_id=row["client_id"],
        category=row["category"],
        content=dict(row["content"]),
        canonical_key=str(row["canonical_key"]),
        verification_status=str(row["verification_status"]),
        authorizing_decision_id=UUID(str(row["authorizing_decision_id"])),
        supersedes_entry_id=(
            None if row["supersedes_entry_id"] is None else UUID(str(row["supersedes_entry_id"]))
        ),
        status=row["status"],
        version=int(row["version"]),
        valid_from=_db_time(row["valid_from"]),
        expires_at=_db_time(row["expires_at"]),
        created_by_user_id=str(row["created_by_user_id"]),
        created_at=_db_time(row["created_at"]),
        updated_at=_db_time(row["updated_at"]),
    )
