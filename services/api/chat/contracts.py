"""Strict wire contracts for TrackFlow chat.

The four correlation fields used as extensions to the briefing are
``message_id``, ``generation_id``, ``cursor`` and ``code``.  They identify a
durable message/generation, close the snapshot-to-live-event race, and expose
only bounded machine-readable errors and optional progress stages.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from services.rag.chunking import DOCUMENT_PLANS
from services.rag.contracts import KnowledgeCoverage


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


AnswerKind: TypeAlias = Literal["insufficient_context", "clarification"]
PublicationKind: TypeAlias = Literal["authorized_unit"]
CoverageTopic: TypeAlias = Literal["return_window", "return_cost", "return_inspection"]
_KNOWLEDGE_SECTIONS = frozenset(
    (plan.source_document, section)
    for plan in DOCUMENT_PLANS
    for section in plan.sections
)
ProgressStage: TypeAlias = Literal[
    "processing", "retrieving_context", "preparing_response", "validating_response", "interrupting",
]


class GenerationProgressData(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)
    generation_id: str = Field(min_length=1, max_length=64)
    stage: ProgressStage
    # Independent, process-local revision. It never advances history's cursor.
    cursor: int = Field(ge=1)


class GenerationProgressEvent(StrictModel):
    event: Literal["generation_progress"] = "generation_progress"
    data: GenerationProgressData


class ChatKnowledgeSource(StrictModel):
    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    source_document: str
    section: str
    retrieval_kind: Literal["primary", "complementary"]

    @model_validator(mode="after")
    def validate_section(self) -> ChatKnowledgeSource:
        if (self.source_document, self.section) not in _KNOWLEDGE_SECTIONS:
            raise ValueError("unknown knowledge source")
        return self


class ChatKnowledge(StrictModel):
    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    # A natural answer can carry references without making a coverage claim.
    coverage: KnowledgeCoverage | None = Field(default=None, exclude_if=lambda value: value is None)
    sources: tuple[ChatKnowledgeSource, ...] = Field(max_length=len(_KNOWLEDGE_SECTIONS))

    @model_validator(mode="after")
    def validate_metadata(self) -> ChatKnowledge:
        sections = {(source.source_document, source.section) for source in self.sources}
        if (
            (self.coverage == "insufficient") != (not self.sources)
            or len(sections) != len(self.sources)
            or (self.sources and not any(source.retrieval_kind == "primary" for source in self.sources))
        ):
            raise ValueError("inconsistent knowledge coverage or sources")
        return self


class UnitCoverage(StrictModel):
    """Coverage of an explicit QA topic set, independent of execution status."""

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    status: Literal["complete", "partial", "none", "indeterminate"]
    missing_topics: tuple[CoverageTopic, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def validate_missing_topics(self) -> UnitCoverage:
        if len(self.missing_topics) != len(set(self.missing_topics)) or (
            self.status == "complete" and self.missing_topics
        ):
            raise ValueError("inconsistent unit coverage")
        return self


class ChatMessage(StrictModel):
    message_id: str = Field(min_length=1, max_length=64)
    role: Literal["user", "assistant"]
    content: str = Field(max_length=16_000)
    status: Literal["completed", "streaming", "interrupted", "failed"]
    sequence: int = Field(ge=1)
    created_at: datetime
    answer_kind: AnswerKind | None = Field(default=None, exclude_if=lambda value: value is None)
    knowledge: ChatKnowledge | None = Field(default=None, exclude_if=lambda value: value is None)
    publication_kind: PublicationKind | None = Field(default=None, exclude_if=lambda value: value is None)
    unit_coverage: UnitCoverage | None = Field(default=None, exclude_if=lambda value: value is None)


class UnitReference(StrictModel):
    """Internal reference for current authorization of one published unit."""

    model_config = ConfigDict(frozen=True)

    unit_id: str = Field(pattern=r"^[A-Z][A-Z0-9-]{0,31}$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TokenChunkData(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=1, max_length=16_000)
    sequence: int = Field(ge=1)
    message_id: str = Field(min_length=1, max_length=64)
    generation_id: str = Field(min_length=1, max_length=64)
    cursor: int = Field(ge=1)
    answer_kind: AnswerKind | None = Field(default=None, exclude_if=lambda value: value is None)
    knowledge: ChatKnowledge | None = Field(default=None, exclude_if=lambda value: value is None)
    publication_kind: PublicationKind | None = Field(default=None, exclude_if=lambda value: value is None)
    unit_ref: UnitReference | None = Field(default=None, exclude=True)
    unit_coverage: UnitCoverage | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_unit_reference(self) -> TokenChunkData:
        if self.unit_ref is not None and (
            self.publication_kind != "authorized_unit" or self.knowledge is not None or self.answer_kind is not None
        ):
            raise ValueError("invalid authorized unit metadata")
        if self.unit_coverage is not None and self.publication_kind != "authorized_unit":
            raise ValueError("coverage requires authorized unit publication")
        return self


class TokenChunkEvent(StrictModel):
    event: Literal["token_chunk"] = "token_chunk"
    data: TokenChunkData


class CoverageUpdatedData(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)
    message_id: str = Field(min_length=1, max_length=64)
    generation_id: str = Field(min_length=1, max_length=64)
    cursor: int = Field(ge=1)
    coverage: UnitCoverage


class CoverageUpdatedEvent(StrictModel):
    event: Literal["coverage_updated"] = "coverage_updated"
    data: CoverageUpdatedData


class GenerationInterruptedData(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)
    message_id: str = Field(min_length=1, max_length=64)
    status: Literal["interrupted"] = "interrupted"
    generation_id: str = Field(min_length=1, max_length=64)
    cursor: int = Field(ge=1)


class GenerationInterruptedEvent(StrictModel):
    event: Literal["generation_interrupted"] = "generation_interrupted"
    data: GenerationInterruptedData


class GenerationCompletedData(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)
    message_id: str = Field(min_length=1, max_length=64)
    generation_id: str = Field(min_length=1, max_length=64)
    cursor: int = Field(ge=1)


class GenerationCompletedEvent(StrictModel):
    event: Literal["generation_completed"] = "generation_completed"
    data: GenerationCompletedData


class SessionSnapshotData(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)
    messages: list[ChatMessage]
    cursor: int = Field(ge=0)
    progress: GenerationProgressData | None = Field(default=None, exclude_if=lambda value: value is None)


class SessionSnapshotEvent(StrictModel):
    event: Literal["session_snapshot"] = "session_snapshot"
    data: SessionSnapshotData


class ErrorData(StrictModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=64)
    code: Literal[
        "invalid_event",
        "generation_active",
        "generation_unavailable",
        "support_authorization_required",
        "session_unavailable",
    ]


class ErrorEvent(StrictModel):
    event: Literal["error"] = "error"
    data: ErrorData


ServerEvent: TypeAlias = Annotated[
    Union[
        TokenChunkEvent,
        CoverageUpdatedEvent,
        GenerationProgressEvent,
        GenerationInterruptedEvent,
        GenerationCompletedEvent,
        SessionSnapshotEvent,
        ErrorEvent,
    ],
    Field(discriminator="event"),
]


class AuthenticateData(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)
    access_token: str = Field(min_length=1, max_length=8192)
    support_token: str | None = Field(default=None, min_length=1, max_length=8192)
    progress_events: bool = False


class AuthenticateEvent(StrictModel):
    event: Literal["authenticate"] = "authenticate"
    data: AuthenticateData


class UserMessageData(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized


class UserMessageEvent(StrictModel):
    event: Literal["user_message"] = "user_message"
    data: UserMessageData


class InterruptRequestedData(StrictModel):
    session_id: str = Field(min_length=1, max_length=64)
    new_input: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("new_input")
    @classmethod
    def normalize_new_input(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("new_input must not be blank")
        return normalized


class InterruptRequestedEvent(StrictModel):
    event: Literal["interrupt_requested"] = "interrupt_requested"
    data: InterruptRequestedData


ClientEvent: TypeAlias = Annotated[
    Union[AuthenticateEvent, UserMessageEvent, InterruptRequestedEvent],
    Field(discriminator="event"),
]

CLIENT_EVENT_ADAPTER = TypeAdapter(ClientEvent)


class CreateChatSessionRequest(StrictModel):
    client_id: str = Field(min_length=1, max_length=36)


class ChatSessionResponse(StrictModel):
    session_id: str
    agent_id: Literal["first_line_cx"] = "first_line_cx"
    user_id: str
    client_id: str
    status: Literal["active", "interrupted", "closed"]
    created_at: datetime
