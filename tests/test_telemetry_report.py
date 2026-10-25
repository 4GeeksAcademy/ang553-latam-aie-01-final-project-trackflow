"""HTTP contracts, date validation and deterministic report cache tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.cache import TTLCache
from services.api.database import get_db
from services.api.routes import telemetry


NOW = datetime(2026, 10, 25, 4, 30, 0, 123456, tzinfo=timezone.utc)
START = datetime(2026, 10, 24, 7, 0, 0, 123456, tzinfo=timezone.utc)
END = datetime(2026, 10, 25, 7, 0, 0, 654321, tzinfo=timezone.utc)
PARAMS = {"start_date": START.isoformat(), "end_date": END.isoformat()}
METRICS = {
    "event_volume_by_day_and_type",
    "operational_failures_by_type_and_day",
    "stock_insufficiency_by_day_warehouse_category",
    "auth_failure_rate",
}


def make_report(session, start_date, end_date):
    return {
        "period": {"from": start_date.isoformat(), "to": end_date.isoformat()},
        "metrics": {
            "event_volume_by_day_and_type": [
                {"day": "2026-10-24", "event_type": "auth_login_failed", "count": 2}
            ],
            "operational_failures_by_type_and_day": [
                {"day": "2026-10-24", "event_type": "auth_login_failed", "count": 2}
            ],
            "stock_insufficiency_by_day_warehouse_category": [],
            "auth_failure_rate": [],
        },
    }


@pytest.fixture
def report_env(monkeypatch):
    clock = Mock(return_value=0.0)
    cache = TTLCache[dict](ttl_seconds=60, clock=clock)
    build = Mock(side_effect=make_report)
    dates = Mock(return_value=NOW)
    session = Mock()
    monkeypatch.setattr(telemetry, "report_cache", cache)
    monkeypatch.setattr(telemetry, "build_telemetry_report", build)
    monkeypatch.setattr(telemetry, "_utc_now", dates)
    app = FastAPI()
    app.include_router(telemetry.router)
    assert get_db is telemetry.get_db
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, session, build, dates, clock, cache


def test_production_cache_ttl():
    assert telemetry.report_cache.ttl_seconds == 60


def test_default_window_resolves_now_once_and_caches_complete_report(report_env):
    client, session, build, dates, _, cache = report_env

    response = client.get("/telemetry/report")

    assert response.status_code == 200
    dates.assert_called_once_with()
    build.assert_called_once_with(session, NOW - timedelta(days=7), NOW)
    body = response.json()
    assert body["period"] == {
        "from": (NOW - timedelta(days=7)).isoformat(),
        "to": NOW.isoformat(),
    }
    assert (
        datetime.fromisoformat(body["period"]["to"])
        - datetime.fromisoformat(body["period"]["from"])
    ) == timedelta(days=7)
    assert cache.get("telemetry_report:v1:default") == body
    assert body == make_report(*build.call_args.args)
    assert set(body) == {"period", "metrics"}
    assert set(body["period"]) == {"from", "to"}
    assert set(body["metrics"]) == METRICS


def test_explicit_window_normalizes_utc_without_losing_microseconds(report_env):
    client, session, build, dates, _, cache = report_env
    response = client.get(
        "/telemetry/report",
        params={
            "start_date": "2026-10-24T00:00:00.123456-07:00",
            "end_date": "2026-10-25T09:00:00.654321+02:00",
        },
    )

    assert response.status_code == 200
    build.assert_called_once_with(session, START, END)
    assert all(date.tzinfo is timezone.utc for date in build.call_args.args[1:])
    assert response.json()["period"] == {"from": START.isoformat(), "to": END.isoformat()}
    assert cache.get(
        f"telemetry_report:v1:{START.isoformat()}:{END.isoformat()}"
    ) == response.json()
    dates.assert_not_called()


def test_equivalent_offsets_hit_the_same_cache_entry(report_env):
    client, _, build, _, _, cache = report_env
    first = client.get(
        "/telemetry/report",
        params={
            "start_date": "2026-10-24T00:00:00.123456-07:00",
            "end_date": "2026-10-25T09:00:00.654321+02:00",
        },
    )
    second = client.get("/telemetry/report", params=PARAMS)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    build.assert_called_once()
    assert len(cache) == 1


@pytest.mark.parametrize("params", [None, PARAMS])
def test_cache_hit_preserves_response_and_does_not_recompute(report_env, params):
    client, _, build, dates, clock, _ = report_env
    first = client.get("/telemetry/report", params=params)
    clock.return_value = 59.999
    dates.return_value = NOW + timedelta(seconds=59)
    second = client.get("/telemetry/report", params=params)

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    build.assert_called_once()
    assert dates.call_count == (1 if params is None else 0)


@pytest.mark.parametrize("params", [None, PARAMS])
def test_cache_expires_at_exactly_sixty_seconds(report_env, params):
    client, _, build, dates, clock, _ = report_env
    first = client.get("/telemetry/report", params=params)
    clock.return_value = 60.0
    dates.return_value = NOW + timedelta(seconds=60)
    second = client.get("/telemetry/report", params=params)

    assert first.status_code == second.status_code == 200
    assert build.call_count == 2
    if params is None:
        assert dates.call_count == 2
        assert second.json()["period"]["to"] == (NOW + timedelta(seconds=60)).isoformat()
    else:
        assert second.json() == first.json()
        dates.assert_not_called()


def test_different_periods_have_independent_cache_entries(report_env):
    client, _, build, _, _, cache = report_env
    first = client.get("/telemetry/report", params=PARAMS)
    second = client.get(
        "/telemetry/report",
        params={**PARAMS, "end_date": (END + timedelta(microseconds=1)).isoformat()},
    )
    default = client.get("/telemetry/report")

    assert first.status_code == second.status_code == default.status_code == 200
    assert first.json()["period"] != second.json()["period"]
    assert build.call_count == 3
    assert len(cache) == 3


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"start_date": START.isoformat()}, "start_date and end_date must be provided together"),
        ({"end_date": END.isoformat()}, "start_date and end_date must be provided together"),
        (
            {"start_date": "2026-10-24T07:00:00", "end_date": END.isoformat()},
            "start_date and end_date must be timezone-aware",
        ),
        (
            {"start_date": START.isoformat(), "end_date": "2026-10-25T07:00:00"},
            "start_date and end_date must be timezone-aware",
        ),
        (
            {"start_date": "2026-10-24T07:00:00", "end_date": "2026-10-25T07:00:00"},
            "start_date and end_date must be timezone-aware",
        ),
        (
            {"start_date": START.isoformat(), "end_date": START.isoformat()},
            "start_date must be earlier than end_date",
        ),
        (
            {"start_date": END.isoformat(), "end_date": START.isoformat()},
            "start_date must be earlier than end_date",
        ),
        (
            {"start_date": "2026-10-24T00:00:00.123456-07:00", "end_date": START.isoformat()},
            "start_date must be earlier than end_date",
        ),
    ],
)
def test_invalid_period_returns_stable_422_without_building(report_env, params, detail):
    client, _, build, dates, _, cache = report_env
    response = client.get("/telemetry/report", params=params)

    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    build.assert_not_called()
    dates.assert_not_called()
    assert len(cache) == 0


def test_malformed_date_is_rejected_by_fastapi(report_env):
    client, _, build, _, _, cache = report_env
    response = client.get(
        "/telemetry/report", params={**PARAMS, "start_date": "not-a-date"}
    )

    assert response.status_code == 422
    build.assert_not_called()
    assert len(cache) == 0


def test_failed_build_is_not_cached_and_next_request_can_succeed(report_env):
    client, _, build, _, _, cache = report_env
    build.side_effect = RuntimeError("report failed")

    response = client.get("/telemetry/report", params=PARAMS)

    assert response.status_code == 500
    assert len(cache) == 0
    build.side_effect = make_report
    assert client.get("/telemetry/report", params=PARAMS).status_code == 200
    assert build.call_count == 2
    assert len(cache) == 1
