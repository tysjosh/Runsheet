"""Dispatch Board telemetry and audit hooks (plan task 14; design K16, K17; R22, R24).

Metrics carry K16 names and allow-listed tags only, and no metric tag or log
line carries a customer name, an address or a free-text reason (R24.3).
"""
from __future__ import annotations

import logging
import uuid

import pytest

from errors.exceptions import AppException
from fuel.services.dispatch_board_models import HistoryQuery, ValidateBody
from fuel.services.dispatch_board_telemetry import METRIC_NAMES, BoardTelemetry, safe_tags
from tests.unit._dispatch_board_fakes import T, TODAY, TZ, Harness, SpyTelemetry
from tests.unit._loading_plan_fakes import FakeActivityLog

PII = ("Jane Doe Farms", "9 Secret Lane", "Secret ack reason", "jane@example")


def test_safe_tags_allow_list_and_value_shape():
    tags = safe_tags({"tenant_id": T, "type": "assign_orders", "customer": "Jane", "reason_code": "Jane Doe", "outcome": None})
    assert tags == {"tenant_id": T, "type": "assign_orders", "reason_code": "other"}


def test_unknown_metric_names_are_rejected():
    with pytest.raises(ValueError):
        BoardTelemetry(telemetry=SpyTelemetry()).metric("board.something_else", 1)
    assert {"board.snapshot.ms", "board.command.count", "board.undo.count", "board.publish.lane.ms"} <= METRIC_NAMES


def test_metric_never_raises_into_the_caller():
    class Broken:
        def record_metric(self, *a, **k):
            raise RuntimeError("telemetry down")

    BoardTelemetry(telemetry=Broken()).metric("board.command.count", 1, tenant_id=T)
    BoardTelemetry(telemetry=None, telemetry_getter=lambda: None).metric("board.command.count", 1)


async def test_k16_metrics_and_no_customer_data_anywhere(caplog):
    caplog.set_level(logging.DEBUG)
    h = Harness()
    h.seed_order("o1", customer_name=PII[0], ship_to_address=PII[1])
    h.seed_order("o2", customer_name=PII[0], ship_to_address=PII[1], status="on_hold")
    h.names[(T, "user-1")] = "jane@example.com"
    await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    await h.service.validate(T, TODAY, ValidateBody.model_validate({"item": {"kind": "order", "ids": ["o1"]}, "candidates": ["T1"]}), tz=TZ)
    await h.lane_with("T1", "o1")
    warning = next(c for c in h.draft().lanes["T1"].checks if c.reason_code == "no_driver")
    await h.run("acknowledge_warning", truck_id="T1", warning_id=warning.warning_id, reason=PII[2], versions={})
    with pytest.raises(AppException):
        await h.run("assign_orders", order_ids=["o2"], truck_id="T1", lanes=("T1",))  # blocked
    with pytest.raises(AppException):
        await h.run("pair_driver", truck_id="T1", driver_id="d1", versions={"T1": 0})  # conflict
    assign = next(d for d in h.store.command_docs() if d["type"] == "assign_orders" and d["result"] == "committed")
    await h.run("revert", target_command_id=assign["command_id"], lanes=("T1",))
    await h.service.history(T, TODAY, HistoryQuery(), tz=TZ)

    names = {m[0] for m in h.telemetry.metrics}
    assert {
        "board.snapshot.ms", "board.validate.ms", "board.command.ms", "board.command.count",
        "board.conflict.count", "board.override.count", "board.undo.count",
    } <= names
    results = {m[2].get("result") for m in h.telemetry.metrics if m[0] == "board.command.count"}
    assert {"committed", "blocked", "conflict"} <= results
    assert all(m[2].get("input_modality") for m in h.telemetry.metrics if m[0] == "board.command.count")
    for name, _value, tags in h.telemetry.metrics:
        assert name in METRIC_NAMES
        for value in tags.values():
            assert not any(p.lower() in str(value).lower() for p in PII)
    text = caplog.text
    for p in PII:
        assert p not in text


async def test_audit_publish_writes_activity_log_and_audit_events_with_ids_only():
    activity = FakeActivityLog()
    spy = SpyTelemetry()
    telemetry = BoardTelemetry(telemetry=spy, activity_log=activity)
    await telemetry.audit_publish(
        tenant_id=T,
        user_id="user-1",
        actor_name="ana",
        service_date=TODAY.isoformat(),
        lanes=[
            {"truck_id": "T1", "plans": ["bp-L1-r1"], "orders": ["o1"], "driver_id": "d1", "result": "published", "customer_name": PII[0]},
            {"truck_id": "T2", "plans": [], "orders": ["o2"], "driver_id": "d2", "result": "failed"},
        ],
    )
    entries = activity.of("dispatch_board_publish")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["actor_name"] == "ana" and entry["outcome"] == "partial"
    assert [l["truck_id"] for l in entry["lanes"]] == ["T1", "T2"]
    assert PII[0] not in str(entry)
    assert [(a[0], a[1], a[2], a[3], a[4]) for a in spy.audits] == [
        ("dispatch_board", "user-1", "board_lane", "T1", "publish"),
        ("dispatch_board", "user-1", "board_lane", "T2", "publish"),
    ]
    assert PII[0] not in str(spy.audits)


async def test_audit_publish_survives_a_failing_activity_log(caplog):
    class Broken:
        async def log(self, entry):
            raise RuntimeError("index down " + PII[1])

    spy = SpyTelemetry()
    await BoardTelemetry(telemetry=spy, activity_log=Broken()).audit_publish(
        tenant_id=T, user_id="u", actor_name="ana", service_date="2026-10-08", lanes=[{"truck_id": "T1", "result": "published"}]
    )
    assert len(spy.audits) == 1 and PII[1] not in caplog.text
