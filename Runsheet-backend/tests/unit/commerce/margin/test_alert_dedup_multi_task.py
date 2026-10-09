"""Multi-task alert de-dup (design freeze 7, tasks.md item 23).

Two ECS tasks share one database (here one in-memory SQLite). Task 1 is not
the sweep leader and has no signal bus wired; it writes a flagged live record
through the real MarginService write path. Task 2 is the leader and drains the
queue. Leadership can move, and two RevenueGuard instances can process the
same pending row. ``uq_malert_dedupe`` keeps every case to one alert. The
Postgres race variant is ``tests/postgres/test_margin_concurrency.py``.
"""
from __future__ import annotations

import logging

import pytest

from Agents.overlay.data_contracts import RiskSignal, Severity

from ._revenue_guard_support import (
    RecordingRepo,
    alert_state,
    alerts,
    build_guard,
    delivery,
    write,
)
from ._service_support import SweepStore, build_service
from .conftest import TENANT_A

pytestmark = pytest.mark.usefixtures("flag_on")


@pytest.fixture(autouse=True)
def _delivery_leakage_stage(monkeypatch):
    """``.env.test`` turns invoicing on; these tests use the delivery stage."""
    from config.settings import clear_settings_cache

    monkeypatch.setenv("COMMERCE_INVOICING_ENABLED", "false")
    clear_settings_cache()
    yield
    clear_settings_cache()


async def _task1_write(repo, order_id="ORD-1", flag="negative", n=0):
    """Task 1 (non-leader, no bus): the margin service writes the record."""

    service = build_service(repo, SweepStore())  # no signal_bus, no provider
    result = await service._write(TENANT_A, delivery(order_id, n=n, flag=flag), "live")
    assert result.outcome == "written"
    assert result.record["alert_state"] == "pending"
    assert await alerts(repo, TENANT_A) == []  # the writer never alerts
    return result.record


async def test_non_leader_write_then_leader_cycle_one_alert(repo):
    task1_guard = build_guard(repo)  # also running on task 1, never the leader
    pending = await _task1_write(repo)
    # Even if a hint reached task 1's bus subscriber, nothing is buffered.
    await task1_guard.guard._on_signal(RiskSignal(
        source_agent="margin_feed", entity_id=pending["record_id"], entity_type="delivery",
        severity=Severity.HIGH, confidence=1.0, ttl_seconds=60, tenant_id=TENANT_A,
    ))
    assert task1_guard.guard._signal_buffer == []

    leader = build_guard(repo)  # task 2
    await leader.guard.monitor_cycle()
    assert len(await alerts(repo, TENANT_A)) == 1
    assert await alert_state(pending["record_id"]) == "done"

    await leader.guard.monitor_cycle()  # second cycle
    assert len(await alerts(repo, TENANT_A)) == 1


async def test_leadership_change_second_instance_still_one_alert(repo):
    pending = await _task1_write(repo)
    old_leader = build_guard(repo)
    await old_leader.guard.monitor_cycle()
    assert len(await alerts(repo, TENANT_A)) == 1
    # A replay: the row is put back to pending (e.g. a crash before commit on
    # another path), and the new leader processes it again.
    from sqlalchemy import update

    from persistence.database import session_scope
    from persistence.models import MarginRecordORM

    async with session_scope() as session:
        await session.execute(
            update(MarginRecordORM)
            .where(MarginRecordORM.record_id == pending["record_id"])
            .values(alert_state="pending")
        )
    new_leader = build_guard(repo)
    await new_leader.guard.monitor_cycle()
    assert len(await alerts(repo, TENANT_A)) == 1
    assert await alert_state(pending["record_id"]) == "done"


async def test_interleaved_instances_same_pending_record(repo, caplog):
    pending = await _task1_write(repo)
    second = build_guard(repo)

    # Instance 1 loads the pending batch, then instance 2 runs its whole pass
    # for the same record before instance 1 continues with its stale batch.
    first_repo = RecordingRepo(repo, [])

    async def _pending_records(tenant_id, limit=500):
        rows = await repo.pending_records(tenant_id, limit)
        await second.guard._evaluate_tenant(tenant_id, True)
        return rows

    first_repo.pending_records = _pending_records
    first = build_guard(first_repo)
    with caplog.at_level(logging.DEBUG, logger="commerce.services.margin_repository"):
        await first.guard._evaluate_tenant(TENANT_A, True)
    items = await alerts(repo, TENANT_A)
    assert len(items) == 1 and items[0]["record_id"] == pending["record_id"]
    assert await alert_state(pending["record_id"]) == "done"
    assert any("margin alert deduplicated" in r.getMessage() for r in caplog.records)


async def test_interleaved_leakage_one_alert_one_proposal(repo):
    for i in range(3):
        await write(repo, TENANT_A, delivery(f"ORD-{i}", n=i, flag="below"))
    second = build_guard(repo)
    second_proposals = []
    first_repo = RecordingRepo(repo, [])

    async def _pending_records(tenant_id, limit=500):
        rows = await repo.pending_records(tenant_id, limit)
        second_proposals.extend(await second.guard._evaluate_tenant(tenant_id, True))
        return rows

    first_repo.pending_records = _pending_records
    first = build_guard(first_repo)
    first_proposals = await first.guard._evaluate_tenant(TENANT_A, True)
    assert len(second_proposals) == 1
    assert first_proposals == []  # its leakage insert was a dedupe no-op
    assert len(await alerts(repo, TENANT_A, alert_type="leakage_proposal")) == 1


async def test_failing_record_stays_pending_others_proceed(repo):
    bad = await _task1_write(repo, "ORD-BAD")
    good = await _task1_write(repo, "ORD-GOOD", flag="missing", n=1)
    rec = RecordingRepo(repo, [])

    async def _outcome(tenant_id, record_id, alerts_=(), **kw):
        if record_id == bad["record_id"]:
            raise RuntimeError("lost connection")
        return await repo.record_alert_outcome(tenant_id, record_id, alerts_, **kw)

    rec.record_alert_outcome = _outcome
    leader = build_guard(rec)
    await leader.guard.monitor_cycle()
    assert await alert_state(bad["record_id"]) == "pending"
    assert await alert_state(good["record_id"]) == "done"
    assert [a["record_id"] for a in await alerts(repo, TENANT_A)] == [good["record_id"]]

    # The next cycle (on a healthy instance) picks the failed row up: one alert each.
    await build_guard(repo).guard.monitor_cycle()
    assert await alert_state(bad["record_id"]) == "done"
    assert sorted(a["record_id"] for a in await alerts(repo, TENANT_A)) == sorted(
        [bad["record_id"], good["record_id"]]
    )
