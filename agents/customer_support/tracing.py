"""Bounded, content-free execution traces for the support graph."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from services.rag.contracts import RAGProviderError


_TRACE_LOGGER = logging.getLogger("agent.customer_support.trace")
_TRACE_LOGGER.setLevel(logging.INFO)
if not _TRACE_LOGGER.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _TRACE_LOGGER.addHandler(_handler)
_TRACE_LOGGER.propagate = False


def sanitized_error(error: BaseException) -> dict[str, object]:
    """Describe a failure without retaining its message or provider payload."""
    if isinstance(error, RAGProviderError):
        return {
            "category": error.category,
            "status_code": error.status_code,
        }
    category = getattr(error, "category", None)
    if category in {"timeout", "unavailable", "deadline_exhausted"}:
        return {"category": category, "status_code": None}
    return {"category": "technical_error", "status_code": None}


class TraceRecorder:
    """Keep a bounded TTL cache of sanitized graph traces in process memory."""

    def __init__(
        self,
        *,
        max_runs: int = 100,
        retention_seconds: float = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_runs <= 0:
            raise ValueError("max_runs must be positive")
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        self.max_runs = max_runs
        self.retention_seconds = retention_seconds
        self._clock = clock
        self._active: dict[str, dict[str, Any]] = {}
        self._completed: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def now(self) -> float:
        """Return the recorder clock so node durations share one time source."""
        return self._clock()

    def begin(self, run_id: str) -> None:
        with self._lock:
            self._cleanup_locked()
            self._active[run_id] = {
                "started": self._clock(),
                "events": [],
            }

    def record_node(
        self,
        run_id: str,
        *,
        node: str,
        status: str,
        started: float,
        transition: str | None,
        state: Mapping[str, object],
        error: BaseException | None = None,
        resilience: Mapping[str, object] | None = None,
    ) -> None:
        with self._lock:
            active = self._active[run_id]
            event: dict[str, object] = {
                "sequence": len(active["events"]) + 1,
                "node": node,
                "status": status,
                "duration_ms": round((self._clock() - started) * 1000, 3),
                "transition": transition,
                "state": dict(state),
            }
            if error is not None:
                event["error"] = sanitized_error(error)
            if resilience:
                event["resilience"] = dict(resilience)
            active["events"].append(event)

    def finish(
        self,
        run_id: str,
        *,
        outcome: str,
        error: BaseException | None = None,
    ) -> dict[str, object]:
        with self._lock:
            active = self._active.pop(run_id)
            finished = self._clock()
            trace: dict[str, Any] = {
                "run_id": run_id,
                "status": "failed" if error is not None else "completed",
                "outcome": outcome,
                "duration_ms": round((finished - active["started"]) * 1000, 3),
                "nodes": active["events"],
                "_expires_at": finished + self.retention_seconds,
            }
            if error is not None:
                trace["error"] = sanitized_error(error)
            self._completed[run_id] = trace
            self._completed.move_to_end(run_id)
            self._cleanup_locked()
            public = self._public(trace)
        _TRACE_LOGGER.info(
            "TrackFlow agent trace | %s",
            json.dumps(public, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        )
        return public

    def get(self, run_id: str) -> dict[str, object] | None:
        with self._lock:
            self._cleanup_locked()
            trace = self._completed.get(run_id)
            return None if trace is None else deepcopy(self._public(trace))

    def export(self, run_ids: list[str] | None = None) -> list[dict[str, object]]:
        with self._lock:
            self._cleanup_locked()
            selected = (
                self._completed.values()
                if run_ids is None
                else (self._completed[run_id] for run_id in run_ids if run_id in self._completed)
            )
            return [deepcopy(self._public(trace)) for trace in selected]

    def cleanup(self) -> None:
        with self._lock:
            self._cleanup_locked()

    @staticmethod
    def _public(trace: Mapping[str, Any]) -> dict[str, object]:
        return {key: value for key, value in trace.items() if not key.startswith("_")}

    def _cleanup_locked(self) -> None:
        now = self._clock()
        expired = [
            run_id
            for run_id, trace in self._completed.items()
            if trace["_expires_at"] <= now
        ]
        for run_id in expired:
            self._completed.pop(run_id, None)
        while len(self._completed) > self.max_runs:
            self._completed.popitem(last=False)

