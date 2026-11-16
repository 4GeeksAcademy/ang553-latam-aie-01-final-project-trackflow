"""Typed contracts for TrackFlow's structured episodic memory."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MemoryCategory(str, Enum):
    carrier_coverage = "carrier_coverage"
    recurring_incident = "recurring_incident"
    b2b_report_preference = "b2b_report_preference"


class MemoryScope(str, Enum):
    global_ = "global"
    client = "client"


class ProposalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    edited = "edited"
    ambiguous_discard = "ambiguous_discard"
    expired = "expired"


class ProposalOutcome(str, Enum):
    approved = "approved"
    rejected = "rejected"
    edited = "edited"
    ambiguous_discard = "ambiguous_discard"
    expired = "expired"


class EntryStatus(str, Enum):
    active = "active"
    superseded = "superseded"
    expired = "expired"


class MemoryProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    origin_interaction_id: str = Field(min_length=1, max_length=64)
    origin_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: MemoryScope
    client_id: str | None = Field(default=None, min_length=1, max_length=36)
    category: MemoryCategory
    content: dict[str, Any]
    canonical_key: str = Field(min_length=1, max_length=200)
    valid_until: datetime | None = None

    @field_validator("origin_interaction_id", "canonical_key")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_scope(self) -> "MemoryProposalCreate":
        if self.scope is MemoryScope.global_ and self.client_id is not None:
            raise ValueError("global memory must not have client_id")
        if self.scope is MemoryScope.client and self.client_id is None:
            raise ValueError("client memory requires client_id")
        if (
            self.category is MemoryCategory.b2b_report_preference
            and self.scope is not MemoryScope.client
        ):
            raise ValueError("B2B report preference must be client-scoped")
        if self.valid_until is not None and self.valid_until.tzinfo is None:
            raise ValueError("valid_until must be timezone-aware")
        return self


class MemoryProposalRecord(BaseModel):
    id: UUID
    conversation_id: UUID
    user_id: str
    scope: MemoryScope
    client_id: str | None
    category: MemoryCategory
    content: dict[str, Any]
    canonical_key: str
    origin_interaction_id: str
    origin_message_sha256: str
    status: ProposalStatus
    parent_proposal_id: UUID | None
    valid_until: datetime | None
    expires_at: datetime
    created_at: datetime
    resolved_at: datetime | None
    version: int


class MemoryDecisionRecord(BaseModel):
    id: UUID
    proposal_id: UUID
    outcome: ProposalOutcome
    actor_user_id: str
    idempotency_key: str
    decided_at: datetime


class MemoryEntryRecord(BaseModel):
    id: UUID
    scope: MemoryScope
    client_id: str | None
    category: MemoryCategory
    content: dict[str, Any]
    canonical_key: str
    verification_status: str
    authorizing_decision_id: UUID
    supersedes_entry_id: UUID | None
    status: EntryStatus
    version: int
    valid_from: datetime
    expires_at: datetime
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime


class ResolutionResult(BaseModel):
    proposal: MemoryProposalRecord
    decision: MemoryDecisionRecord
    entry: MemoryEntryRecord | None = None
    replacement_proposal: MemoryProposalRecord | None = None


class ConsentAction(str, Enum):
    approve = "approve"
    reject = "reject"
    edit = "edit"
    ambiguous = "ambiguous"
    topic_change = "topic_change"


class ConsentSource(str, Enum):
    explicit = "explicit"
    classifier = "classifier"
    conservative_text = "conservative_text"


class ConsentDisposition(str, Enum):
    approved = "approved"
    rejected = "rejected"
    replacement_pending = "replacement_pending"
    discarded = "discarded"
    expired = "expired"


class ConsentRequest(BaseModel):
    """Transport-neutral consent input for a pending memory proposal."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=64)
    proposal_id: UUID | None = None
    action: ConsentAction | None = None
    user_text: str | None = Field(default=None, max_length=2000)
    edited_proposal: MemoryProposalCreate | None = None

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("idempotency_key must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_edit_contract(self) -> "ConsentRequest":
        if self.action is ConsentAction.edit and self.edited_proposal is None:
            raise ValueError("explicit edit requires edited_proposal")
        if self.action not in {None, ConsentAction.edit} and self.edited_proposal is not None:
            raise ValueError("edited_proposal is valid only for edit classification")
        if self.action is ConsentAction.topic_change and not (self.user_text or "").strip():
            raise ValueError("topic_change requires the new support question")
        return self


class ConsentClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    action: ConsentAction
    confidence: float = Field(ge=0.0, le=1.0)
    source: ConsentSource
    edited_content: dict[str, Any] | None = None
    support_question: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_conversational_payload(self) -> "ConsentClassification":
        if self.action is not ConsentAction.edit and self.edited_content is not None:
            raise ValueError("edited_content is valid only for edit")
        if self.support_question is not None:
            normalized = self.support_question.strip()
            self.support_question = normalized or None
        return self


class ConsentResult(BaseModel):
    classification: ConsentClassification
    disposition: ConsentDisposition
    resolution: ResolutionResult
    continue_support_question: bool = False
    support_question: str | None = None
