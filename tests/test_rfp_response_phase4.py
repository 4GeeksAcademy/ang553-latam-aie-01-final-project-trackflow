from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from services.rfp_intake import relay
from services.rfp_response.tasks import process_rfp_response
from services.task_queue.celery_app import celery_app


ROOT = Path(__file__).parents[1]


def test_response_celery_task_is_registered_with_closed_name() -> None:
    assert process_rfp_response.name == "trackflow.process_rfp_response"
    assert celery_app.tasks["trackflow.process_rfp_response"].name == process_rfp_response.name


@pytest.mark.parametrize(("job_kind", "expected_task"), [
    ("intake", "trackflow.process_rfp"),
    ("response_generation", "trackflow.process_rfp_response"),
])
def test_shared_relay_routes_only_by_job_kind(monkeypatch, job_kind, expected_task) -> None:
    observed = {}
    monkeypatch.setattr(relay.celery_app, "send_task",
        lambda *args, **kwargs: observed.update(args=args, kwargs=kwargs))
    outbox_id, job_id = uuid4(), uuid4()
    relay._publish({"outbox_id": outbox_id, "job_id": job_id,
                    "job_kind": job_kind})
    assert observed["args"] == (expected_task,)
    assert observed["kwargs"] == {
        "task_id": str(outbox_id), "kwargs": {"job_id": str(job_id)}}


def test_shared_relay_rejects_unknown_job_kind_before_broker(monkeypatch) -> None:
    monkeypatch.setattr(relay.celery_app, "send_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("broker must not receive an unknown route")))
    with pytest.raises(ValueError, match="unsupported RFP job kind"):
        relay._publish({"outbox_id": uuid4(), "job_id": uuid4(),
                        "job_kind": "unknown"})


def test_api_and_worker_images_include_response_runtime_and_official_sources() -> None:
    api = (ROOT / "services" / "Dockerfile").read_text(encoding="utf-8")
    worker = (ROOT / "services" / "task_queue" / "Dockerfile").read_text(encoding="utf-8")
    expected_pipeline = (
        "COPY data/pipelines/rfp_response/ /app/data/pipelines/rfp_response/"
    )
    assert expected_pipeline in api
    assert expected_pipeline in worker
    assert "COPY docs/company-knowledge-base/ /app/docs/company-knowledge-base/" in worker
