"""Adversarial unit tests for MEM-092 consent coordination."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.pool import StaticPool
from sqlmodel import Session

from services.api.access_control import AccessContext
from services.api.agent_memory.consent import (
    ConsentClassificationInvalid,
    ConsentCoordinator,
    FourGeeksStructuredConsentClassifier,
)
from services.api.agent_memory.contracts import (
    ConsentAction,
    ConsentClassification,
    ConsentDisposition,
    ConsentRequest,
    ConsentSource,
    MemoryCategory,
    MemoryProposalCreate,
    MemoryScope,
    ProposalStatus,
)
from services.api.agent_memory.models import memory_decisions, memory_entries, memory_proposals
from services.api.agent_memory.service import (
    MemoryConflict,
    MemoryPolicyViolation,
    MemoryStore,
    canonical_key_for,
)
from services.api.auth_models import Role
from services.incidents.models import clients, operational_security_metadata


CLIENT_A = "11111111-1111-4111-8111-111111111111"
CLIENT_B = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    operational_security_metadata.create_all(engine)
    with Session(engine) as database:
        database.execute(
            insert(clients),
            [
                {
                    "client_id": CLIENT_A,
                    "display_name": "Consent Client A",
                    "is_active": True,
                    "created_by_user_id": "admin",
                },
                {
                    "client_id": CLIENT_B,
                    "display_name": "Consent Client B",
                    "is_active": True,
                    "created_by_user_id": "admin",
                },
            ],
        )
        database.commit()
        yield database


def access(user_id: str, client_id: str) -> AccessContext:
    return AccessContext(
        user_id=user_id,
        role=Role.manager,
        allowed_client_ids=frozenset({client_id}),
        sensitive_client_ids=frozenset(),
        global_access=False,
    )


def payload(
    conversation_id=None,
    *,
    client_id=CLIENT_A,
    interaction="support-turn",
    rule="SEUR covers the Zaragoza operational zone on weekdays.",
) -> MemoryProposalCreate:
    content = {"carrier": "SEUR", "country": "ES", "coverage_rule": rule}
    return MemoryProposalCreate(
        conversation_id=conversation_id or uuid4(),
        origin_interaction_id=interaction,
        origin_message_sha256=hashlib.sha256(interaction.encode()).hexdigest(),
        scope=MemoryScope.client,
        client_id=client_id,
        category=MemoryCategory.carrier_coverage,
        content=content,
        canonical_key=canonical_key_for(MemoryCategory.carrier_coverage, content),
    )


def pending(session: Session, manager: AccessContext, proposal_payload=None):
    return MemoryStore().create_proposal(
        session, proposal_payload or payload(), manager, now=NOW
    )


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (ConsentAction.approve, ConsentDisposition.approved),
        (ConsentAction.reject, ConsentDisposition.rejected),
    ],
)
def test_explicit_backend_action_approves_or_rejects(
    session: Session, action: ConsentAction, expected: ConsentDisposition
):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)
    result = ConsentCoordinator().handle(
        session,
        ConsentRequest(
            conversation_id=proposal.conversation_id,
            proposal_id=proposal.id,
            idempotency_key=f"explicit-{action.value}",
            action=action,
        ),
        manager,
        now=NOW,
    )
    assert result.disposition is expected
    assert (result.resolution.entry is not None) is (action is ConsentAction.approve)


def test_conservative_text_requires_an_exact_unqualified_utterance(session: Session):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)
    result = ConsentCoordinator().handle(
        session,
        ConsentRequest(
            conversation_id=proposal.conversation_id,
            proposal_id=proposal.id,
            idempotency_key="qualified-yes",
            user_text="Yes, but first show ticket TRF-000001",
        ),
        manager,
        now=NOW,
    )
    assert result.classification.action is ConsentAction.ambiguous
    assert result.disposition is ConsentDisposition.discarded
    assert result.resolution.entry is None


def test_exact_textual_approval_and_rejection_are_supported(session: Session):
    manager = access("manager-a", CLIENT_A)
    approved = pending(session, manager)
    approved_result = ConsentCoordinator().handle(
        session,
        ConsentRequest(
            conversation_id=approved.conversation_id,
            proposal_id=approved.id,
            idempotency_key="text-yes",
            user_text="Sí.",
        ),
        manager,
        now=NOW,
    )
    assert approved_result.disposition is ConsentDisposition.approved

    rejected = pending(session, manager, payload(interaction="second-turn"))
    rejected_result = ConsentCoordinator().handle(
        session,
        ConsentRequest(
            conversation_id=rejected.conversation_id,
            proposal_id=rejected.id,
            idempotency_key="text-no",
            user_text="No lo recuerdes.",
        ),
        manager,
        now=NOW,
    )
    assert rejected_result.disposition is ConsentDisposition.rejected


def test_edit_creates_replacement_and_requires_fresh_confirmation(session: Session):
    manager = access("manager-a", CLIENT_A)
    conversation = uuid4()
    original = pending(session, manager, payload(conversation))
    edited_payload = payload(
        conversation,
        interaction="edited-turn",
        rule="SEUR covers the operational zone on Tuesdays only.",
    )
    edited = ConsentCoordinator().handle(
        session,
        ConsentRequest(
            conversation_id=conversation,
            proposal_id=original.id,
            idempotency_key="edit-original",
            action=ConsentAction.edit,
            edited_proposal=edited_payload,
        ),
        manager,
        now=NOW,
    )
    assert edited.disposition is ConsentDisposition.replacement_pending
    assert edited.resolution.entry is None
    replacement = edited.resolution.replacement_proposal
    assert replacement is not None and replacement.status is ProposalStatus.pending
    assert ConsentCoordinator().recover_pending(session, conversation, manager) == replacement

    confirmed = ConsentCoordinator().handle(
        session,
        ConsentRequest(
            conversation_id=conversation,
            proposal_id=replacement.id,
            idempotency_key="approve-replacement",
            action=ConsentAction.approve,
        ),
        manager,
        now=NOW + timedelta(minutes=1),
    )
    assert confirmed.disposition is ConsentDisposition.approved
    assert confirmed.resolution.entry is not None
    assert confirmed.resolution.entry.content == edited_payload.content


def test_edit_http_retry_returns_the_same_replacement(session: Session):
    manager = access("manager-a", CLIENT_A)
    conversation = uuid4()
    original = pending(session, manager, payload(conversation))
    replacement_payload = payload(
        conversation,
        interaction="retry-edit",
        rule="SEUR covers the operational zone on Fridays only.",
    )
    request = ConsentRequest(
        conversation_id=conversation,
        proposal_id=original.id,
        idempotency_key="retry-edit-request",
        action=ConsentAction.edit,
        edited_proposal=replacement_payload,
    )
    coordinator = ConsentCoordinator()
    first = coordinator.handle(session, request, manager, now=NOW)
    retry = coordinator.handle(session, request, manager, now=NOW)
    assert retry.resolution.decision.id == first.resolution.decision.id
    assert retry.resolution.replacement_proposal == first.resolution.replacement_proposal


def test_ambiguous_silence_discards_without_activating(session: Session):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)
    result = ConsentCoordinator().handle(
        session,
        ConsentRequest(
            conversation_id=proposal.conversation_id,
            proposal_id=proposal.id,
            idempotency_key="silence",
            user_text="   ",
        ),
        manager,
        now=NOW,
    )
    assert result.disposition is ConsentDisposition.discarded
    assert result.resolution.entry is None


def test_topic_change_discards_and_returns_new_question_to_support(session: Session):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)
    result = ConsentCoordinator().handle(
        session,
        ConsentRequest(
            conversation_id=proposal.conversation_id,
            proposal_id=proposal.id,
            idempotency_key="topic-change",
            action=ConsentAction.topic_change,
            user_text="What is the status of ticket TRF-000001?",
        ),
        manager,
        now=NOW,
    )
    assert result.disposition is ConsentDisposition.discarded
    assert result.continue_support_question is True
    assert ConsentCoordinator().recover_pending(
        session, proposal.conversation_id, manager
    ) is None


def test_http_retry_with_same_idempotency_key_returns_same_decision(session: Session):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)
    request = ConsentRequest(
        conversation_id=proposal.conversation_id,
        proposal_id=proposal.id,
        idempotency_key="http-request-id",
        action=ConsentAction.approve,
    )
    first = ConsentCoordinator().handle(session, request, manager, now=NOW)
    retry = ConsentCoordinator().handle(session, request, manager, now=NOW)
    assert retry.resolution.decision.id == first.resolution.decision.id
    assert retry.resolution.entry == first.resolution.entry


def test_second_different_response_cannot_change_resolved_consent(session: Session):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)
    coordinator = ConsentCoordinator()
    coordinator.handle(
        session,
        ConsentRequest(
            conversation_id=proposal.conversation_id,
            proposal_id=proposal.id,
            idempotency_key="first-response",
            action=ConsentAction.approve,
        ),
        manager,
        now=NOW,
    )
    with pytest.raises(MemoryConflict):
        coordinator.handle(
            session,
            ConsentRequest(
                conversation_id=proposal.conversation_id,
                proposal_id=proposal.id,
                idempotency_key="second-response",
                action=ConsentAction.reject,
            ),
            manager,
            now=NOW,
        )
    assert len(session.execute(select(memory_decisions)).scalars().all()) == 1


def test_expired_proposal_never_activates_memory(session: Session):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)
    result = ConsentCoordinator().handle(
        session,
        ConsentRequest(
            conversation_id=proposal.conversation_id,
            proposal_id=proposal.id,
            idempotency_key="late-approval",
            action=ConsentAction.approve,
        ),
        manager,
        now=NOW + timedelta(hours=25),
    )
    assert result.disposition is ConsentDisposition.expired
    assert result.resolution.entry is None


def test_prohibited_content_is_revalidated_at_approval(session: Session):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)
    session.execute(
        update(memory_proposals)
        .where(memory_proposals.c.id == str(proposal.id))
        .values(
            content={
                "carrier": "SEUR",
                "country": "ES",
                "coverage_rule": "Deliver to Calle Mayor 12.",
            }
        )
    )
    session.commit()
    with pytest.raises(MemoryPolicyViolation):
        ConsentCoordinator().handle(
            session,
            ConsentRequest(
                conversation_id=proposal.conversation_id,
                proposal_id=proposal.id,
                idempotency_key="prohibited-approval",
                action=ConsentAction.approve,
            ),
            manager,
            now=NOW,
        )
    stored = session.execute(
        select(memory_proposals.c.status).where(memory_proposals.c.id == str(proposal.id))
    ).scalar_one()
    assert stored == ProposalStatus.pending.value
    assert session.execute(select(memory_entries)).first() is None


def test_prohibited_edit_keeps_original_pending(session: Session):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)
    forbidden = payload(
        proposal.conversation_id,
        interaction="forbidden-edit",
    ).model_copy(
        update={
            "content": {
                "carrier": "SEUR",
                "country": "ES",
                "coverage_rule": "Deliver to C/ Mayor 12.",
            }
        }
    )
    with pytest.raises(MemoryPolicyViolation):
        ConsentCoordinator().handle(
            session,
            ConsentRequest(
                conversation_id=proposal.conversation_id,
                proposal_id=proposal.id,
                idempotency_key="forbidden-edit",
                action=ConsentAction.edit,
                edited_proposal=forbidden,
            ),
            manager,
            now=NOW,
        )
    assert ConsentCoordinator().recover_pending(
        session, proposal.conversation_id, manager
    ) == proposal


def test_pending_recovery_is_scoped_to_conversation_and_identity(session: Session):
    manager_a = access("manager-a", CLIENT_A)
    manager_b = access("manager-b", CLIENT_B)
    proposal = pending(session, manager_a)
    coordinator = ConsentCoordinator()
    assert coordinator.recover_pending(session, proposal.conversation_id, manager_a) == proposal
    assert coordinator.recover_pending(session, proposal.conversation_id, manager_b) is None


class FixedClassifier:
    def __init__(self, action: ConsentAction, confidence: float, proposal_id=None):
        self.action = action
        self.confidence = confidence
        self.proposal_id = proposal_id

    def classify(self, _text, proposal):
        return ConsentClassification(
            proposal_id=self.proposal_id or proposal.id,
            action=self.action,
            confidence=self.confidence,
            source=ConsentSource.classifier,
        )


def test_low_confidence_structured_classification_is_conservatively_discarded(
    session: Session,
):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)
    result = ConsentCoordinator(
        classifier=FixedClassifier(ConsentAction.approve, confidence=0.5)
    ).handle(
        session,
        ConsentRequest(
            conversation_id=proposal.conversation_id,
            proposal_id=proposal.id,
            idempotency_key="low-confidence",
            user_text="sounds good maybe",
        ),
        manager,
        now=NOW,
    )
    assert result.classification.action is ConsentAction.ambiguous
    assert result.disposition is ConsentDisposition.discarded


def test_classifier_cannot_answer_for_another_proposal(session: Session):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)
    coordinator = ConsentCoordinator(
        classifier=FixedClassifier(
            ConsentAction.approve, confidence=1.0, proposal_id=uuid4()
        )
    )
    with pytest.raises(MemoryConflict, match="another proposal"):
        coordinator.handle(
            session,
            ConsentRequest(
                conversation_id=proposal.conversation_id,
                proposal_id=proposal.id,
                idempotency_key="wrong-target",
                user_text="yes",
            ),
            manager,
            now=NOW,
        )


def test_fourgeeks_consent_classifier_uses_json_schema_and_separates_question(
    session: Session,
):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)

    class Gateway:
        def __init__(self):
            self.kwargs = None

        def generate_structured(self, **kwargs):
            self.kwargs = kwargs
            return json.dumps(
                {
                    "action": "approve",
                    "confidence": 0.99,
                    "edited_content": [],
                    "support_questions": ["What is the returns policy?"],
                }
            )

    gateway = Gateway()
    result = FourGeeksStructuredConsentClassifier(
        gateway, timeout_seconds=2.5
    ).classify(
        "Yes, and what is the returns policy?",
        proposal,
    )
    assert result.action is ConsentAction.approve
    assert result.support_question == "What is the returns policy?"
    assert result.proposal_id == proposal.id
    assert gateway.kwargs["schema_name"] == "trackflow_memory_consent"
    assert gateway.kwargs["timeout_seconds"] == 2.5
    assert gateway.kwargs["schema"]["additionalProperties"] is False
    assert set(gateway.kwargs["schema"]["required"]) == set(
        gateway.kwargs["schema"]["properties"]
    )


@pytest.mark.parametrize(
    ("category", "content", "expected_fields"),
    [
        (
            MemoryCategory.carrier_coverage,
            {
                "carrier": "SEUR",
                "country": "ES",
                "coverage_rule": "SEUR covers the operational zone.",
            },
            {"carrier", "country", "coverage_rule"},
        ),
        (
            MemoryCategory.recurring_incident,
            {
                "country": "ES",
                "pattern": "Repeated delayed handoff.",
                "operational_context": "B2B deliveries.",
                "recurrence_count": 2,
            },
            {"country", "pattern", "operational_context", "recurrence_count"},
        ),
        (
            MemoryCategory.b2b_report_preference,
            {
                "preference_type": "format",
                "preference": "Use a monthly CSV report.",
            },
            {"preference_type", "preference"},
        ),
    ],
)
def test_fourgeeks_consent_schema_is_closed_and_category_specific(
    session: Session,
    category: MemoryCategory,
    content: dict[str, object],
    expected_fields: set[str],
):
    manager = access("manager-a", CLIENT_A)
    base = pending(session, manager)
    proposal = base.model_copy(update={"category": category, "content": content})

    class Gateway:
        def __init__(self):
            self.schema = None

        def generate_structured(self, **kwargs):
            self.schema = kwargs["schema"]
            return json.dumps(
                {
                    "action": "ambiguous",
                    "confidence": 0.99,
                    "edited_content": [],
                    "support_questions": [],
                }
            )

    gateway = Gateway()
    result = FourGeeksStructuredConsentClassifier(gateway).classify(
        "Unclear response.", proposal
    )
    assert result.action is ConsentAction.ambiguous
    assert gateway.schema is not None

    object_fields: list[set[str]] = []

    def assert_closed(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                properties = set(node.get("properties", {}))
                assert set(node.get("required", [])) == properties
                object_fields.append(properties)
            for value in node.values():
                assert_closed(value)
        elif isinstance(node, list):
            for value in node:
                assert_closed(value)

    assert_closed(gateway.schema)
    assert expected_fields in object_fields
    if "country" in expected_fields:
        assert '"enum": ["US", "ES"]' in json.dumps(gateway.schema)


def test_fourgeeks_consent_classifier_reports_validation_failure(session: Session):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)

    class Gateway:
        def generate_structured(self, **_kwargs):
            return json.dumps({"action": "ambiguous", "confidence": 0.99})

    with pytest.raises(ConsentClassificationInvalid) as captured:
        FourGeeksStructuredConsentClassifier(Gateway()).classify(
            "Unclear response.", proposal
        )
    assert captured.value.diagnostic == "validation_error"


def test_fourgeeks_consent_classifier_returns_closed_b2b_edit(session: Session):
    manager = access("manager-a", CLIENT_A)
    base = pending(session, manager)
    proposal = base.model_copy(
        update={
            "category": MemoryCategory.b2b_report_preference,
            "content": {
                "preference_type": "format",
                "preference": "Use a monthly CSV report.",
            },
        }
    )
    replacement = {
        "preference_type": "metric_priority",
        "preference": "Prioritize delivery exceptions.",
    }

    class Gateway:
        def generate_structured(self, **_kwargs):
            return json.dumps(
                {
                    "action": "edit",
                    "confidence": 0.99,
                    "edited_content": [replacement],
                    "support_questions": [],
                }
            )

    result = FourGeeksStructuredConsentClassifier(Gateway()).classify(
        "Use delivery exceptions instead.", proposal
    )
    assert result.action is ConsentAction.edit
    assert result.edited_content == replacement
    assert result.support_question is None


def test_fourgeeks_consent_classifier_rejects_invalid_json(session: Session):
    manager = access("manager-a", CLIENT_A)
    proposal = pending(session, manager)

    class Gateway:
        def generate_structured(self, **_kwargs):
            return "{not-json"

    with pytest.raises(ConsentClassificationInvalid) as captured:
        FourGeeksStructuredConsentClassifier(Gateway()).classify("yes", proposal)
    assert captured.value.diagnostic == "invalid_response"
