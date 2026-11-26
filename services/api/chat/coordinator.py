"""One-producer-per-session generation coordination and genuine task cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from services.api.access_control import AccessContext
from services.api.chat.contracts import (
    AnswerKind,
    ChatKnowledge,
    CoverageUpdatedData,
    CoverageUpdatedEvent,
    ErrorData,
    ErrorEvent,
    GenerationCompletedData,
    GenerationCompletedEvent,
    GenerationInterruptedData,
    GenerationInterruptedEvent,
    GenerationProgressData,
    GenerationProgressEvent,
    ProgressStage,
    PublicationKind,
    TokenChunkData,
    TokenChunkEvent,
    UnitCoverage,
    UnitReference,
)
from services.api.chat.pubsub import SessionPubSub
from services.api.chat.store import (
    GenerationConflict,
    GenerationRecord,
    append_chunk,
    begin_generation,
    complete_generation,
    fail_generation,
    interrupt_generation,
    reconcile_orphaned_generation,
    record_unit_coverage,
)
from services.api.database import open_db_session


class ProducerUnavailable(RuntimeError):
    """The safe real-agent producer is not available for this request."""


@dataclass(frozen=True)
class AnnotatedChunk:
    text: str
    answer_kind: AnswerKind | None = None
    knowledge: ChatKnowledge | None = None
    publication_kind: PublicationKind | None = None
    unit_ref: UnitReference | None = None
    unit_coverage: UnitCoverage | None = None


@dataclass(frozen=True)
class CoverageUpdate:
    coverage: UnitCoverage


ProgressReporter = Callable[[ProgressStage], Awaitable[None]]
ProductionItem = str | AnnotatedChunk | CoverageUpdate
Producer = Callable[[str, str], AsyncIterator[ProductionItem]]


@dataclass(frozen=True)
class ProgressProducer:
    run: Callable[[str, str, ProgressReporter | None], AsyncIterator[ProductionItem]]

    def __call__(self, session_id: str, text: str) -> AsyncIterator[ProductionItem]:
        return self.run(session_id, text, None)


class GenerationRepository(Protocol):
    async def begin(self, session_id: str, text: str) -> GenerationRecord: ...
    async def append(
        self, generation: GenerationRecord, token: str, *,
        answer_kind: AnswerKind | None = None, knowledge: ChatKnowledge | None = None,
        publication_kind: PublicationKind | None = None,
        unit_ref: UnitReference | None = None,
        unit_coverage: UnitCoverage | None = None,
    ) -> dict: ...
    async def append_coverage(self, generation: GenerationRecord, coverage: UnitCoverage) -> dict: ...
    async def complete(self, generation: GenerationRecord) -> dict: ...
    async def interrupt(self, generation: GenerationRecord) -> dict: ...
    async def fail(self, generation: GenerationRecord) -> None: ...
    async def reconcile(self, session_id: str) -> dict | None: ...


class SQLGenerationRepository:
    """Short-lived sessions keep database work outside the event loop."""

    def __init__(self, access: AccessContext) -> None:
        self.access = access

    async def _run(self, operation, *args, **kwargs):
        def execute():
            with open_db_session() as db:
                return operation(db, *args, **kwargs)
        # Cancelling an asyncio wrapper cannot stop a database thread. Shield
        # it and, on cancellation, wait for the transaction to finish before
        # the coordinator persists an interrupt.
        inflight = asyncio.create_task(asyncio.to_thread(execute))
        try:
            return await asyncio.shield(inflight)
        except asyncio.CancelledError:
            await inflight
            raise

    async def begin(self, session_id: str, text: str) -> GenerationRecord:
        return await self._run(
            begin_generation,
            self.access,
            session_id,
            text=text,
        )

    async def append(
        self, generation: GenerationRecord, token: str, *,
        answer_kind: AnswerKind | None = None, knowledge: ChatKnowledge | None = None,
        publication_kind: PublicationKind | None = None,
        unit_ref: UnitReference | None = None,
        unit_coverage: UnitCoverage | None = None,
    ) -> dict:
        if unit_coverage is not None:
            return await self._run(
                append_chunk, generation, token, publication_kind=publication_kind,
                answer_kind=answer_kind, knowledge=knowledge, unit_ref=unit_ref,
                unit_coverage=unit_coverage,
            )
        if unit_ref is not None:
            return await self._run(
                append_chunk, generation, token, publication_kind=publication_kind,
                answer_kind=answer_kind, knowledge=knowledge, unit_ref=unit_ref,
            )
        if publication_kind is not None:
            return await self._run(append_chunk, generation, token, publication_kind=publication_kind)
        if knowledge is not None:
            return await self._run(
                append_chunk, generation, token, answer_kind=answer_kind, knowledge=knowledge,
            )
        if answer_kind is None:
            return await self._run(append_chunk, generation, token)
        return await self._run(append_chunk, generation, token, answer_kind=answer_kind)

    async def append_coverage(self, generation: GenerationRecord, coverage: UnitCoverage) -> dict:
        return await self._run(record_unit_coverage, generation, coverage)

    async def complete(self, generation: GenerationRecord) -> dict:
        return await self._run(complete_generation, generation)

    async def interrupt(self, generation: GenerationRecord) -> dict:
        return await self._run(interrupt_generation, generation)

    async def fail(self, generation: GenerationRecord) -> None:
        await self._run(fail_generation, generation)

    async def reconcile(self, session_id: str) -> dict | None:
        return await self._run(
            reconcile_orphaned_generation, self.access, session_id
        )


@dataclass
class _ActiveGeneration:
    generation: GenerationRecord
    task: asyncio.Task
    accepting_chunks: bool = True
    interrupting: bool = False
    reports_progress: bool = False
    progress: GenerationProgressData | None = None


class ChatCoordinator:
    def __init__(self, repository: GenerationRepository, pubsub: SessionPubSub) -> None:
        self.repository = repository
        self.pubsub = pubsub
        self._active: dict[str, _ActiveGeneration] = {}
        self._lock = asyncio.Lock()
        self._progress_cursor = 0

    async def start(self, session_id: str, text: str, producer: Producer) -> bool:
        async with self._lock:
            return await self._start_locked(session_id, text, producer)

    async def _start_locked(self, session_id: str, text: str, producer: Producer) -> bool:
        if session_id in self._active:
            return False
        try:
            generation = await self.repository.begin(session_id, text)
        except GenerationConflict:
            return False
        task = asyncio.create_task(
            self._run_generation(generation, text, producer),
            name=f"chat-generation-{generation.generation_id}",
        )
        self._active[session_id] = _ActiveGeneration(
            generation, task, reports_progress=isinstance(producer, ProgressProducer),
        )
        return True

    async def current_progress(
        self, session_id: str, *, streaming_messages: set[str] | None = None,
    ) -> GenerationProgressData | None:
        async with self._lock:
            active = self._active.get(session_id)
            if active is None or (
                streaming_messages is not None
                and active.generation.assistant_message_id not in streaming_messages
            ):
                return None
            return active.progress

    async def _publish_progress(self, active: _ActiveGeneration, stage: ProgressStage) -> None:
        """Publish while holding the generation lock."""
        if active.progress is not None and active.progress.stage == stage:
            return
        self._progress_cursor += 1
        session_id = active.generation.session_id
        active.progress = GenerationProgressData(
            session_id=session_id,
            generation_id=active.generation.generation_id,
            stage=stage,
            cursor=self._progress_cursor,
        )
        await self.pubsub.publish(session_id, GenerationProgressEvent(data=active.progress))

    async def has_active(self, session_id: str) -> bool:
        async with self._lock:
            return session_id in self._active

    async def reconcile_after_restart(self, session_id: str) -> bool:
        """Reconcile only when this process has no live producer for the session."""
        async with self._lock:
            if session_id in self._active:
                return False
            payload = await self.repository.reconcile(session_id)
            if payload is None:
                return False
            await self.pubsub.publish(
                session_id,
                GenerationInterruptedEvent(
                    data=GenerationInterruptedData.model_validate(payload)
                ),
            )
            return True

    async def _is_current(self, active: _ActiveGeneration) -> bool:
        async with self._lock:
            return (
                self._active.get(active.generation.session_id) is active
                and active.accepting_chunks
            )

    async def _run_generation(
        self, generation: GenerationRecord, text: str, producer: Producer
    ) -> None:
        active = self._active[generation.session_id]
        async def report(stage: ProgressStage) -> None:
            async with self._lock:
                if self._active.get(generation.session_id) is active and active.accepting_chunks:
                    await self._publish_progress(active, stage)

        stream = (
            producer.run(generation.session_id, text, report)
            if isinstance(producer, ProgressProducer)
            else producer(generation.session_id, text)
        )
        try:
            async for chunk in stream:
                if isinstance(chunk, CoverageUpdate):
                    async with self._lock:
                        if self._active.get(generation.session_id) is active and active.accepting_chunks:
                            payload = await self.repository.append_coverage(generation, chunk.coverage)
                            active.progress = None
                            await self.pubsub.publish(
                                generation.session_id,
                                CoverageUpdatedEvent(data=CoverageUpdatedData.model_validate(payload)),
                            )
                    continue
                token = chunk.text if isinstance(chunk, AnnotatedChunk) else chunk
                if not token:
                    continue
                async with self._lock:
                    if self._active.get(generation.session_id) is not active or not active.accepting_chunks:
                        continue
                    if isinstance(chunk, AnnotatedChunk) and chunk.unit_coverage is not None:
                        payload = await self.repository.append(
                            generation, token, publication_kind=chunk.publication_kind,
                            answer_kind=chunk.answer_kind, knowledge=chunk.knowledge,
                            unit_ref=chunk.unit_ref, unit_coverage=chunk.unit_coverage,
                        )
                    elif isinstance(chunk, AnnotatedChunk) and chunk.unit_ref is not None:
                        payload = await self.repository.append(
                            generation, token, publication_kind=chunk.publication_kind,
                            answer_kind=chunk.answer_kind, knowledge=chunk.knowledge,
                            unit_ref=chunk.unit_ref,
                        )
                    elif isinstance(chunk, AnnotatedChunk) and chunk.publication_kind is not None:
                        payload = await self.repository.append(
                            generation, token, publication_kind=chunk.publication_kind,
                        )
                    elif isinstance(chunk, AnnotatedChunk) and chunk.knowledge is not None:
                        payload = await self.repository.append(
                            generation, token, answer_kind=chunk.answer_kind, knowledge=chunk.knowledge,
                        )
                    elif isinstance(chunk, AnnotatedChunk):
                        payload = await self.repository.append(
                            generation, token, answer_kind=chunk.answer_kind,
                        )
                    else:
                        payload = await self.repository.append(generation, token)
                    active.progress = None
                    await self.pubsub.publish(
                        generation.session_id,
                        TokenChunkEvent(data=TokenChunkData.model_validate(payload)),
                    )
            if await self._is_current(active):
                # Completion commit and publication form one coordinator
                # critical section. An interrupt either wins before this lock,
                # or observes ``accepting_chunks=False`` after the completed
                # event has reached every live subscriber.
                async with self._lock:
                    if (
                        self._active.get(generation.session_id) is active
                        and active.accepting_chunks
                    ):
                        payload = await self.repository.complete(generation)
                        active.progress = None
                        await self.pubsub.publish(
                            generation.session_id,
                            GenerationCompletedEvent(
                                data=GenerationCompletedData.model_validate(payload)
                            ),
                        )
                        active.accepting_chunks = False
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                if self._active.get(generation.session_id) is active and active.accepting_chunks:
                    await self.repository.fail(generation)
                    active.accepting_chunks = False
                    active.progress = None
                    await self.pubsub.publish(
                        generation.session_id,
                        ErrorEvent(data=ErrorData(
                            session_id=generation.session_id,
                            code="generation_unavailable",
                        )),
                    )
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass
            async with self._lock:
                if self._active.get(generation.session_id) is active and not active.interrupting:
                    self._active.pop(generation.session_id, None)

    async def interrupt(
        self,
        session_id: str,
        new_input: str | None,
        producer: Producer,
    ) -> bool:
        async with self._lock:
            active = self._active.get(session_id)
            if active is None or active.task.done() or not active.accepting_chunks:
                return False
            active.accepting_chunks = False
            active.interrupting = True
            if active.reports_progress:
                await self._publish_progress(active, "interrupting")
            active.task.cancel()
        try:
            try:
                await active.task
            except asyncio.CancelledError:
                pass
            try:
                payload = await self.repository.interrupt(active.generation)
            except GenerationConflict:
                return False
            async with self._lock:
                await self.pubsub.publish(
                    session_id,
                    GenerationInterruptedEvent(
                        data=GenerationInterruptedData.model_validate(payload)
                    ),
                )
                self._active.pop(session_id, None)
                if new_input is not None:
                    return await self._start_locked(session_id, new_input, producer)
                return True
        finally:
            async with self._lock:
                if self._active.get(session_id) is active:
                    self._active.pop(session_id, None)

    async def close(self) -> None:
        async with self._lock:
            tasks = [active.task for active in self._active.values()]
            for active in self._active.values():
                active.accepting_chunks = False
                if not active.interrupting:
                    active.task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
