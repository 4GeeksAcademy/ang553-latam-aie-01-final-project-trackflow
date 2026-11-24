from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import subprocess
from threading import Barrier, Lock, get_ident
import time
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from data.pipelines.rfp_intake.analyzer import (
    FourGeeksRFPAnalyzer,
    StructuredOutputError,
)
from data.pipelines.rfp_intake.contracts import (
    ClassificationDecision,
    ConversionMetrics,
    DepartmentId,
    DepartmentSection,
    DepartmentSectionReference,
    KeyAspect,
    MetricValue,
    OpenQuestion,
    ReadabilityReport,
    RFPHandoff,
    RFPMetadata,
    RFPMetadataDraft,
    ServiceRequest,
    SourceReference,
    SynthesisDraft,
)
from data.pipelines.rfp_intake.graph import RFPGraphError, RFPIntakeGraph
from data.pipelines.rfp_intake.pdf_conversion import (
    ConverterOutput,
    MarkItDownConverter,
    PDFConversionError,
    pdf_to_markdown,
)
from data.pipelines.rfp_intake.readability import (
    compute_readability,
    detect_readability_language,
)
from data.pipelines.rfp_intake.routing import department_extract, selected_departments


TICKET_ID = UUID("11111111-1111-4111-8111-111111111111")
MARKDOWN = """# Request for proposal
ModaViva requests warehouse storage for 15,000 SKU in Zaragoza.
We use our own carrier and do not request last mile delivery.
We require return processing and reverse logistics support.
Proposal deadline is 2026-08-28.
"""


def ref(line: int) -> SourceReference:
    return SourceReference(line_start=line, line_end=line)


def conversion_metrics() -> ConversionMetrics:
    return ConversionMetrics(
        sha256="a" * 64,
        bytes=100,
        page_count=1,
        pages_with_text=1,
        markdown_reference=f"rfp_intake/{TICKET_ID}/source.md",
        extracted_characters=len(MARKDOWN),
        markdown_characters=len(MARKDOWN),
        converter="fake",
        converter_version="1",
    )


def readability_metrics() -> ReadabilityReport:
    return ReadabilityReport(
        metrics=[
            MetricValue(
                name="flesch_kincaid",
                value=8.0,
                status="computed",
                language="en",
                method_version="test",
                reason=None,
            )
        ]
    )


def valid_metadata_draft() -> RFPMetadataDraft:
    return RFPMetadataDraft(
        rfp_id="MV-2026",
        client_name="ModaViva",
        client_country_code="ES",
        client_country_original="Spain",
        operation_country_codes=["ES"],
        services_requested=[
            ServiceRequest(service_id=DepartmentId.warehouse, requested=True, source_ref=ref(2)),
            ServiceRequest(service_id=DepartmentId.lastmile, requested=False, source_ref=ref(3)),
            ServiceRequest(service_id=DepartmentId.reverse, requested=True, source_ref=ref(4)),
        ],
        monthly_volume_value=None,
        monthly_volume_unit=None,
        monthly_volume_source_ref=None,
        deadline=date(2026, 8, 28),
        deadline_source_ref=ref(5),
        onboarding_target=None,
        onboarding_source_ref=None,
        budget_range=None,
        open_questions=["Monthly order volume is not provided."],
    )


class RecordingSink:
    def __init__(self) -> None:
        self.discards: list[dict] = []
        self.completions: list[dict] = []

    def persist_discard(self, **kwargs) -> None:
        self.discards.append(kwargs)

    def persist_complete(self, **kwargs) -> None:
        self.completions.append(kwargs)


class ConcurrentAnalyzer:
    def __init__(self) -> None:
        self.barrier = Barrier(2)
        self.worker_threads: set[int] = set()
        self.worker_times: list[tuple[float, float]] = []
        self.synthesized_at: float | None = None
        self._lock = Lock()

    def classify(self, _markdown: str) -> ClassificationDecision:
        return ClassificationDecision(
            is_valid_rfp=True,
            reason_code="legitimate_rfp",
            evidence_refs=[ref(1)],
        )

    def extract_metadata(self, _markdown: str) -> RFPMetadataDraft:
        return valid_metadata_draft()

    def analyze_department(
        self,
        *,
        metadata: RFPMetadata,
        department_id: DepartmentId,
        relevant_extract: str,
    ) -> DepartmentSection:
        assert metadata.currency == "EUR"
        assert relevant_extract and len(relevant_extract) < len(MARKDOWN) + 50
        started = time.monotonic()
        self.barrier.wait(timeout=2)
        time.sleep(0.02)
        finished = time.monotonic()
        with self._lock:
            self.worker_threads.add(get_ident())
            self.worker_times.append((started, finished))
        source_line = 2 if department_id is DepartmentId.warehouse else 4
        return DepartmentSection(
            department_id=department_id,
            key_aspects=[
                KeyAspect(
                    fact=f"Grounded {department_id.value} requirement",
                    source_ref=ref(source_line),
                    requested_action="Confirm operational response",
                )
            ],
            open_questions=[],
            source_references=[ref(source_line)],
            detected_conflicts=[],
        )

    def synthesize(self, *, metadata: RFPMetadata, sections: list[DepartmentSection]) -> SynthesisDraft:
        assert [item.department_id for item in sections] == [
            DepartmentId.warehouse,
            DepartmentId.reverse,
        ]
        self.synthesized_at = time.monotonic()
        assert self.synthesized_at >= max(end for _, end in self.worker_times)
        return SynthesisDraft(
            summary_for_sales="Ask Ana about storage and Sofía about returns.",
            open_questions=[
                OpenQuestion(
                    question="What is the monthly order volume?",
                    reason="Only SKU count is present.",
                    source_ref=ref(2),
                )
            ],
            detected_conflicts=[],
        )


class DiscardAnalyzer:
    worker_called = False

    def classify(self, _markdown: str) -> ClassificationDecision:
        return ClassificationDecision(
            is_valid_rfp=False,
            reason_code="vendor_pitch",
            evidence_refs=[ref(1)],
        )

    def extract_metadata(self, _markdown: str):
        raise AssertionError("metadata must not run for a discarded document")

    def analyze_department(self, **_kwargs):
        self.worker_called = True
        raise AssertionError("worker must not run for a discarded document")

    def synthesize(self, **_kwargs):
        raise AssertionError("synthesizer must not run for a discarded document")


def test_graph_runs_parallel_workers_then_one_synthesizer() -> None:
    analyzer = ConcurrentAnalyzer()
    sink = RecordingSink()
    result = RFPIntakeGraph().execute(
        ticket_id=TICKET_ID,
        markdown=MARKDOWN,
        conversion_metrics=conversion_metrics(),
        readability_metrics=readability_metrics(),
        analyzer=analyzer,
        sink=sink,
        now=lambda: datetime(2026, 11, 16, tzinfo=timezone.utc),
    )

    assert result.outcome == "analysis_complete"
    assert result.metadata is not None and result.metadata.currency == "EUR"
    assert [section.department_id for section in result.sections] == [
        DepartmentId.warehouse,
        DepartmentId.reverse,
    ]
    assert len(analyzer.worker_threads) == 2
    assert len(sink.completions) == 1
    assert not sink.discards
    assert result.handoff is not None
    assert [ref.responsible for ref in result.handoff.department_section_references] == [
        "Ana Whitfield",
        "Sofía Ramos",
    ]


def test_send_payload_excludes_full_markdown() -> None:
    metadata = RFPMetadata(
        **valid_metadata_draft().model_dump(),
        currency="EUR",
        departments_needed=[DepartmentId.warehouse, DepartmentId.reverse],
    )

    sends = RFPIntakeGraph._dispatch_workers({
        "ticket_id": TICKET_ID,
        "markdown": MARKDOWN,
        "metadata": metadata,
        "selected_departments": metadata.departments_needed,
    })

    assert len(sends) == 2
    for send in sends:
        assert set(send.arg) == {
            "department_id",
            "metadata",
            "relevant_extract",
            "source_line_count",
        }
        assert "markdown" not in send.arg
        assert send.arg["source_line_count"] == len(MARKDOWN.splitlines())
        assert send.arg["metadata"] is metadata
        assert send.arg["relevant_extract"]


def test_non_rfp_stops_without_metadata_workers_or_handoff() -> None:
    analyzer = DiscardAnalyzer()
    sink = RecordingSink()
    result = RFPIntakeGraph().execute(
        ticket_id=TICKET_ID,
        markdown="RapidShip offers TrackFlow discounted carrier rates.",
        conversion_metrics=conversion_metrics(),
        readability_metrics=readability_metrics(),
        analyzer=analyzer,
        sink=sink,
    )
    assert result.outcome == "discarded"
    assert result.sections == [] and result.handoff is None
    assert len(sink.discards) == 1 and not sink.completions
    assert not analyzer.worker_called


def test_routing_uses_requested_services_not_negated_mentions() -> None:
    metadata = valid_metadata_draft()
    assert selected_departments(metadata) == [DepartmentId.warehouse, DepartmentId.reverse]
    assert "L2:" in department_extract(MARKDOWN, DepartmentId.warehouse)
    assert "L4:" in department_extract(MARKDOWN, DepartmentId.reverse)


def test_handoff_rejects_unapproved_responsible() -> None:
    with pytest.raises(ValidationError, match="responsible"):
        DepartmentSectionReference(
            department_id=DepartmentId.warehouse,
            responsible="Invented Person",
            ticket_id=TICKET_ID,
        )


class FakeConverter:
    name = "fake"
    version = "1.0"

    def convert(
        self, _path: Path, *, max_pages: int, timeout_seconds: float
    ) -> ConverterOutput:
        assert max_pages == 10
        assert timeout_seconds == 5
        return ConverterOutput(
            markdown="# Converted\nUseful request text.",
            page_count=1,
            pages_with_text=1,
        )


def test_pdf_conversion_validates_signature_and_records_hash(tmp_path: Path) -> None:
    source = tmp_path / "request.pdf"
    source.write_bytes(b"%PDF-1.4\n1 0 obj<</Type /Page>>endobj")
    result = pdf_to_markdown(
        source,
        markdown_reference=f"rfp_intake/{TICKET_ID}/source.md",
        max_pages=10,
        timeout_seconds=5,
        converter=FakeConverter(),
    )
    assert result.markdown.startswith("# Converted")
    assert result.metrics.page_count == 1
    assert result.metrics.pages_with_text == 1
    assert result.metrics.markdown_reference.endswith("/source.md")
    assert result.metrics.converter == "fake"
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(PDFConversionError, match="not a PDF"):
        pdf_to_markdown(
            bad,
            markdown_reference=f"rfp_intake/{TICKET_ID}/source.md",
            max_pages=10,
            timeout_seconds=5,
            converter=FakeConverter(),
        )


def test_conversion_metrics_reject_incoherent_pages() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        ConversionMetrics(
            sha256="a" * 64,
            bytes=100,
            page_count=1,
            pages_with_text=2,
            markdown_reference=f"rfp_intake/{TICKET_ID}/source.md",
            extracted_characters=10,
            markdown_characters=10,
            converter="fake",
            converter_version="1",
        )


def test_markitdown_converter_enforces_subprocess_timeout(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "request.pdf"
    source.write_bytes(b"%PDF-1.4")

    def expire(*_args, **kwargs):
        assert kwargs["timeout"] == 0.25
        raise subprocess.TimeoutExpired("pdf-worker", kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", expire)
    with pytest.raises(PDFConversionError, match="timed out"):
        MarkItDownConverter().convert(source, max_pages=12, timeout_seconds=0.25)


def test_pdf_conversion_rejects_parser_page_count_above_limit(tmp_path: Path) -> None:
    source = tmp_path / "request.pdf"
    source.write_bytes(b"%PDF-1.4")

    class TooLongConverter(FakeConverter):
        def convert(self, _path: Path, *, max_pages: int, timeout_seconds: float):
            return ConverterOutput(markdown="usable", page_count=max_pages + 1, pages_with_text=1)

    with pytest.raises(PDFConversionError, match="page limit"):
        pdf_to_markdown(
            source,
            markdown_reference=f"rfp_intake/{TICKET_ID}/source.md",
            max_pages=10,
            timeout_seconds=5,
            converter=TooLongConverter(),
        )


class FakeReadabilityResult:
    def __init__(self, score: float) -> None:
        self.score = score


class FakeReadability:
    def __init__(self, _text: str) -> None:
        pass

    def flesch_kincaid(self) -> FakeReadabilityResult:
        return FakeReadabilityResult(7.5)

    def gunning_fog(self) -> FakeReadabilityResult:
        return FakeReadabilityResult(9.25)


def test_readability_computes_english_and_marks_spanish_not_applicable() -> None:
    english = compute_readability(
        "The warehouse and delivery request is for the customer.",
        readability_factory=FakeReadability,
    )
    assert [metric.status for metric in english.metrics] == ["computed", "computed"]
    spanish = compute_readability("El almacén y la entrega para los pedidos.")
    assert all(metric.status == "not_applicable" for metric in spanish.metrics)


def test_language_detection_allows_bilingual_headings_with_dominant_body() -> None:
    spanish_body = (
        "REQUEST FOR PROPOSAL. The solicitud para el almacén con la empresa, "
        "para los pedidos con almacenamiento."
    )
    english_body = (
        "Solicitud. The warehouse and delivery request is for the customer "
        "with warehouse support."
    )
    balanced = "The request para el almacén and warehouse support."

    assert detect_readability_language(spanish_body) == "es"
    assert detect_readability_language(english_body) == "en"
    assert detect_readability_language(balanced) == "unknown"


def test_join_rejects_partial_results() -> None:
    state = {
        "selected_departments": [DepartmentId.warehouse, DepartmentId.reverse],
        "department_results": [
            DepartmentSection(
                department_id=DepartmentId.warehouse,
                key_aspects=[
                    KeyAspect(fact="Storage", source_ref=ref(2), requested_action="Confirm")
                ],
                open_questions=[],
                source_references=[ref(2)],
                detected_conflicts=[],
            )
        ],
    }
    with pytest.raises(RFPGraphError, match="do not match"):
        RFPIntakeGraph._validate_join(state)  # type: ignore[arg-type]


class FakeGateway:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict] = []

    def generate_structured(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return self.response


@pytest.mark.parametrize(
    ("reason_code", "is_valid_rfp"),
    [
        ("legitimate_rfp", True),
        ("vendor_pitch", False),
        ("not_a_request", False),
        ("outside_trackflow_scope", False),
        ("ambiguous", False),
    ],
)
def test_classification_contract_accepts_each_coherent_reason_mapping(
    reason_code: str, is_valid_rfp: bool
) -> None:
    decision = ClassificationDecision(
        is_valid_rfp=is_valid_rfp,
        reason_code=reason_code,
        evidence_refs=[ref(1)],
    )

    assert decision.reason_code == reason_code
    assert decision.is_valid_rfp is is_valid_rfp


@pytest.mark.parametrize(
    ("reason_code", "is_valid_rfp"),
    [
        ("legitimate_rfp", False),
        ("vendor_pitch", True),
    ],
)
def test_classification_contract_rejects_both_incoherent_mapping_classes(
    reason_code: str, is_valid_rfp: bool
) -> None:
    with pytest.raises(ValidationError, match="classification result and reason disagree"):
        ClassificationDecision(
            is_valid_rfp=is_valid_rfp,
            reason_code=reason_code,
            evidence_refs=[ref(1)],
        )


def test_fourgeeks_classifier_uses_closed_schema_and_accepts_valid_json() -> None:
    gateway = FakeGateway(
        '{"is_valid_rfp":true,"reason_code":"legitimate_rfp",'
        '"evidence_refs":[{"line_start":1,"line_end":1}]}'
    )
    decision = FourGeeksRFPAnalyzer(gateway).classify(MARKDOWN)
    assert decision.is_valid_rfp
    assert gateway.calls[0]["schema_name"] == "trackflow_rfp_classification"
    assert gateway.calls[0]["schema"]["additionalProperties"] is False
    assert "<untrusted_rfp_markdown>" in gateway.calls[0]["user"]


def test_fourgeeks_classifier_prompt_contains_complete_reason_mapping() -> None:
    gateway = FakeGateway(
        '{"is_valid_rfp":true,"reason_code":"legitimate_rfp",'
        '"evidence_refs":[{"line_start":1,"line_end":1}]}'
    )

    FourGeeksRFPAnalyzer(gateway).classify("Generic client request")
    system = gateway.calls[0]["system"]

    assert "is_valid_rfp=true if and only if reason_code=legitimate_rfp" in system
    assert (
        "is_valid_rfp=false when reason_code is vendor_pitch, not_a_request, "
        "outside_trackflow_scope, or ambiguous"
    ) in system
    assert "use ambiguous with is_valid_rfp=false instead" in system
    assert "Cite real one-based Markdown lines" in system
    assert "ModaViva" not in system


def test_fourgeeks_classifier_prompt_does_not_force_document_outcome() -> None:
    legitimate = FakeGateway(
        '{"is_valid_rfp":true,"reason_code":"legitimate_rfp",'
        '"evidence_refs":[{"line_start":1,"line_end":1}]}'
    )
    rejected = FakeGateway(
        '{"is_valid_rfp":false,"reason_code":"not_a_request",'
        '"evidence_refs":[{"line_start":1,"line_end":1}]}'
    )

    accepted = FourGeeksRFPAnalyzer(legitimate).classify("Please prepare a proposal")
    discarded = FourGeeksRFPAnalyzer(rejected).classify("Irregular verbs exercise")

    assert accepted.is_valid_rfp is True
    assert discarded.is_valid_rfp is False
    assert legitimate.calls[0]["system"] == rejected.calls[0]["system"]
    assert legitimate.calls[0]["user"] != rejected.calls[0]["user"]


def test_ambiguous_mapping_remains_semantically_rejected_after_contract_validation() -> None:
    gateway = FakeGateway(
        '{"is_valid_rfp":false,"reason_code":"ambiguous",'
        '"evidence_refs":[{"line_start":1,"line_end":1}]}'
    )

    with pytest.raises(StructuredOutputError, match="ambiguous") as captured:
        FourGeeksRFPAnalyzer(gateway).classify("Insufficiently clear request")

    assert captured.value.diagnostic.category == "semantic_validation"
    assert captured.value.diagnostic.path == "reason_code"


def test_structured_not_a_request_discards_without_workers_or_handoff() -> None:
    gateway = FakeGateway(
        '{"is_valid_rfp":false,"reason_code":"not_a_request",'
        '"evidence_refs":[{"line_start":1,"line_end":1}]}'
    )
    sink = RecordingSink()

    result = RFPIntakeGraph().execute(
        ticket_id=TICKET_ID,
        markdown="English and Spanish verbs\nbe - was/were - been",
        conversion_metrics=conversion_metrics(),
        readability_metrics=readability_metrics(),
        analyzer=FourGeeksRFPAnalyzer(gateway),
        sink=sink,
    )

    assert result.outcome == "discarded"
    assert result.metadata is None and result.sections == [] and result.handoff is None
    assert len(sink.discards) == 1 and not sink.completions
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["schema_name"] == "trackflow_rfp_classification"


def test_fourgeeks_classifier_rejects_extra_or_ambiguous_output_for_retry() -> None:
    extra = FakeGateway(
        '{"is_valid_rfp":false,"reason_code":"vendor_pitch",'
        '"evidence_refs":[{"line_start":1,"line_end":1}],"unsafe":"x"}'
    )
    with pytest.raises(StructuredOutputError):
        FourGeeksRFPAnalyzer(extra).classify(MARKDOWN)
    ambiguous = FakeGateway(
        '{"is_valid_rfp":false,"reason_code":"ambiguous",'
        '"evidence_refs":[{"line_start":1,"line_end":1}]}'
    )
    with pytest.raises(StructuredOutputError, match="ambiguous"):
        FourGeeksRFPAnalyzer(ambiguous).classify(MARKDOWN)


def test_handoff_requires_exact_selected_workstream_order() -> None:
    created = datetime(2026, 11, 16, tzinfo=timezone.utc)
    with pytest.raises(ValidationError, match="must match"):
        RFPHandoff(
            ticket_id=TICKET_ID,
            metadata_reference=TICKET_ID,
            summary_for_sales="Summary",
            selected_departments=[DepartmentId.warehouse, DepartmentId.reverse],
            department_section_references=[
                DepartmentSectionReference(
                    department_id=DepartmentId.reverse,
                    responsible="Sofía Ramos",
                    ticket_id=TICKET_ID,
                ),
                DepartmentSectionReference(
                    department_id=DepartmentId.warehouse,
                    responsible="Ana Whitfield",
                    ticket_id=TICKET_ID,
                ),
            ],
            open_questions=[],
            detected_conflicts=[],
            created_at=created,
        )
