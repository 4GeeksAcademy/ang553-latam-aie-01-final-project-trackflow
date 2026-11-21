"""Deterministic bilingual readability with technical failures kept out of results."""

from __future__ import annotations

import re
from typing import Any, Callable

from data.pipelines.rfp_response.contracts import ReadabilityEvaluation, ReadabilityMetric


class ReadabilityTechnicalError(RuntimeError):
    pass


_WORDS = re.compile(r"[A-Za-zÀ-ÿ]+")
_SENTENCES = re.compile(r"[.!?]+")
_VOWELS = re.compile(r"[aeiouáéíóúü]+", re.IGNORECASE)


def _average_sentence_words(text: str) -> float:
    return len(_WORDS.findall(text)) / max(1, len(_SENTENCES.findall(text)))


def evaluate_readability(text: str, *, language: str, non_narrative: bool = False,
                         english_factory: Callable[[str], Any] | None = None) -> ReadabilityEvaluation:
    if language not in {"en", "es"}:
        raise ValueError("readability language must be en or es")
    if non_narrative:
        return ReadabilityEvaluation(execution_status="not_applicable", language=language,
            readability_score=None, metrics=[], **{"pass": None},
            not_applicable_reason="allowlisted_non_narrative_artifact")
    normalized = " ".join(text.split())
    if not normalized:
        return ReadabilityEvaluation(execution_status="evaluated", language=language,
            readability_score=0.0,
            metrics=[ReadabilityMetric(name="average_sentence_words", value=0.0)],
            **{"pass": False}, not_applicable_reason=None)
    average = _average_sentence_words(normalized)
    if language == "es":
        words = _WORDS.findall(normalized)
        syllables = sum(max(1, len(_VOWELS.findall(word.casefold()))) for word in words)
        score = max(0.0, min(100.0, 206.835 - 62.3 * syllables / len(words) - average))
        return ReadabilityEvaluation(execution_status="evaluated", language="es",
            readability_score=score,
            metrics=[ReadabilityMetric(name="szigriszt_pazos", value=score),
                     ReadabilityMetric(name="average_sentence_words", value=average)],
            **{"pass": score >= 50.0 and average <= 25.0}, not_applicable_reason=None)
    if english_factory is None:
        words = _WORDS.findall(normalized)
        syllable_counts = [max(1, len(_VOWELS.findall(word.casefold()))) for word in words]
        syllables = sum(syllable_counts)
        complex_words = sum(value >= 3 for value in syllable_counts)
        syllables_per_word = syllables / len(words)
        ease = 206.835 - 1.015 * average - 84.6 * syllables_per_word
        grade = 0.39 * average + 11.8 * syllables_per_word - 15.59
        fog = 0.4 * (average + 100 * complex_words / len(words))
    else:
        try:
            result = english_factory(normalized)
            ease = float(result.flesch().score)
            grade = float(result.flesch_kincaid().score)
            fog = float(result.gunning_fog().score)
        except Exception as exc:
            raise ReadabilityTechnicalError("English readability computation failed") from exc
    score = max(0.0, min(100.0, ease))
    return ReadabilityEvaluation(execution_status="evaluated", language="en",
        readability_score=score,
        metrics=[ReadabilityMetric(name="flesch_reading_ease", value=ease),
                 ReadabilityMetric(name="flesch_kincaid_grade", value=grade),
                 ReadabilityMetric(name="gunning_fog", value=fog),
                 ReadabilityMetric(name="average_sentence_words", value=average)],
        **{"pass": score >= 50.0 and grade <= 12.0 and fog <= 14.0},
        not_applicable_reason=None)
