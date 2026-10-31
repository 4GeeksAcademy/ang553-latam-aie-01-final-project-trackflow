"""Coordinator contract tests.

The fake keeps uncommitted writes in a transaction overlay. It does not prove
PostgreSQL atomicity, row locks, or cross-connection exclusion.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from pipelines.concurrency import acquire_week_lease, release_week_lease
from pipelines.coordinator import (
    SYNTHETIC_COVERAGE_SOURCE,
    ConfirmedLeaseRight,
    CoverageEvidence,
    publish_weekly_business_report,
    resolve_uncertain_publication,
)
from pipelines.publication import official_discrepancy_rate
from pipelines.run_store import (
    STATUS_BLOCKED_QUALITY,
    STATUS_FAILED_RETRYABLE,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED_IN_PROGRESS,
    STATUS_SUCCEEDED,
    STATUS_SUCCEEDED_NOOP,
    STATUS_SUPERSEDED,
    create_run,
    latest_run,
    latest_valid_publication,
    record_fencing_advance,
)
from pipelines.weekly_performance import QualityIssue, QualityReport, WeeklyAggregate

WEEK = date(2026, 10, 19)
STARTED = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc)
CLIENT_A = "7f6e3d21-8c4a-4b92-a1d7-5e9f2c6b1048"
CLIENT_B = "8a7f4e32-9d5b-4c03-b2e8-6f0a3d7c2159"
FINGERPRINT = "telemetry=a1 movements=b2 stockexit=c3 rules=weekly-v1"
RULES = "weekly-v1"
_MISSING = object()


class MappingResult:
    def __init__(self, rows):
        self._rows = [dict(row) for row in rows]

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class Store:
    def __init__(self):
        self.runs = {}
        self.controls = {}
        self.kpis = {}
        self.next_id = 1


class Connection:
    def __init__(self, store, *, autocommit=False):
        self.store = store
        self.autocommit = autocommit
        self.dialect = SimpleNamespace(name="postgresql")
        self.open = True
        self.db_now = NOW
        self.runs = {}
        self.controls = {}
        self.kpis = {}
        self.events = []
        self.commits = 0
        self.rollbacks = 0
        self.locks = set()
        self.fail_upsert = None
        self.fail_commit = None

    def in_transaction(self):
        return self.open

    def get_execution_options(self):
        if self.autocommit:
            return {"isolation_level": "AUTOCOMMIT"}
        return {}

    def commit(self):
        self.events.append("COMMIT")
        if self.fail_commit is not None:
            raise self.fail_commit
        self.store.runs.update(self.runs)
        self.store.controls.update(self.controls)
        self.store.kpis.update(self.kpis)
        self.runs = {}
        self.controls = {}
        self.kpis = {}
        self.locks.clear()
        self.commits += 1

    def rollback(self):
        self.events.append("ROLLBACK")
        self.runs = {}
        self.controls = {}
        self.kpis = {}
        self.locks.clear()
        self.rollbacks += 1

    def execute(self, statement, parameters=None):
        sql = str(statement)
        params = dict(parameters or {})
        self.events.append(sql)
        if ":" not in sql:
            raise AssertionError("statement is not parameterized")
        if "telemetry_events" in sql.lower():
            raise AssertionError("telemetry write is forbidden")
        if "FOR UPDATE" in sql.upper():
            self.locks.add(params.get("week_start"))
        if "op:" in sql:
            op = sql.split("op:", 1)[1].split("*/", 1)[0].strip()
            return MappingResult(self._apply(op, params))
        if "INSERT INTO reporting.weekly_warehouse_client_performance" in sql:
            return MappingResult(self._upsert(params))
        if "FROM reporting.weekly_warehouse_client_performance" in sql:
            return MappingResult(self._published(params))
        raise AssertionError(f"unexpected statement {sql}")

    def _apply(self, op, params):
        if op == "create_run":
            return [self._create_run(params)]
        if op == "select_run":
            row = self._read_run(params["run_id"])
            return [] if row is None else [row]
        if op == "update_run":
            return self._update_run(params)
        if op == "record_commit_observation":
            return self._observe(params)
        if op == "latest_run":
            return self._latest_run(params)
        if op == "ensure_control":
            if self._read_control(params["week_start"]) is None:
                self.controls[params["week_start"]] = self._new_control(params["week_start"])
            return []
        if op == "select_control":
            row = self._read_control(params["week_start"])
            return [] if row is None else [row]
        if op == "record_valid_publication":
            return self._record_valid(params)
        if op == "latest_valid_publication":
            return self._latest_publication(params)
        if op == "lock_control":
            return [self._lock(params)]
        if op == "grant_lease":
            return self._grant(params)
        if op == "verify_lease":
            return self._verify(params)
        if op == "release_lease":
            return self._release(params)
        raise AssertionError(f"unexpected operation {op}")

    def _read_run(self, run_id):
        return self.runs.get(run_id, self.store.runs.get(run_id))

    def _edit_run(self, run_id):
        if run_id not in self.runs and run_id in self.store.runs:
            self.runs[run_id] = dict(self.store.runs[run_id])
        return self.runs.get(run_id)

    def _read_control(self, week):
        return self.controls.get(week, self.store.controls.get(week))

    def _edit_control(self, week):
        if week not in self.controls and week in self.store.controls:
            self.controls[week] = dict(self.store.controls[week])
        return self.controls.get(week)

    def _create_run(self, params):
        if params["run_id"] in self.store.runs or params["run_id"] in self.runs:
            raise IntegrityError("INSERT", params, Exception("duplicate key"))
        row = {
            "run_id": params["run_id"],
            "pipeline_name": params["pipeline_name"],
            "target_week_start": params["target_week_start"],
            "trigger_type": params["trigger_type"],
            "started_at": params["started_at"] or STARTED,
            "finished_at": None,
            "status": STATUS_PENDING,
            "stage_reached": params["stage_reached"],
            "records_processed": None,
            "aggregates_calculated": None,
            "rows_inserted": None,
            "rows_updated": None,
            "rows_unchanged": None,
            "error_code": None,
            "error_message": None,
            "block_reason": None,
            "commit_result": "not_attempted",
            "prefect_flow_run_id": params["prefect_flow_run_id"],
            "prefect_task_run_id": params["prefect_task_run_id"],
        }
        self.runs[row["run_id"]] = row
        return row

    def _update_run(self, params):
        row = self._edit_run(params["run_id"])
        if row is None or row["status"] != params["expected_status"]:
            return []
        row.update(
            {
                "status": params["status"],
                "stage_reached": params["stage_reached"],
                "finished_at": params["finished_at"],
                "records_processed": params["records_processed"],
                "aggregates_calculated": params["aggregates_calculated"],
                "rows_inserted": params["rows_inserted"],
                "rows_updated": params["rows_updated"],
                "rows_unchanged": params["rows_unchanged"],
                "error_code": params["error_code"],
                "error_message": params["error_message"],
                "block_reason": params["block_reason"],
                "commit_result": params["commit_result"],
                "prefect_flow_run_id": params["prefect_flow_run_id"],
                "prefect_task_run_id": params["prefect_task_run_id"],
            }
        )
        return [row]

    def _observe(self, params):
        row = self._edit_run(params["run_id"])
        if row is None or row["status"] != params["expected_status"]:
            return []
        row.update(
            {
                "commit_result": params["commit_result"],
                "stage_reached": params["stage_reached"],
                "error_code": params["error_code"],
                "error_message": params["error_message"],
            }
        )
        return [row]

    def _latest_run(self, params):
        rows = list(self.store.runs.values())
        rows.extend(row for identity, row in self.runs.items() if identity not in self.store.runs)
        chosen = [
            row
            for row in rows
            if row["pipeline_name"] == params["pipeline_name"]
            and (
                params["target_week_start"] is None
                or row["target_week_start"] == params["target_week_start"]
            )
        ]
        chosen.sort(key=lambda row: (row["started_at"], str(row["run_id"])), reverse=True)
        return chosen[:1]

    def _new_control(self, week):
        return {
            "week_start": week,
            "owner_run_id": None,
            "lease_acquired_at": None,
            "lease_expires_at": None,
            "fencing_token": 0,
            "last_valid_publication_run_id": None,
            "last_valid_published_at": None,
            "dependency_fingerprint": None,
            "published_row_count": None,
            "rules_version": None,
        }

    def _record_valid(self, params):
        row = self._edit_control(params["week_start"])
        if row is None:
            return []
        row.update(
            {
                "last_valid_publication_run_id": params["run_id"],
                "last_valid_published_at": params["published_at"],
                "published_row_count": params["published_row_count"],
                "dependency_fingerprint": params["dependency_fingerprint"],
                "rules_version": params["rules_version"],
            }
        )
        return [row]

    def _latest_publication(self, params):
        row = self._read_control(params["week_start"])
        if row is None or row["last_valid_publication_run_id"] is None:
            return []
        return [
            {
                "week_start": row["week_start"],
                "run_id": row["last_valid_publication_run_id"],
                "published_at": row["last_valid_published_at"],
                "published_row_count": row["published_row_count"],
                "dependency_fingerprint": row["dependency_fingerprint"],
                "rules_version": row["rules_version"],
                "fencing_token": row["fencing_token"],
            }
        ]

    def _lock(self, params):
        row = self._read_control(params["week_start"])
        expires = row["lease_expires_at"]
        return {
            **row,
            "is_free": row["owner_run_id"] is None,
            "is_owner": row["owner_run_id"] == params["run_id"],
            "is_expired": expires is not None and expires <= self.db_now,
        }

    def _grant(self, params):
        row = self._edit_control(params["week_start"])
        expired = row["lease_expires_at"] is not None and row["lease_expires_at"] <= self.db_now
        if row["fencing_token"] != params["expected_fencing_token"]:
            return []
        if row["owner_run_id"] is not None and not expired:
            return []
        row.update(
            {
                "owner_run_id": params["run_id"],
                "lease_acquired_at": self.db_now,
                "lease_expires_at": self.db_now + timedelta(seconds=params["lease_seconds"]),
                "fencing_token": row["fencing_token"] + 1,
            }
        )
        return [row]

    def _verify(self, params):
        row = self._read_control(params["week_start"])
        if row is None:
            return []
        expires = row["lease_expires_at"]
        return [
            {
                **row,
                "is_owner": row["owner_run_id"] == params["run_id"],
                "token_matches": row["fencing_token"] == params["fencing_token"],
                "unexpired": expires is not None and expires > self.db_now,
            }
        ]

    def _release(self, params):
        row = self._edit_control(params["week_start"])
        if (
            row is None
            or row["owner_run_id"] != params["run_id"]
            or row["fencing_token"] != params["fencing_token"]
        ):
            return []
        token = row["fencing_token"]
        row["owner_run_id"] = None
        row["lease_acquired_at"] = None
        row["lease_expires_at"] = None
        return [{"week_start": row["week_start"], "fencing_token": token}]

    def _published(self, params):
        rows = []
        for key, row in {**self.store.kpis, **self.kpis}.items():
            if key[2] == params["week_start"]:
                rows.append(row)
        return rows

    def _upsert(self, params):
        if self.fail_upsert is not None:
            raise self.fail_upsert
        returned = []
        index = 0
        while f"warehouse_{index}" in params:
            key = (
                params[f"warehouse_{index}"],
                params[f"client_id_{index}"],
                params[f"week_start_{index}"],
            )
            proposed = {
                "warehouse": key[0],
                "client_id": key[1],
                "week_start": key[2],
                "inbound_units_count": params[f"inbound_units_count_{index}"],
                "outbound_orders_count": params[f"outbound_orders_count_{index}"],
                "stockout_events_count": params[f"stockout_events_count_{index}"],
                "discrepancy_events_count": params[f"discrepancy_events_count_{index}"],
                "discrepancy_rate": params[f"discrepancy_rate_{index}"],
            }
            current = self.kpis.get(key, self.store.kpis.get(key))
            if current is None:
                row = {"id": self.store.next_id, "computed_at": self.db_now, **proposed}
                self.store.next_id += 1
                self.kpis[key] = row
                returned.append(row)
            else:
                same = all(current[name] == proposed[name] for name in proposed)
                if same:
                    index += 1
                    continue
                row = dict(current)
                row.update(proposed)
                row["computed_at"] = self.db_now
                self.kpis[key] = row
                returned.append(row)
            index += 1
        return returned


def _quality(**overrides):
    values = {
        "accepted_event_ids": (),
        "rejected_event_ids": (),
        "duplicate_event_ids_consolidated": (),
        "duplicate_movement_keys_consolidated": (),
        "duplicate_movement_event_ids_consolidated": (),
        "conflict_movement_keys": (),
        "issues": (),
        "cross_week_verification_pending": (),
        "coverage_pending": (),
    }
    values.update(overrides)
    return QualityReport(**values)


def _evidence(**overrides):
    values = {
        "confirmed_event_types": frozenset(
            {"stock_threshold_triggered", "inventory_discrepancy_detected"}
        ),
        "dispatch_order_ids": (("dispatch-1", "order-1"),),
        "lookup_complete": True,
        "source": SYNTHETIC_COVERAGE_SOURCE,
    }
    values.update(overrides)
    return CoverageEvidence(**values)


def _aggregate(client=CLIENT_A, inbound=3, outbound=2, stockouts=1, discrepancies=1):
    return WeeklyAggregate(
        "los_angeles",
        client,
        WEEK,
        inbound,
        outbound,
        stockouts,
        discrepancies,
        official_discrepancy_rate(outbound, discrepancies),
    )


def _pair(store=None, started_at=STARTED):
    store = Store() if store is None else store
    operational = Connection(store)
    business = Connection(store)
    run = create_run(
        operational,
        target_week_start=WEEK,
        trigger_type="manual",
        started_at=started_at,
    )
    granted = acquire_week_lease(operational, WEEK, run.run_id, lease_seconds=900)
    operational.commit()
    return store, Connection(store), business, run, granted


def _publish(
    store,
    operational,
    business,
    run,
    token,
    aggregates=(_aggregate(),),
    quality=_MISSING,
    coverage=_MISSING,
    fingerprint=FINGERPRINT,
    rules=RULES,
    committed=True,
):
    return publish_weekly_business_report(
        week_start=WEEK,
        run_id=run.run_id,
        aggregates=aggregates,
        quality=_quality() if quality is _MISSING else quality,
        coverage=_evidence() if coverage is _MISSING else coverage,
        dependency_fingerprint=fingerprint,
        rules_version=rules,
        lease=ConfirmedLeaseRight(WEEK, run.run_id, token, committed),
        business=business,
        operational=operational,
        now=NOW,
    )


def _kinds(connection):
    kinds = []
    for event in connection.events:
        if event == "COMMIT":
            kinds.append("commit")
        elif "op:verify_lease" in event:
            kinds.append("verify")
        elif "FROM reporting.weekly_warehouse_client_performance" in event:
            kinds.append("reconcile")
        elif "INSERT INTO reporting.weekly_warehouse_client_performance" in event:
            kinds.append("upsert")
        elif "op:record_valid_publication" in event:
            kinds.append("metadata")
        elif "op:release_lease" in event:
            kinds.append("release")
    return kinds


def test_confirmed_publication_is_one_transaction_and_not_production_certified():
    store, operational, business, run, granted = _pair()
    result = _publish(
        store,
        operational,
        business,
        run,
        granted.fencing_token,
        aggregates=(_aggregate(), _aggregate(CLIENT_B, inbound=4)),
    )
    assert result.status == STATUS_SUCCEEDED
    assert result.publication_recorded is True
    assert result.business_publication_valid is True
    assert result.production_certified is False
    assert result.inserted == 2
    assert _kinds(business) == ["verify", "reconcile", "upsert", "metadata", "release", "commit"]
    assert _kinds(business).count("commit") == 1
    assert business.commits == 1
    publication = latest_valid_publication(Connection(store), WEEK)
    assert publication.run_id == run.run_id
    assert publication.dependency_fingerprint == FINGERPRINT
    assert latest_run(Connection(store), target_week_start=WEEK).status == STATUS_SUCCEEDED


def test_operational_checkpoint_coverage_can_publish_without_production_certification():
    store, operational, business, run, granted = _pair()
    coverage = _evidence(
        source="operational_checkpoints",
        dispatch_order_ids=(
            ("dispatch-a", "order-1"),
            ("dispatch-b", "order-1"),
        ),
    )
    result = _publish(
        store,
        operational,
        business,
        run,
        granted.fencing_token,
        coverage=coverage,
    )
    assert result.status == STATUS_SUCCEEDED
    assert result.coverage_source == "operational_checkpoints"
    assert result.production_certified is False


def test_identical_republish_does_not_change_id_or_computed_at():
    store, operational, business, run, granted = _pair()
    _publish(store, operational, business, run, granted.fencing_token)
    original = store.kpis[("los_angeles", CLIENT_A, WEEK)]
    second_store, second_operational, second_business, second, second_grant = _pair(
        store,
        STARTED + timedelta(minutes=5),
    )
    result = _publish(
        second_store,
        second_operational,
        second_business,
        second,
        second_grant.fencing_token,
    )
    current = store.kpis[("los_angeles", CLIENT_A, WEEK)]
    assert result.status == STATUS_SUCCEEDED_NOOP
    assert result.unchanged == 1
    assert result.updated == 0
    assert current["id"] == original["id"]
    assert current["computed_at"] == original["computed_at"]


def test_changed_kpis_update_values_and_computed_at_without_changing_id():
    store, operational, business, run, granted = _pair()
    _publish(store, operational, business, run, granted.fencing_token)
    original = dict(store.kpis[("los_angeles", CLIENT_A, WEEK)])
    _store, operational, business, run, granted = _pair(store, STARTED + timedelta(minutes=5))
    business.db_now = NOW + timedelta(minutes=5)
    result = _publish(
        store,
        operational,
        business,
        run,
        granted.fencing_token,
        aggregates=(_aggregate(inbound=9),),
    )
    current = store.kpis[("los_angeles", CLIENT_A, WEEK)]
    assert result.status == STATUS_SUCCEEDED
    assert result.updated == 1
    assert current["id"] == original["id"]
    assert current["inbound_units_count"] == 9
    assert current["computed_at"] != original["computed_at"]


@pytest.mark.parametrize(
    ("quality", "coverage", "expected"),
    [
        (_quality(coverage_pending=("stock_threshold_triggered",)), _MISSING, "coverage_insufficient"),
        (
            _quality(
                issues=(
                    QualityIssue("aggregate_integer_overflow", "counter exceeds integer"),
                )
            ),
            _MISSING,
            "quality_blocked",
        ),
        (
            _quality(cross_week_verification_pending=(("dispatch", "movement-1"),)),
            _MISSING,
            "classification_incomplete",
        ),
        (
            _quality(
                issues=(QualityIssue("stockexit_movement_missing", "auxiliary lookup incomplete"),)
            ),
            _MISSING,
            "quality_blocked",
        ),
        (_quality(conflict_movement_keys=(("dispatch", "movement-1"),)), _MISSING, "identity_conflict"),
        (_MISSING, _evidence(source="production"), "coverage_insufficient"),
        (_MISSING, None, "coverage_evidence_required"),
    ],
)
def test_quality_and_coverage_blocks_do_not_write(quality, coverage, expected):
    store, operational, business, run, granted = _pair()
    result = _publish(
        store,
        operational,
        business,
        run,
        granted.fencing_token,
        quality=quality,
        coverage=coverage,
    )
    assert result.status == STATUS_BLOCKED_QUALITY
    assert expected in result.errors
    assert result.publication_recorded is False
    assert result.production_certified is False
    assert business.events == []
    assert store.kpis == {}
    assert latest_valid_publication(Connection(store), WEEK) is None
    assert latest_run(Connection(store), target_week_start=WEEK).status == STATUS_BLOCKED_QUALITY


def test_empty_batch_without_coverage_is_not_a_certified_quiet_week():
    store, operational, business, run, granted = _pair()
    result = _publish(
        store,
        operational,
        business,
        run,
        granted.fencing_token,
        aggregates=(),
        coverage=None,
    )
    assert result.status == STATUS_BLOCKED_QUALITY
    assert "empty_batch_not_certified" in result.errors
    assert "coverage_evidence_required" in result.errors
    assert business.events == []
    assert store.kpis == {}


def test_disappeared_keys_block_without_deleting_the_previous_publication():
    store, operational, business, run, granted = _pair()
    first = _publish(
        store,
        operational,
        business,
        run,
        granted.fencing_token,
        aggregates=(_aggregate(), _aggregate(CLIENT_B)),
    )
    _store, operational, business, run, granted = _pair(store, STARTED + timedelta(minutes=5))
    result = _publish(
        store,
        operational,
        business,
        run,
        granted.fencing_token,
        aggregates=(_aggregate(),),
    )
    assert first.publication_recorded is True
    assert result.status == STATUS_BLOCKED_QUALITY
    assert "reconciliation_conflict" in result.errors
    assert ("los_angeles", CLIENT_B, WEEK) in store.kpis
    assert latest_valid_publication(Connection(store), WEEK).run_id == first.run_id


def test_undurable_lease_does_not_authorize_upsert():
    store, operational, business, run, granted = _pair()
    result = _publish(
        store,
        operational,
        business,
        run,
        granted.fencing_token,
        committed=False,
    )
    assert result.status == STATUS_SUPERSEDED
    assert business.events == []
    assert store.kpis == {}


def test_missing_expired_stale_and_foreign_leases_do_not_publish():
    store = Store()
    setup = Connection(store)
    missing = create_run(setup, target_week_start=WEEK, trigger_type="manual", started_at=STARTED)
    setup.commit()
    missing_business = Connection(store)
    missing_result = _publish(store, Connection(store), missing_business, missing, 1)
    assert missing_result.status == STATUS_SUPERSEDED
    assert "upsert" not in _kinds(missing_business)

    store, operational, business, owner, granted = _pair(store, STARTED + timedelta(minutes=1))
    store.controls[WEEK]["lease_expires_at"] = NOW
    business.db_now = NOW
    expired = _publish(store, operational, business, owner, granted.fencing_token)
    assert expired.status == STATUS_SUPERSEDED
    assert "upsert" not in _kinds(business)

    store, _operational, _business, owner, granted = _pair(store, STARTED + timedelta(minutes=2))
    releaser = Connection(store)
    assert release_week_lease(releaser, WEEK, owner.run_id, granted.fencing_token).status == "released"
    releaser.commit()
    recover = Connection(store)
    recovered = acquire_week_lease(recover, WEEK, owner.run_id, lease_seconds=900)
    recover.commit()
    stale_business = Connection(store)
    stale = _publish(store, Connection(store), stale_business, owner, granted.fencing_token)
    assert stale.status == STATUS_SUPERSEDED
    assert recovered.fencing_token == granted.fencing_token + 1
    assert "upsert" not in _kinds(stale_business)
    releaser = Connection(store)
    assert release_week_lease(
        releaser, WEEK, owner.run_id, recovered.fencing_token
    ).status == "released"
    releaser.commit()

    store, _operational, _business, foreign, foreign_grant = _pair(
        store,
        STARTED + timedelta(minutes=3),
    )
    assert store.controls[WEEK]["owner_run_id"] == foreign.run_id
    intruder_setup = Connection(store)
    intruder = create_run(
        intruder_setup,
        target_week_start=WEEK,
        trigger_type="manual",
        started_at=STARTED + timedelta(minutes=4),
    )
    intruder_setup.commit()
    rejected = _publish(
        store,
        Connection(store),
        Connection(store),
        intruder,
        foreign_grant.fencing_token,
    )
    assert rejected.status == STATUS_SKIPPED_IN_PROGRESS
    assert store.controls[WEEK]["owner_run_id"] == foreign.run_id
    assert store.controls[WEEK]["fencing_token"] == foreign_grant.fencing_token


def test_failure_before_commit_rolls_back_and_preserves_the_prior_publication():
    store, operational, business, run, granted = _pair()
    first = _publish(store, operational, business, run, granted.fencing_token)
    original = dict(store.kpis[("los_angeles", CLIENT_A, WEEK)])
    _store, operational, business, run, granted = _pair(store, STARTED + timedelta(minutes=5))
    business.fail_upsert = IntegrityError("INSERT", {}, Exception("constraint"))
    result = _publish(store, operational, business, run, granted.fencing_token)
    assert result.status == STATUS_FAILED_RETRYABLE
    assert result.publication_recorded is False
    assert business.commits == 0
    assert business.rollbacks == 1
    assert store.kpis[("los_angeles", CLIENT_A, WEEK)] == original
    assert latest_valid_publication(Connection(store), WEEK).run_id == first.run_id
    assert latest_run(Connection(store), target_week_start=WEEK).status == STATUS_FAILED_RETRYABLE


def test_uncertain_commit_is_not_success_and_is_not_retried():
    store, operational, business, run, granted = _pair()
    business.fail_commit = OperationalError("COMMIT", {}, Exception("connection reset"))
    result = _publish(store, operational, business, run, granted.fencing_token)
    assert result.status == "commit_uncertain"
    assert result.business_publication_valid is False
    assert result.publication_recorded is False
    assert _kinds(business).count("upsert") == 1
    assert business.commits == 0
    assert store.kpis == {}
    assert latest_run(Connection(store), target_week_start=WEEK).status == STATUS_RUNNING
    with pytest.raises(Exception) as rejected:
        _publish(store, Connection(store), Connection(store), run, granted.fencing_token)
    assert getattr(rejected.value, "code", "") == "status_conflict"


def test_recovery_recognizes_confirmed_metadata_and_does_not_upsert():
    store, operational, business, run, granted = _pair()
    business.fail_commit = OperationalError("COMMIT", {}, Exception("connection reset"))
    _publish(store, operational, business, run, granted.fencing_token)
    store.controls[WEEK].update(
        {
            "last_valid_publication_run_id": run.run_id,
            "last_valid_published_at": NOW,
            "published_row_count": 1,
            "dependency_fingerprint": FINGERPRINT,
            "rules_version": RULES,
        }
    )
    reader = Connection(store)
    recognized = resolve_uncertain_publication(
        reader,
        week_start=WEEK,
        run_id=run.run_id,
        dependency_fingerprint=FINGERPRINT,
        rules_version=RULES,
        published_row_count=1,
        operational=Connection(store),
        now=NOW,
        persist=True,
    )
    assert recognized.status == STATUS_SUCCEEDED
    assert recognized.recovered is True
    assert recognized.publication_recorded is True
    assert recognized.production_certified is False
    assert "upsert" not in _kinds(reader)
    assert latest_run(Connection(store), target_week_start=WEEK).status == STATUS_SUCCEEDED


def test_recovery_without_conclusive_evidence_does_not_invent_a_commit():
    store, operational, business, run, granted = _pair()
    business.fail_commit = OperationalError("COMMIT", {}, Exception("timed out"))
    _publish(store, operational, business, run, granted.fencing_token)
    reader = Connection(store)
    unresolved = resolve_uncertain_publication(
        reader,
        week_start=WEEK,
        run_id=run.run_id,
        dependency_fingerprint=FINGERPRINT,
        rules_version=RULES,
        operational=Connection(store),
        now=NOW,
        persist=True,
    )
    assert unresolved.status == "commit_uncertain"
    assert unresolved.evidence_inconclusive is True
    assert unresolved.publication_recorded is False
    assert "upsert" not in _kinds(reader)
    assert latest_run(Connection(store), target_week_start=WEEK).status == STATUS_RUNNING


def test_later_failed_run_does_not_replace_the_valid_publication():
    store, operational, business, run, granted = _pair()
    first = _publish(store, operational, business, run, granted.fencing_token)
    _store, operational, business, failed, failed_grant = _pair(store, STARTED + timedelta(minutes=5))
    result = _publish(
        store,
        operational,
        business,
        failed,
        failed_grant.fencing_token,
        quality=_quality(coverage_pending=("inventory_discrepancy_detected",)),
    )
    assert result.status == STATUS_BLOCKED_QUALITY
    assert latest_valid_publication(Connection(store), WEEK).run_id == first.run_id
    assert latest_run(Connection(store), target_week_start=WEEK).run_id == failed.run_id


def test_coordinator_does_not_use_an_alternate_fence_or_telemetry_write():
    source = open("pipelines/coordinator.py", encoding="utf-8").read()
    assert "record_fencing_advance" not in source
    assert "attempt_technical_commit" not in source
    assert "telemetry_events" not in source
    assert "@flow" not in source and "@task" not in source
    store, operational, business, run, granted = _pair()
    before = dict(store.controls[WEEK])
    with pytest.raises(Exception) as blocked:
        record_fencing_advance(
            operational,
            WEEK,
            expected_fencing_token=before["fencing_token"],
            owner_run_id=uuid4(),
        )
    assert blocked.value.code == "lease_authority_forbidden"
    assert store.controls[WEEK]["owner_run_id"] == before["owner_run_id"]
    assert store.controls[WEEK]["fencing_token"] == before["fencing_token"]
    business.autocommit = True
    with pytest.raises(Exception) as autocommit:
        _publish(store, Connection(store), business, run, granted.fencing_token)
    assert autocommit.value.code == "autocommit_forbidden"


@pytest.mark.skipif(
    not os.getenv("TRACKFLOW_PUBLICATION_TEST_DATABASE_URL"),
    reason="Pending isolated PostgreSQL. DATABASE_URL is not used.",
)
def test_real_postgresql_coordinator_is_not_silently_passed():
    from tests.test_pipeline_postgres_integration import (
        _assert_coordinator_atomicity_and_recovery,
    )

    _assert_coordinator_atomicity_and_recovery()
