"""Language-aware readability metrics over normalized Markdown."""

from __future__ import annotations

import importlib.metadata
import re
from typing import Any, Callable

from data.pipelines.rfp_intake.contracts import MetricValue, ReadabilityReport


_MARKDOWN_MARKS = re.compile(r"[#*_>`~|\[\]()]")
_WORDS = re.compile(r"[A-Za-zÀ-ÿ]+")
_ENGLISH = frozenset({"the", "and", "for", "with", "from", "delivery", "warehouse"})
_SPANISH = frozenset({"el", "la", "los", "las", "para", "con", "desde", "almacén"})


def normalize_markdown(markdown: str) -> str:
    return " ".join(_MARKDOWN_MARKS.sub(" ", markdown).split())


def detect_readability_language(text: str) -> str:
    words = [word.casefold() for word in _WORDS.findall(text)]
    english = sum(word in _ENGLISH for word in words)
    spanish = sum(word in _SPANISH for word in words)
    if english >= 2 and spanish == 0:
        return "en"
    if spanish >= 2 and english == 0:
        return "es"
    return "unknown"


def _package_version() -> str:
    try:
        return importlib.metadata.version("py-readability-metrics")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def compute_readability(
    markdown: str,
    *,
    readability_factory: Callable[[str], Any] | None = None,
) -> ReadabilityReport:
    text = normalize_markdown(markdown)
    language = detect_readability_language(text)
    version = _package_version()
    names = ("flesch_kincaid", "gunning_fog")
    if language != "en":
        reason = "formula_not_validated_for_document_language"
        return ReadabilityReport(
            metrics=[
                MetricValue(
                    name=name,
                    value=None,
                    status="not_applicable",
                    language=language,
                    method_version=version,
                    reason=reason,
                )
                for name in names
            ]
        )
    if readability_factory is None:
        try:
            from readability import Readability
        except ImportError:
            return ReadabilityReport(
                metrics=[
                    MetricValue(
                        name=name,
                        value=None,
                        status="failed",
                        language=language,
                        method_version=version,
                        reason="readability_dependency_unavailable",
                    )
                    for name in names
                ]
            )
        readability_factory = Readability
    try:
        readability = readability_factory(text)
        values = {
            "flesch_kincaid": float(readability.flesch_kincaid().score),
            "gunning_fog": float(readability.gunning_fog().score),
        }
    except Exception:
        return ReadabilityReport(
            metrics=[
                MetricValue(
                    name=name,
                    value=None,
                    status="failed",
                    language=language,
                    method_version=version,
                    reason="metric_computation_failed",
                )
                for name in names
            ]
        )
    return ReadabilityReport(
        metrics=[
            MetricValue(
                name=name,
                value=value,
                status="computed",
                language=language,
                method_version=version,
                reason=None,
            )
            for name, value in values.items()
        ]
    )
