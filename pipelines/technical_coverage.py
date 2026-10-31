"""Test-only technical coverage attestation for an isolated PostgreSQL fixture.

This module never certifies business or production coverage.  Its positive
path is created only after inspecting extracted fixture data on the exact
disposable database identity authorized for integration tests.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import json
from hashlib import sha256
import os
import subprocess
from typing import Any
from urllib.parse import urlparse
from weakref import WeakSet

from sqlalchemy import text

from pipelines.extraction import WeeklyExtractionResult
from pipelines.weekly_performance import (
    OFFICIAL_EVENT_TYPES,
    OUTBOUND_ORDER_CREATED,
    WeeklyTransformationResult,
)

_ATTESTATION = object()
_AUTHORIZED_CONTAINER_NAME = "trackflow-pg-f2-933cf5b"
_AUTHORIZED_CAPABILITIES: WeakSet[ControlledTestDatabaseCapability] = WeakSet()
_PROVENANCE = "isolated_postgresql_controlled_test_fixture"
_SCOPE = "technical_test_only_no_business_certification"
_ALLOWED_COVERAGE_DIAGNOSTICS = frozenset(
    {
        "Coverage is unverified: stock_threshold_triggered has no confirmed producer/capture guarantee.",
        "Coverage is unverified: inventory_discrepancy_detected has no confirmed producer/capture guarantee.",
        "An outbound order lacks verified commercial-order identity and dispatch evidence.",
    }
)


@dataclass(frozen=True, eq=False)
class ControlledTestDatabaseCapability:
    """Opaque proof that tests inspected the expected disposable Docker server."""

    engine_identity: int
    container_id: str
    system_identifier: str
    host_port: int
    _authorization: object


@dataclass(frozen=True)
class TechnicalCoverageEvidence:
    week_start: date
    dependency_fingerprint: str
    rules_version: str
    event_ids: tuple[str, ...]
    event_types: frozenset[str]
    outbound_order_client_pairs: tuple[tuple[str, str], ...]
    aggregate_keys: tuple[tuple[str, str, date], ...]
    provenance: str
    scope: str
    identity: str
    content_fingerprint: str
    server_identity: str
    _attestation: object
    _database_capability: ControlledTestDatabaseCapability

    @property
    def production_certified(self) -> bool:
        return False


def verify_controlled_test_coverage(
    engine: Any,
    extracted: WeeklyExtractionResult,
    transformed: WeeklyTransformationResult,
    *,
    week_start: date,
    dependency_fingerprint: str,
    rules_version: str,
    database_capability: ControlledTestDatabaseCapability,
) -> TechnicalCoverageEvidence:
    """Verify a complete synthetic fixture on the authorized disposable DB."""
    server_identity = _validate_database_capability(engine, database_capability)
    with engine.connect() as connection:
        identity = connection.execute(
            text("SELECT current_database() AS database_name, current_user AS user_name")
        ).mappings().one()
    if identity != {"database_name": "trackflow_test", "user_name": "trackflow_test"}:
        raise ValueError("technical coverage requires the authorized disposable PostgreSQL identity")
    if extracted.metadata.week_start != week_start or not extracted.metadata.snapshot_consistent:
        raise ValueError("technical coverage week or snapshot is invalid")
    event_ids = tuple(sorted(str(row["id"]) for row in extracted.events))
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("technical fixture event identities are not unique")
    event_types = frozenset(str(row["event_type"]) for row in extracted.events)
    if event_types != OFFICIAL_EVENT_TYPES:
        raise ValueError("technical fixture must contain exactly the four official event types")
    quality = transformed.quality
    if (
        quality.rejected_event_ids
        or quality.issues
        or quality.conflict_movement_keys
        or quality.cross_week_verification_pending
        or set(quality.accepted_event_ids) != set(event_ids)
    ):
        raise ValueError("technical fixture contains rejected, partial, or conflicting evidence")
    outbound_pairs: list[tuple[str, str]] = []
    for row in extracted.events:
        if row["event_type"] != OUTBOUND_ORDER_CREATED:
            continue
        order_id = str(row["tags"]["order_id"])
        record = extracted.commercial_order_evidence.records.get(order_id)
        if record is None or record.dispatch_count < 1 or not record.client_id:
            raise ValueError("outbound order is not backed by verified dispatch evidence")
        outbound_pairs.append((order_id, record.client_id))
    if not outbound_pairs or len(outbound_pairs) != len(set(outbound_pairs)):
        raise ValueError("outbound dispatch identities are missing or not unique")
    aggregate_keys = tuple(
        sorted((item.warehouse, item.client_id, item.week_start) for item in transformed.aggregates)
    )
    if not aggregate_keys:
        raise ValueError("technical fixture produced no aggregates")
    content_fingerprint = _content_fingerprint(extracted, transformed)
    outbound_order_client_pairs = tuple(sorted(outbound_pairs))
    identity = _evidence_identity(
        week_start=week_start,
        dependency_fingerprint=dependency_fingerprint,
        rules_version=rules_version,
        event_ids=event_ids,
        event_types=event_types,
        outbound_order_client_pairs=outbound_order_client_pairs,
        aggregate_keys=aggregate_keys,
        provenance=_PROVENANCE,
        scope=_SCOPE,
        content_fingerprint=content_fingerprint,
        server_identity=server_identity,
    )
    return TechnicalCoverageEvidence(
        week_start=week_start,
        dependency_fingerprint=dependency_fingerprint,
        rules_version=rules_version,
        event_ids=event_ids,
        event_types=event_types,
        outbound_order_client_pairs=outbound_order_client_pairs,
        aggregate_keys=aggregate_keys,
        provenance=_PROVENANCE,
        scope=_SCOPE,
        identity=identity,
        content_fingerprint=content_fingerprint,
        server_identity=server_identity,
        _attestation=_ATTESTATION,
        _database_capability=database_capability,
    )


def apply_verified_technical_coverage(
    extracted: WeeklyExtractionResult,
    transformed: WeeklyTransformationResult,
    evidence: TechnicalCoverageEvidence | None,
    *,
    week_start: date,
    dependency_fingerprint: str,
    rules_version: str,
    engine: Any | None = None,
) -> WeeklyTransformationResult:
    """Clear only unresolved coverage diagnostics after attestation validation."""
    if evidence is None:
        return transformed
    if engine is None:
        raise ValueError("technical coverage requires its authorized database engine")
    server_identity = _validate_database_capability(engine, evidence._database_capability)
    keys = tuple(sorted((a.warehouse, a.client_id, a.week_start) for a in transformed.aggregates))
    actual_content_fingerprint = _content_fingerprint(extracted, transformed)
    actual_outbound_order_client_pairs = _outbound_order_client_pairs(extracted)
    expected_identity = _evidence_identity(
        week_start=evidence.week_start,
        dependency_fingerprint=evidence.dependency_fingerprint,
        rules_version=evidence.rules_version,
        event_ids=evidence.event_ids,
        event_types=evidence.event_types,
        outbound_order_client_pairs=evidence.outbound_order_client_pairs,
        aggregate_keys=evidence.aggregate_keys,
        provenance=evidence.provenance,
        scope=evidence.scope,
        content_fingerprint=evidence.content_fingerprint,
        server_identity=evidence.server_identity,
    )
    if (
        evidence._attestation is not _ATTESTATION
        or evidence.provenance != _PROVENANCE
        or evidence.scope != _SCOPE
        or evidence.week_start != week_start
        or evidence.dependency_fingerprint != dependency_fingerprint
        or evidence.rules_version != rules_version
        or evidence.aggregate_keys != keys
        or set(evidence.event_ids) != set(transformed.quality.accepted_event_ids)
        or evidence.event_types != OFFICIAL_EVENT_TYPES
        or evidence.outbound_order_client_pairs != actual_outbound_order_client_pairs
        or evidence.content_fingerprint != actual_content_fingerprint
        or evidence.server_identity != server_identity
        or evidence.identity != expected_identity
    ):
        raise ValueError("technical coverage attestation does not match this transformation")
    return replace(
        transformed,
        quality=replace(
            transformed.quality,
            coverage_pending=tuple(
                diagnostic
                for diagnostic in transformed.quality.coverage_pending
                if diagnostic not in _ALLOWED_COVERAGE_DIAGNOSTICS
            ),
        ),
    )


def authorize_controlled_test_database(
    engine: Any,
    *,
    container_name: str,
) -> ControlledTestDatabaseCapability:
    """Issue an in-memory capability only after Docker and PostgreSQL checks."""
    if container_name != _AUTHORIZED_CONTAINER_NAME:
        raise ValueError("technical coverage container is not authorized for this execution")
    url = engine.url
    rendered_url = url.render_as_string(hide_password=False)
    parsed = urlparse(rendered_url)
    configured_test_url = os.getenv("TRACKFLOW_PUBLICATION_TEST_DATABASE_URL")
    if (
        engine.dialect.name != "postgresql"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port is None
        or not configured_test_url
        or rendered_url != configured_test_url
        or configured_test_url == os.getenv("DATABASE_URL")
    ):
        raise ValueError("technical coverage requires the dedicated loopback test URL")
    inspection = subprocess.run(
        [
            "docker", "inspect", container_name, "--format",
            "{{json .Id}}|{{json .State.Running}}|{{json .State.Health.Status}}|"
            "{{json (index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostIp}}|"
            "{{json (index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}|"
            "{{json (index .HostConfig.Tmpfs \"/var/lib/postgresql/data\")}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().split("|")
    if len(inspection) != 6:
        raise ValueError("authorized PostgreSQL container inspection is incomplete")
    container_id, running, health, host_ip, host_port, tmpfs = map(json.loads, inspection)
    if (
        running is not True
        or health != "healthy"
        or host_ip != "127.0.0.1"
        or int(host_port) != parsed.port
        or not tmpfs
    ):
        raise ValueError("technical coverage requires the healthy isolated tmpfs container")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT current_database() AS database_name, current_user AS user_name, "
                "system_identifier::text FROM pg_control_system()"
            )
        ).mappings().one()
    if row["database_name"] != "trackflow_test" or row["user_name"] != "trackflow_test":
        raise ValueError("technical coverage requires the authorized disposable PostgreSQL identity")
    capability = ControlledTestDatabaseCapability(
        engine_identity=id(engine),
        container_id=container_id,
        system_identifier=row["system_identifier"],
        host_port=parsed.port,
        _authorization=_ATTESTATION,
    )
    _AUTHORIZED_CAPABILITIES.add(capability)
    return capability


def _validate_database_capability(engine: Any, capability: ControlledTestDatabaseCapability) -> str:
    if (
        capability._authorization is not _ATTESTATION
        or capability not in _AUTHORIZED_CAPABILITIES
        or capability.engine_identity != id(engine)
    ):
        raise ValueError("technical coverage database capability is invalid")
    with engine.connect() as connection:
        system_identifier = connection.execute(
            text("SELECT system_identifier::text FROM pg_control_system()")
        ).scalar_one()
    if str(system_identifier) != capability.system_identifier:
        raise ValueError("technical coverage database capability does not match this server")
    return f"docker:{capability.container_id};postgres:{capability.system_identifier};port:{capability.host_port}"


def _outbound_order_client_pairs(
    extracted: WeeklyExtractionResult,
) -> tuple[tuple[str, str], ...]:
    pairs = []
    for row in extracted.events:
        if row["event_type"] == OUTBOUND_ORDER_CREATED:
            order_id = str(row["tags"]["order_id"])
            record = extracted.commercial_order_evidence.records.get(order_id)
            if record is None or record.dispatch_count < 1 or not record.client_id:
                raise ValueError("outbound order is not backed by verified dispatch evidence")
            pairs.append((order_id, record.client_id))
    return tuple(sorted(pairs))


def _evidence_identity(**material: Any) -> str:
    """Deterministic integrity digest; this is not a cryptographic signature."""
    normalized = tuple((key, material[key]) for key in sorted(material))
    return sha256(repr(normalized).encode("utf-8")).hexdigest()


def _content_fingerprint(
    extracted: WeeklyExtractionResult,
    transformed: WeeklyTransformationResult,
) -> str:
    """Canonical digest of the exact extracted evidence and transform result."""
    events = sorted(
        (
            str(row["id"]),
            row["timestamp"].isoformat(),
            str(row["service"]),
            str(row["event_type"]),
            json.dumps(row["tags"], sort_keys=True, separators=(",", ":"), default=str),
        )
        for row in extracted.events
    )
    commercial_orders = sorted(
        (
            key,
            value.warehouse,
            value.client_id,
            value.dispatch_count,
        )
        for key, value in extracted.commercial_order_evidence.records.items()
    )
    movements = sorted(
        (key, sorted(item.week_starts), item.complete)
        for key, item in extracted.movement_week_evidence.items()
    )
    aggregates = sorted(
        (
            item.warehouse,
            item.client_id,
            item.week_start.isoformat(),
            item.inbound_units_count,
            item.outbound_orders_count,
            item.stockout_events_count,
            item.discrepancy_events_count,
            str(item.discrepancy_rate),
        )
        for item in transformed.aggregates
    )
    quality = transformed.quality
    quality_material = (
        quality.accepted_event_ids,
        quality.rejected_event_ids,
        quality.conflict_movement_keys,
        quality.cross_week_verification_pending,
        tuple((issue.code, issue.message, issue.event_ids, issue.movement_key) for issue in quality.issues),
        quality.coverage_pending,
    )
    return sha256(
        repr((events, commercial_orders, movements, aggregates, quality_material)).encode("utf-8")
    ).hexdigest()
