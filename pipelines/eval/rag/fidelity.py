"""Deterministic material-fidelity checks for recorded RAG answers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MaterialRequirement:
    """One required commercial fact with equivalent accepted formulations."""

    name: str
    any_patterns: tuple[str, ...]


@dataclass(frozen=True)
class FidelityAssessment:
    passed: bool
    matched: tuple[str, ...]
    missing: tuple[str, ...]


def parse_requirements(raw: list[dict[str, Any]]) -> tuple[MaterialRequirement, ...]:
    requirements: list[MaterialRequirement] = []
    for item in raw:
        name = str(item["name"]).strip()
        patterns = tuple(str(pattern) for pattern in item["any_patterns"])
        if not name or not patterns or any(not pattern for pattern in patterns):
            raise ValueError("material requirements must have a name and patterns")
        requirements.append(MaterialRequirement(name, patterns))
    if not requirements:
        raise ValueError("at least one material requirement is required")
    return tuple(requirements)


def assess_material_fidelity(
    answer: str,
    requirements: tuple[MaterialRequirement, ...],
) -> FidelityAssessment:
    """Require every material fact while allowing equivalent wordings per fact."""
    matched: list[str] = []
    missing: list[str] = []
    for requirement in requirements:
        found = any(
            re.search(pattern, answer, flags=re.IGNORECASE) is not None
            for pattern in requirement.any_patterns
        )
        (matched if found else missing).append(requirement.name)
    return FidelityAssessment(not missing, tuple(matched), tuple(missing))
