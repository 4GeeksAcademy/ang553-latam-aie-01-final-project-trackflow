"""Stable service-facing entrypoint for the persistence-neutral RFP graph."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from data.pipelines.rfp_intake.analyzer import RFPAnalyzer
from data.pipelines.rfp_intake.contracts import (
    ConversionMetrics,
    ReadabilityReport,
    RFPExecutionResult,
    RFPIntakeDecision,
    RFPIntakeDecisionSink,
    RFPResultSink,
)
from data.pipelines.rfp_intake.graph import RFPIntakeGraph


def process_rfp_ticket(
    *,
    ticket_id: UUID,
    markdown: str,
    conversion_metrics: ConversionMetrics,
    readability_metrics: ReadabilityReport,
    analyzer: RFPAnalyzer,
    sink: RFPResultSink,
    intake_sink: RFPIntakeDecisionSink | None = None,
    decision: RFPIntakeDecision | None = None,
    now: Callable[[], datetime] | None = None,
) -> RFPExecutionResult:
    return RFPIntakeGraph().execute(
        ticket_id=ticket_id,
        markdown=markdown,
        conversion_metrics=conversion_metrics,
        readability_metrics=readability_metrics,
        analyzer=analyzer,
        sink=sink,
        intake_sink=intake_sink,
        decision=decision,
        now=now,
    )
