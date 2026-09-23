"""Unit tests for telemetry storage mapping and bulk persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from services.api.telemetry_schemas import TelemetryEvent
from services.api.telemetry_storage import (
    EVENT_PROPERTY_ALLOWLISTS,
    bulk_insert_telemetry,
    filter_tags,
    map_event_to_row,
    persist_backend_telemetry,
)


SCHEMA_PATH = Path(__file__).parents[1] / "docs/telemetry/event-schemas.json"


def make_event(event_type: str = "backoffice_section_entered", properties=None) -> TelemetryEvent:
    return TelemetryEvent(
        eventId=uuid4(),
        timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        sessionId=None,
        userId=None,
        event_type=event_type,
        schemaVersion="1.0",
        requestId=None,
        properties=(
            {"section": "inventory_products"}
            if properties is None
            else properties
        ),
    )


def test_filter_tags_keeps_allowlisted_properties():
    properties = {"section": "inventory_products"}
    assert filter_tags("backoffice_section_entered", properties) == properties


def test_filter_tags_removes_unknown_property():
    assert filter_tags("backoffice_section_entered", {"section": "suppliers", "extra": 1}) == {
        "section": "suppliers"
    }


def test_filter_tags_handles_mixed_properties_without_mutating_input():
    properties = {"section": "incidents", "unknown": {"nested": True}}
    original = properties.copy()
    assert filter_tags("backoffice_section_entered", properties) == {"section": "incidents"}
    assert properties == original


def test_filter_tags_handles_empty_and_unknown_event_types():
    assert filter_tags("auth_password_reset_completed", {"extra": 1}) == {}
    assert filter_tags("not_cataloged", {"extra": 1}) == {}


def test_filter_tags_handles_explicit_empty_properties():
    assert filter_tags("backoffice_section_entered", {}) == {}


def test_map_event_to_row_has_exact_closed_mapping():
    event = make_event(properties={"section": "inventory_orders", "eventId": "must-not-be-tagged"})
    row = map_event_to_row(event)

    assert row == {
        "timestamp": event.timestamp,
        "service": "backoffice",
        "event_type": "backoffice_section_entered",
        "level": "info",
        "value": None,
        "message": None,
        "tags": {"section": "inventory_orders"},
    }
    assert "id" not in row
    assert not {"eventId", "sessionId", "userId", "schemaVersion", "requestId"} & row["tags"].keys()


def test_map_event_to_row_accepts_api_service():
    assert map_event_to_row(make_event(), service="api")["service"] == "api"


def test_persist_backend_telemetry_maps_all_events_and_bulk_inserts_once(monkeypatch):
    events = [make_event(), make_event(properties={"section": "suppliers"})]
    session = Mock()
    bulk_insert = Mock()
    monkeypatch.setattr(
        "services.api.telemetry_storage.bulk_insert_telemetry", bulk_insert
    )

    persist_backend_telemetry(session, events)

    bulk_insert.assert_called_once_with(
        session,
        [map_event_to_row(event, service="api") for event in events],
    )


def test_bulk_insert_empty_rows_does_nothing():
    session = Mock()
    bulk_insert_telemetry(session, [])
    session.execute.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


def test_bulk_insert_executes_one_multi_row_statement_and_commits_once():
    session = Mock()
    rows = [
        map_event_to_row(make_event()),
        map_event_to_row(make_event(properties={"section": "suppliers"})),
    ]

    bulk_insert_telemetry(session, rows)

    session.execute.assert_called_once()
    session.commit.assert_called_once()
    session.rollback.assert_not_called()
    statement = session.execute.call_args.args[0]
    assert statement.is_insert
    compiled = statement.compile(dialect=postgresql.dialect())
    assert compiled.string.startswith("INSERT INTO telemetry_events")
    assert compiled.string.count("), (") == 1


def test_bulk_insert_rolls_back_and_propagates_execute_error():
    session = Mock()
    error = RuntimeError("execute failed")
    session.execute.side_effect = error

    with pytest.raises(RuntimeError, match="execute failed"):
        bulk_insert_telemetry(session, [{"event_type": "x"}])

    session.rollback.assert_called_once()
    session.commit.assert_not_called()


def test_bulk_insert_rolls_back_and_propagates_commit_error():
    session = Mock()
    error = RuntimeError("commit failed")
    session.commit.side_effect = error

    with pytest.raises(RuntimeError, match="commit failed"):
        bulk_insert_telemetry(session, [{"event_type": "x"}])

    session.execute.assert_called_once()
    session.commit.assert_called_once()
    session.rollback.assert_called_once()


def test_allowlists_match_event_schema_without_drift():
    schema = json.loads(SCHEMA_PATH.read_text())
    schema_allowlists = {}

    for reference in schema["oneOf"]:
        event_name = reference["$ref"].rsplit("/", 1)[1]
        event_definition = schema["definitions"][event_name]
        properties = event_definition["properties"]["properties"]["properties"]
        schema_allowlists[event_name] = frozenset(properties)

    assert schema_allowlists == EVENT_PROPERTY_ALLOWLISTS
    assert len(schema_allowlists) == 17
    assert schema_allowlists["auth_password_reset_completed"] == frozenset()
