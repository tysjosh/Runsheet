"""Recompute: range cap, only_missing, supersede, equal-hash skip, digest, runs, audit.

AC-18, AC-44, design "Recompute". The as_of boundary itself is pinned by
``test_recompute_end_day_boundary.py`` (merge-blocking).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from commerce.models.margin import MarginRecomputeRequest
from commerce.services.margin_jobs import RECOMPUTE_HEARTBEAT_EVERY
from commerce.services.margin_repository import RecordFilters
from errors.exceptions import AppException

from ._cost_basis_support import add_entry
from ._margin_fakes import CapturingTelemetry
from ._service_support import (
    CapturingBus,
    SweepStore,
    build_service,
    invoice_doc,
    order_doc,
    seed_invoice,
    seed_order,
)
from .conftest import TENANT_A

UTC = timezone.utc
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
START, END = date(2026, 10, 1), date(2026, 10, 10)
IN_RANGE = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)


def _request(**overrides) -> MarginRecomputeRequest:
    values = dict(start_date=START, end_date=END, reason="late BOLs", stages=["invoice", "delivery"])
    values.update(overrides)
    return MarginRecomputeRequest(**values)


def _service(repo, **kwargs):
    store = SweepStore()
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("signal_bus", CapturingBus())
    return build_service(repo, store, **kwargs), store


async def _override(repo, micros=2_500_000):
    await add_entry(repo, TENANT_A, "override", unit_cost_micros=micros, effective_at=datetime(2026, 1, 1, tzinfo=UTC))


async def _run(service, repo, **request):
    result = await service.start_recompute(TENANT_A, "admin-1", _request(**request))
    await service.drain()
    return await repo.get_run(TENANT_A, result["run_id"])


def test_range_cap_92_days_ok_93_rejected():
    assert _request(start_date=date(2026, 1, 1), end_date=date(2026, 4, 2))
    with pytest.raises(ValidationError):
        _request(start_date=date(2026, 1, 1), end_date=date(2026, 4, 3))


async def test_only_missing_reprocesses_frozen_none_and_skips_costed(repo, flag_on, make_candidate, make_missing_cost):
    service, store = _service(repo)
    for invoice_id in ("I-NONE", "I-COSTED"):
        await seed_invoice(
            store, invoice_doc(invoice_id, created_at=IN_RANGE, delivered_at=IN_RANGE, status="open", finalized_at=IN_RANGE)
        )
    none_key, costed_key = "invoice:I-NONE:line:0", "invoice:I-COSTED:line:0"
    await repo.write_record(
        TENANT_A, make_missing_cost(stage="invoice", source_key=none_key, invoice_id="I-NONE", line_index=0, as_of=IN_RANGE), "finalize"
    )
    await repo.write_record(
        TENANT_A, make_candidate(stage="invoice", source_key=costed_key, invoice_id="I-COSTED", line_index=0, as_of=IN_RANGE), "finalize"
    )
    await _override(repo)  # the late cost arrives

    run = await _run(service, repo, stages=["invoice"])

    assert run["status"] == "completed"
    assert run["counts"]["sources"] == 2 and run["counts"]["written"] == 1 and run["counts"]["skipped"] == 1
    versions = await repo.record_versions(TENANT_A, "invoice", none_key)
    assert [(v["version"], v["status"]) for v in versions] == [(1, "superseded"), (2, "active")]
    latest = versions[-1]
    assert latest["method"] == "override" and latest["origin"] == "recompute"
    assert latest["recompute_run_id"] == run["run_id"] and latest["frozen_at"] is not None
    assert latest["alert_state"] == "none"
    assert [v["version"] for v in await repo.record_versions(TENANT_A, "invoice", costed_key)] == [1]


async def test_only_missing_false_equal_hash_skips(repo, flag_on):
    service, store = _service(repo)
    await _override(repo)
    await seed_order(store, order_doc("ORD-1", created_at=IN_RANGE, delivered_at=IN_RANGE))
    first = await _run(service, repo, stages=["delivery"], only_missing=False)
    second = await _run(service, repo, stages=["delivery"], only_missing=False)
    assert first["counts"]["written"] == 1
    assert second["counts"]["written"] == 0 and second["counts"]["skipped"] == 1
    assert [v["version"] for v in await repo.record_versions(TENANT_A, "delivery", "order:ORD-1")] == [1]


async def test_zero_flag_run_digest_done_and_no_signal(repo, flag_on):
    bus = CapturingBus()
    service, store = _service(repo, signal_bus=bus)
    await _override(repo)  # 3.00 - 2.50 = 0.50 >= floor 0.10: no alerting flag
    await seed_order(store, order_doc("ORD-1", created_at=IN_RANGE, delivered_at=IN_RANGE))
    run = await _run(service, repo, stages=["delivery"])
    assert run["counts"]["written"] == 1
    assert sum(run["counts"]["by_flag"].values()) == 0
    assert run["digest_state"] == "done"
    assert bus.published == []


async def test_flagged_run_digest_pending_and_one_hint(repo, flag_on):
    bus = CapturingBus()
    service, store = _service(repo, signal_bus=bus)
    for order_id in ("ORD-1", "ORD-2"):
        await seed_order(store, order_doc(order_id, created_at=IN_RANGE, delivered_at=IN_RANGE))
    run = await _run(service, repo, stages=["delivery"])
    assert run["counts"]["by_flag"]["missing_cost"] == 2
    assert run["digest_state"] == "pending"
    (signal,) = bus.published  # recompute records never publish per-record signals
    assert signal.entity_type == "margin_recompute_run" and signal.entity_id == run["run_id"]
    assert signal.severity.value == "low"
    assert signal.context == {"run_id": run["run_id"], "counts_by_flag": run["counts"]["by_flag"]}
    records = (await repo.list_records(TENANT_A, RecordFilters(), limit=10)).items
    assert {r["alert_state"] for r in records} == {"none"}


async def test_concurrent_start_is_409_and_stale_run_is_taken_over(repo, flag_on, caplog):
    service, _ = _service(repo)
    running = await repo.start_run(
        TENANT_A, requested_by="a", start_date=START, end_date=END, stages=["delivery"], only_missing=True, reason="r"
    )
    with pytest.raises(AppException) as excinfo:
        await service.start_recompute(TENANT_A, "admin-1", _request())
    assert excinfo.value.status_code == 409
    assert excinfo.value.error_code == "MARGIN_RECOMPUTE_RUNNING"
    assert excinfo.value.details == {"run_id": running["run_id"]}

    await repo.heartbeat(TENANT_A, running["run_id"], now=datetime.now(UTC) - timedelta(hours=2))
    with caplog.at_level(logging.WARNING):
        run = await _run(service, repo)
    assert run["status"] == "completed"
    assert (await repo.get_run(TENANT_A, running["run_id"]))["status"] == "failed"
    assert any("stale" in r.getMessage() for r in caplog.records)


async def test_heartbeat_every_100_sources(repo, flag_on, monkeypatch):
    service, store = _service(repo)
    await _override(repo)
    for i in range(RECOMPUTE_HEARTBEAT_EVERY + 20):
        await seed_order(store, order_doc(f"ORD-{i:04d}", created_at=IN_RANGE, delivered_at=IN_RANGE))
    beats = []
    real = repo.heartbeat

    async def counting(tenant_id, run_id, **kwargs):
        beats.append(dict(kwargs.get("counts") or {}))
        return await real(tenant_id, run_id, **kwargs)

    monkeypatch.setattr(repo, "heartbeat", counting)
    run = await _run(service, repo, stages=["delivery"])
    assert run["counts"]["sources"] == RECOMPUTE_HEARTBEAT_EVERY + 20
    assert len(beats) == 1 and beats[0]["written"] == RECOMPUTE_HEARTBEAT_EVERY


async def test_audit_start_and_finish(repo, flag_on):
    telemetry = CapturingTelemetry()
    service, store = _service(repo, telemetry=telemetry)
    await seed_order(store, order_doc("ORD-1", created_at=IN_RANGE, delivered_at=IN_RANGE))
    run = await _run(service, repo, stages=["delivery"])
    started, finished = telemetry.events
    assert started["event_type"] == "margin_recompute_started" and started["action"] == "start"
    assert started["user_id"] == "admin-1" and started["resource_id"] == run["run_id"]
    assert started["details"]["tenant_id"] == TENANT_A
    assert (started["details"]["start_date"], started["details"]["end_date"]) == ("2026-10-01", "2026-10-10")
    assert "reason" not in started["details"]
    assert finished["event_type"] == "margin_recompute_finished" and finished["details"]["status"] == "completed"
    assert finished["details"]["counts"]["written"] == 1
    assert set(finished["details"]["counts"]) == {
        "sources", "out_of_range", "written", "skipped", "invalid_inputs", "no_inputs", "errors", "by_flag"
    }


async def test_unexpected_failure_marks_run_failed(repo, flag_on, monkeypatch, caplog):
    telemetry = CapturingTelemetry()
    service, _ = _service(repo, telemetry=telemetry)

    async def broken(*args, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(service.orders, "search", broken)
    with caplog.at_level(logging.ERROR):
        run = await _run(service, repo, stages=["delivery"])
    assert run["status"] == "failed" and run["digest_state"] == "none"
    assert any("margin recompute failed" in r.getMessage() for r in caplog.records)
    assert telemetry.events[-1]["details"]["status"] == "failed"
