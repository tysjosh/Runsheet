"""RevenueGuard on the margin queue (tasks.md item 22).

AC-27, AC-38–AC-41, AC-43–AC-45, design freezes 7 and 11, the leakage
"live" reading, alert-resolution audits, ids-only payloads (FR8.2) and the
LearningPolicyAgent interaction.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from Agents.overlay.data_contracts import (
    InterventionProposal,
    PolicyChangeProposal,
    RiskSignal,
    Severity,
)
from Agents.overlay.revenue_guard import (
    ACTION_ALERT_SHADOW,
    ACTION_DIGEST_SHADOW,
    ACTION_LEAKAGE_SHADOW,
    RevenueGuard,
)
from commerce.services import margin_service as margin_service_module
from config.settings import clear_settings_cache
from errors.exceptions import AppException

from ._revenue_guard_support import (
    RecordingRepo,
    alert_state,
    alerts,
    build_guard,
    completed_run,
    delivery,
    invoice_line,
    record,
    write,
)
from ._service_support import SweepStore, assert_no_money, build_service
from .conftest import TENANT_A, TENANT_B

UTC = timezone.utc

pytestmark = pytest.mark.usefixtures("flag_on")


@pytest.fixture(autouse=True)
def invoicing(monkeypatch):
    """Set ``commerce_invoicing_enabled`` (leakage stage selection).

    Off by default here (``.env.test`` turns it on), so the delivery stage is
    the leakage stage unless a test switches it.
    """

    def _set(enabled: bool) -> None:
        monkeypatch.setenv("COMMERCE_INVOICING_ENABLED", "true" if enabled else "false")
        clear_settings_cache()

    _set(False)
    yield _set
    clear_settings_cache()


async def _below_floor_deliveries(repo, tenant, count, *, customer="CUST-1", product="DIESEL_2",
                                  prefix="ORD", start=0, mode="live"):
    rows = []
    for i in range(start, start + count):
        rows.append(await write(
            repo, tenant,
            delivery(f"{prefix}-{i}", n=i, flag="below", customer=customer, product=product),
            mode,
        ))
    return rows


# ---------------------------------------------------------------------------
# Construction, subscriptions, signals (FR5.2, AC-40, freeze 7/11)
# ---------------------------------------------------------------------------


def test_subscriptions_exact_and_cooldown_60():
    h = build_guard(None)
    assert h.guard._subscription_specs == [
        {"message_type": RiskSignal, "filters": {"source_agent": "margin_feed"}}
    ]
    assert h.guard.cooldown_minutes == 60
    assert h.guard._leakage_threshold == 3


def test_removed_percentage_path():
    import Agents.overlay.revenue_guard as module

    for name in ("DEFAULT_MARGIN_TARGET_PCT", "JOBS_CURRENT_INDEX", "REVENUE_REPORTS_INDEX"):
        assert not hasattr(module, name), name
    for name in ("_compute_route_margins", "_detect_leakage", "_maybe_generate_weekly_report"):
        assert not hasattr(RevenueGuard, name), name
    assert not hasattr(build_guard(None).guard, "_route_margins")


async def test_on_signal_discards():
    h = build_guard(None)
    signal = RiskSignal(
        source_agent="margin_feed", entity_id="mr_1", entity_type="delivery",
        severity=Severity.HIGH, confidence=1.0, ttl_seconds=60, tenant_id=TENANT_A,
    )
    await h.guard._on_signal(signal)
    await h.guard._on_signal(signal)
    assert h.guard._signal_buffer == []
    assert h.guard._signals_discarded == 2


async def test_evaluate_stub_touches_nothing(repo):
    log = []
    rec = RecordingRepo(repo, log)
    h = build_guard(rec, log=log)
    assert await h.guard.evaluate([]) == []
    assert log == [] and h.store.calls == []


async def test_no_repository_warns_once(caplog):
    h = build_guard(None)
    with caplog.at_level(logging.WARNING, logger="agent.revenue_guard"):
        assert await h.guard.monitor_cycle() == ([], [])
        assert await h.guard.monitor_cycle() == ([], [])
    assert sum("no margin repository" in r.getMessage() for r in caplog.records) == 1


async def test_flag_off_touches_nothing(repo, monkeypatch):
    await write(repo, TENANT_A, delivery("ORD-1", flag="negative"))
    monkeypatch.setenv("COMMERCE_MARGIN_FEED_ENABLED", "false")
    clear_settings_cache()
    log = []
    h = build_guard(RecordingRepo(repo, log), log=log)
    assert await h.guard.monitor_cycle() == ([], [])
    assert log == []


# ---------------------------------------------------------------------------
# Per-record alerts (AC-38, AC-39, AC-41, AC-45)
# ---------------------------------------------------------------------------


async def test_negative_margin_one_high_alert_across_cycles(repo):
    row = await write(repo, TENANT_A, delivery("ORD-1", flag="negative"))
    h = build_guard(repo)
    await h.guard.monitor_cycle()
    await h.guard.monitor_cycle()
    items = await alerts(repo, TENANT_A)
    assert len(items) == 1
    alert = items[0]
    assert (alert["alert_type"], alert["severity"], alert["status"]) == ("negative_margin", "high", "open")
    assert alert["dedupe_key"] == row["record_id"] == alert["record_id"]
    assert alert["order_id"] == "ORD-1"
    assert await alert_state(row["record_id"]) == "done"
    assert h.store.calls == []  # AC-45: no document-store (jobs_current) access


async def test_missing_cost_one_medium_alert(repo):
    row = await write(repo, TENANT_A, delivery("ORD-1", flag="missing"))
    h = build_guard(repo)
    await h.guard.monitor_cycle()
    items = await alerts(repo, TENANT_A)
    assert [(a["alert_type"], a["severity"]) for a in items] == [("missing_cost", "medium")]
    assert items[0]["details"]["no_cost_reason"] == "no_lots_no_rack"
    assert await alert_state(row["record_id"]) == "done"
    assert h.store.calls == []


async def test_invoice_record_alerts_too(repo):
    await write(repo, TENANT_A, invoice_line("INV-1", 0, "ORD-1", flag="negative"))
    h = build_guard(repo)
    await h.guard.monitor_cycle()
    assert [a["alert_type"] for a in await alerts(repo, TENANT_A)] == ["negative_margin"]


async def test_below_floor_alone_no_alert(repo):
    row = await write(repo, TENANT_A, delivery("ORD-1", flag="below"))
    h = build_guard(repo)
    _, proposals = await h.guard.monitor_cycle()
    assert proposals == []
    assert await alerts(repo, TENANT_A) == []
    assert await alert_state(row["record_id"]) == "done"


async def test_estimate_and_recompute_records_never_alert(repo):
    await write(repo, TENANT_A, delivery("ORD-1", flag="negative", stage="order_estimate",
                                         source_key="estimate:ORD-1"))
    await write(repo, TENANT_A, delivery("ORD-2", flag="negative"), "recompute")
    h = build_guard(repo)
    await h.guard.monitor_cycle()
    assert await alerts(repo, TENANT_A) == []


# ---------------------------------------------------------------------------
# Leakage (AC-39, AC-40, freeze "live" reading, Simplification 2)
# ---------------------------------------------------------------------------


async def test_three_below_floor_sales_one_proposal_and_pending_review_alert(repo):
    rows = await _below_floor_deliveries(repo, TENANT_A, 3)
    h = build_guard(repo)
    _, proposals = await h.guard.monitor_cycle()
    assert len(proposals) == 1
    proposal = proposals[0]
    assert isinstance(proposal, PolicyChangeProposal)
    assert not isinstance(proposal, InterventionProposal)
    assert proposal.source_agent == "revenue_guard"
    assert proposal.tenant_id == TENANT_A
    assert proposal.parameter == "margin.review.CUST-1.DIESEL_2"
    assert proposal.old_value == {"flag": "below_floor", "consecutive": 3}
    assert proposal.new_value == {"action": "review_pricing"}
    assert proposal.rollback_plan == {"action": "none", "reason": "advisory only"}
    assert sorted(proposal.evidence) == sorted(r["record_id"] for r in rows)
    leak = await alerts(repo, TENANT_A, alert_type="leakage_proposal")
    assert len(leak) == 1
    assert (leak[0]["severity"], leak[0]["status"]) == ("high", "pending_review")
    assert leak[0]["proposal_id"] == proposal.proposal_id
    newest = rows[-1]["record_id"]
    assert leak[0]["dedupe_key"] == f"CUST-1|DIESEL_2|{newest}"
    # Active mode publishes the proposal; a PolicyChangeProposal never
    # reaches the ConfirmationProtocol (FR5.9: nothing changes prices).
    assert h.bus.published == [proposal]
    h.confirmation.process_mutation.assert_not_called()
    assert all([await alert_state(r["record_id"]) == "done" for r in rows])
    assert_no_money(proposal.model_dump(mode="json"))


async def test_two_below_floor_sales_no_proposal(repo):
    await _below_floor_deliveries(repo, TENANT_A, 2)
    h = build_guard(repo)
    _, proposals = await h.guard.monitor_cycle()
    assert proposals == []


async def test_unflagged_sale_breaks_the_run(repo):
    await _below_floor_deliveries(repo, TENANT_A, 2)
    await write(repo, TENANT_A, delivery("ORD-OK", n=5))  # newest sale is healthy
    h = build_guard(repo)
    _, proposals = await h.guard.monitor_cycle()
    assert proposals == []


async def test_cooldown_stops_a_second_proposal(repo):
    await _below_floor_deliveries(repo, TENANT_A, 3)
    h = build_guard(repo)
    assert len((await h.guard.monitor_cycle())[1]) == 1
    leak = (await alerts(repo, TENANT_A, alert_type="leakage_proposal"))[0]
    await repo.transition_alert(TENANT_A, leak["alert_id"], action="dismiss", actor="admin-1")
    await _below_floor_deliveries(repo, TENANT_A, 1, start=3)
    _, proposals = await h.guard.monitor_cycle()
    assert proposals == []  # same instance: 60-minute cooldown per key
    assert len(await alerts(repo, TENANT_A, alert_type="leakage_proposal")) == 1


async def test_pending_review_stops_a_second_proposal(repo):
    await _below_floor_deliveries(repo, TENANT_A, 3)
    assert len((await build_guard(repo).guard.monitor_cycle())[1]) == 1
    await _below_floor_deliveries(repo, TENANT_A, 1, start=3)
    fresh = build_guard(repo)  # no cooldown in memory (e.g. a leader change)
    _, proposals = await fresh.guard.monitor_cycle()
    assert proposals == []
    assert len(await alerts(repo, TENANT_A, alert_type="leakage_proposal")) == 1


async def test_two_recompute_plus_one_live_one_proposal(repo):
    await _below_floor_deliveries(repo, TENANT_A, 2, mode="recompute")
    await _below_floor_deliveries(repo, TENANT_A, 1, start=2)
    h = build_guard(repo)
    _, proposals = await h.guard.monitor_cycle()
    assert len(proposals) == 1
    assert len(proposals[0].evidence) == 3


async def test_three_recompute_only_no_proposal(repo):
    await _below_floor_deliveries(repo, TENANT_A, 3, mode="recompute")
    h = build_guard(repo)
    _, proposals = await h.guard.monitor_cycle()
    assert proposals == []
    assert await alerts(repo, TENANT_A) == []


async def test_invoice_split_lines_count_as_one_sale(repo, invoicing):
    invoicing(True)
    for i in range(3):
        await write(repo, TENANT_A, invoice_line(f"INV-{i}", 0, f"ORD-{i}", n=i, flag="below"))
        await write(repo, TENANT_A, invoice_line(f"INV-{i}", 1, f"ORD-{i}", n=i))  # OI-14 split
    h = build_guard(repo)
    _, proposals = await h.guard.monitor_cycle()
    assert len(proposals) == 1
    assert len(proposals[0].evidence) == 6  # every line of the three sales


async def test_two_split_invoices_are_two_sales_not_four(repo, invoicing):
    invoicing(True)
    for i in range(2):
        await write(repo, TENANT_A, invoice_line(f"INV-{i}", 0, f"ORD-{i}", n=i, flag="below"))
        await write(repo, TENANT_A, invoice_line(f"INV-{i}", 1, f"ORD-{i}", n=i, flag="below"))
    h = build_guard(repo)
    assert (await h.guard.monitor_cycle())[1] == []


async def test_delivery_and_invoice_for_one_order_count_once(repo, invoicing):
    invoicing(True)
    for i in range(2):
        await write(repo, TENANT_A, delivery(f"ORD-{i}", n=i, flag="below"))
        await write(repo, TENANT_A, invoice_line(f"INV-{i}", 0, f"ORD-{i}", n=i, flag="below"))
    h = build_guard(repo)
    assert (await h.guard.monitor_cycle())[1] == []  # 2 sales, not 4 records
    await write(repo, TENANT_A, delivery("ORD-2", n=2, flag="below"))
    await write(repo, TENANT_A, invoice_line("INV-2", 0, "ORD-2", n=2, flag="below"))
    _, proposals = await h.guard.monitor_cycle()
    assert len(proposals) == 1
    evidence = {(await record(repo, TENANT_A, rid))["stage"] for rid in proposals[0].evidence}
    assert evidence == {"invoice"}


async def test_delivery_stage_when_invoicing_off(repo, invoicing):
    invoicing(False)
    for i in range(3):
        await write(repo, TENANT_A, invoice_line(f"INV-{i}", 0, f"ORD-{i}", n=i, flag="below"))
    h = build_guard(repo)
    assert (await h.guard.monitor_cycle())[1] == []  # invoice records are not the leakage stage


# ---------------------------------------------------------------------------
# Tenant isolation (AC-27, FR5.6)
# ---------------------------------------------------------------------------


async def test_tenant_a_below_floor_never_counts_for_b(repo):
    await _below_floor_deliveries(repo, TENANT_A, 3)
    await _below_floor_deliveries(repo, TENANT_B, 2, prefix="B-ORD")
    h = build_guard(repo)
    _, proposals = await h.guard.monitor_cycle()
    assert [p.tenant_id for p in proposals] == [TENANT_A]
    assert await alerts(repo, TENANT_B) == []
    a_ids = {r["record_id"] for r in await _all_records(repo, TENANT_A)}
    assert set(proposals[0].evidence) <= a_ids
    # B's third below-floor sale triggers B's own proposal from B's records only.
    await _below_floor_deliveries(repo, TENANT_B, 1, prefix="B-ORD", start=2)
    _, proposals = await h.guard.monitor_cycle()
    assert [p.tenant_id for p in proposals] == [TENANT_B]
    b_ids = {r["record_id"] for r in await _all_records(repo, TENANT_B)}
    assert set(proposals[0].evidence) <= b_ids


async def _all_records(repo, tenant):
    from commerce.services.margin_repository import RecordFilters

    return (await repo.list_records(tenant, RecordFilters(status="all"), limit=200)).items


# ---------------------------------------------------------------------------
# Modes (AC-43, freeze 11)
# ---------------------------------------------------------------------------


async def test_disabled_tenant_untouched_after_mode_read(repo):
    a = await write(repo, TENANT_A, delivery("ORD-A", flag="negative"))
    await write(repo, TENANT_B, delivery("ORD-B", flag="negative"))
    log = []
    rec = RecordingRepo(repo, log)
    h = build_guard(rec, modes={TENANT_A: "disabled", TENANT_B: "active_gated"}, log=log)
    await h.guard.monitor_cycle()
    assert len(await alerts(repo, TENANT_B)) == 1
    assert await alerts(repo, TENANT_A) == []
    assert await alert_state(a["record_id"]) == "pending"
    mode_read = log.index(("mode", TENANT_A))
    assert [entry for entry in log[mode_read + 1:] if entry[1] == TENANT_A] == []
    assert h.activity.entries == [] and h.bus.published == []


async def test_disabled_rows_expire_after_seven_days_without_alert(repo):
    old = await write(repo, TENANT_A, delivery("ORD-OLD", flag="negative"),
                      now=datetime.now(UTC) - timedelta(days=8))
    recent = await write(repo, TENANT_A, delivery("ORD-NEW", n=1, flag="negative"),
                         now=datetime.now(UTC) - timedelta(days=6))
    h = build_guard(repo, modes={TENANT_A: "disabled"})
    await h.guard.monitor_cycle()
    assert await alert_state(old["record_id"]) == "expired"
    assert await alert_state(recent["record_id"]) == "pending"
    assert await alerts(repo, TENANT_A) == []
    # Re-enabled: only the unexpired row alerts; the expired one never does.
    h.flags.modes[TENANT_A] = "active_gated"
    await h.guard.monitor_cycle()
    assert [a["record_id"] for a in await alerts(repo, TENANT_A)] == [recent["record_id"]]
    assert await alert_state(old["record_id"]) == "expired"


async def test_shadow_writes_activity_entries_only(repo):
    neg = await write(repo, TENANT_A, delivery("ORD-NEG", flag="negative", customer="CUST-9"))
    rows = await _below_floor_deliveries(repo, TENANT_A, 3, start=1)
    run_id = await completed_run(repo, TENANT_A, {"missing_cost": 0, "negative_margin": 2, "below_floor": 0})
    h = build_guard(repo, modes={TENANT_A: "shadow"})
    _, proposals = await h.guard.monitor_cycle()
    assert len(proposals) == 1
    assert await alerts(repo, TENANT_A) == []
    assert sorted(h.activity.actions()) == sorted(
        [ACTION_ALERT_SHADOW, ACTION_LEAKAGE_SHADOW, ACTION_DIGEST_SHADOW]
    )
    assert h.store.calls == []  # agent_shadow_proposals never indexed
    assert h.bus.published == []
    for r in [neg, *rows]:
        assert await alert_state(r["record_id"]) == "done"
    assert (await repo.get_run(TENANT_A, run_id))["digest_state"] == "done"
    by_action = {e["action_type"]: e for e in h.activity.entries}
    alert_entry = by_action[ACTION_ALERT_SHADOW]
    assert alert_entry["agent_id"] == "revenue_guard" and alert_entry["tenant_id"] == TENANT_A
    assert alert_entry["details"]["record_id"] == neg["record_id"]
    assert alert_entry["details"]["flags"] == ["negative_margin"]
    leak_entry = by_action[ACTION_LEAKAGE_SHADOW]["details"]
    assert leak_entry["proposal_id"] == proposals[0].proposal_id
    assert (leak_entry["customer_id"], leak_entry["product_code"]) == ("CUST-1", "DIESEL_2")
    assert sorted(leak_entry["evidence"]) == sorted(r["record_id"] for r in rows)
    assert by_action[ACTION_DIGEST_SHADOW]["details"]["run_id"] == run_id
    # FR8.2: activity entries are dispatcher-visible; ids, flag names and
    # counts only. Flag names (``missing_cost``) are allowed as count keys.
    for entry in h.activity.entries:
        _assert_ids_and_flags_only(entry)
    assert_no_money(proposals[0].model_dump(mode="json"))


_FLAG_NAMES = {"missing_cost", "negative_margin", "below_floor", "terminal_unattributed"}


def _assert_ids_and_flags_only(value):
    if isinstance(value, dict):
        assert_no_money({k: None for k in value if k not in _FLAG_NAMES})
        for key, item in value.items():
            if key in _FLAG_NAMES:
                assert isinstance(item, int), (key, item)
            else:
                _assert_ids_and_flags_only(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_ids_and_flags_only(item)


async def test_unknown_mode_fails_closed(repo):
    await write(repo, TENANT_A, delivery("ORD-1", flag="negative"))
    h = build_guard(repo, modes={TENANT_A: "something_new"})
    await h.guard.monitor_cycle()
    assert await alerts(repo, TENANT_A) == []


async def test_mode_read_failure_skips_tenant_and_continues(repo):
    a = await write(repo, TENANT_A, delivery("ORD-A", flag="negative"))
    await write(repo, TENANT_B, delivery("ORD-B", flag="negative"))
    h = build_guard(repo)
    real = h.guard._get_mode

    async def _get_mode(tenant_id):
        if tenant_id == TENANT_A:
            raise RuntimeError("flag store down")
        return await real(tenant_id)

    h.guard._get_mode = _get_mode
    await h.guard.monitor_cycle()
    assert await alert_state(a["record_id"]) == "pending"
    assert len(await alerts(repo, TENANT_B)) == 1


async def test_tenant_pass_failure_is_isolated(repo):
    await write(repo, TENANT_A, delivery("ORD-A", flag="negative"))
    await write(repo, TENANT_B, delivery("ORD-B", flag="negative"))
    log = []
    rec = RecordingRepo(repo, log)
    real = repo.pending_digests

    async def _digests(tenant_id):
        if tenant_id == TENANT_A:
            raise RuntimeError("boom")
        return await real(tenant_id)

    rec.pending_digests = _digests
    h = build_guard(rec)
    await h.guard.monitor_cycle()
    assert await alerts(repo, TENANT_A) == []
    assert len(await alerts(repo, TENANT_B)) == 1


async def test_queue_query_failure_returns_empty(repo, caplog):
    h = build_guard(repo)

    class _Broken:
        async def pending_work_tenants(self):
            raise RuntimeError("db down")

    rec = RecordingRepo(repo, [])
    rec.discovery = _Broken()
    h.guard.set_margin_repository(rec)
    with caplog.at_level(logging.ERROR, logger="agent.revenue_guard"):
        assert await h.guard.monitor_cycle() == ([], [])
    assert any("queue query failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Failure isolation per record
# ---------------------------------------------------------------------------


async def test_one_failing_record_does_not_block_others(repo, caplog):
    bad = await write(repo, TENANT_A, delivery("ORD-BAD", flag="negative"))
    good = await write(repo, TENANT_A, delivery("ORD-GOOD", n=1, flag="missing"))
    rec = RecordingRepo(repo, [])
    real = repo.record_alert_outcome

    async def _outcome(tenant_id, record_id, alerts_=(), **kw):
        if record_id == bad["record_id"]:
            raise RuntimeError("write failed")
        return await real(tenant_id, record_id, alerts_, **kw)

    rec.record_alert_outcome = _outcome
    h = build_guard(rec)
    with caplog.at_level(logging.ERROR, logger="agent.revenue_guard"):
        await h.guard.monitor_cycle()
    assert await alert_state(bad["record_id"]) == "pending"
    assert await alert_state(good["record_id"]) == "done"
    assert [a["record_id"] for a in await alerts(repo, TENANT_A)] == [good["record_id"]]
    messages = [r.getMessage() for r in caplog.records]
    assert any(bad["record_id"] in m and "RuntimeError" in m for m in messages)


# ---------------------------------------------------------------------------
# Recompute digest (AC-44, FR5.8)
# ---------------------------------------------------------------------------


async def test_completed_run_one_digest_alert(repo):
    await write(repo, TENANT_A, delivery("ORD-R", flag="negative"), "recompute")
    run_id = await completed_run(repo, TENANT_A, {"missing_cost": 1, "negative_margin": 1, "below_floor": 0})
    h = build_guard(repo)
    await h.guard.monitor_cycle()
    await h.guard.monitor_cycle()
    items = await alerts(repo, TENANT_A)
    assert [(a["alert_type"], a["severity"], a["dedupe_key"], a["run_id"]) for a in items] == [
        ("recompute_digest", "info", run_id, run_id)
    ]
    assert items[0]["details"]["by_flag"]["negative_margin"] == 1
    assert (await repo.get_run(TENANT_A, run_id))["digest_state"] == "done"


async def test_run_without_flags_has_no_digest(repo):
    await completed_run(repo, TENANT_A, {"missing_cost": 0, "negative_margin": 0, "below_floor": 0})
    h = build_guard(repo)
    await h.guard.monitor_cycle()
    assert await alerts(repo, TENANT_A) == []


# ---------------------------------------------------------------------------
# Alert resolution audit (N14)
# ---------------------------------------------------------------------------


async def test_acknowledge_approve_dismiss_each_audit_once(repo):
    await write(repo, TENANT_A, delivery("ORD-NEG", flag="negative", customer="CUST-9"))
    await _below_floor_deliveries(repo, TENANT_A, 3, start=1)
    await _below_floor_deliveries(repo, TENANT_A, 3, start=10, customer="CUST-2")
    h = build_guard(repo)
    await h.guard.monitor_cycle()
    service = build_service(repo, SweepStore())
    telemetry = service.telemetry
    neg = (await alerts(repo, TENANT_A, alert_type="negative_margin"))[0]
    leaks = await alerts(repo, TENANT_A, alert_type="leakage_proposal")
    assert len(leaks) == 2

    acknowledged = await service.transition_alert(TENANT_A, "admin-1", neg["alert_id"], "acknowledge", note="seen")
    approved = await service.transition_alert(TENANT_A, "admin-1", leaks[0]["alert_id"], "approve")
    dismissed = await service.transition_alert(TENANT_A, "admin-1", leaks[1]["alert_id"], "dismiss", note="ok")
    assert (acknowledged["status"], approved["status"], dismissed["status"]) == (
        "acknowledged", "approved", "dismissed"
    )
    events = [e for e in telemetry.events if e["event_type"] == "margin_alert_resolved"]
    assert [e["action"] for e in events] == ["acknowledge", "approve", "dismiss"]
    for event, alert in zip(events, [neg, *leaks]):
        assert event["resource_type"] == "margin_alert"
        assert event["resource_id"] == alert["alert_id"]
        assert event["user_id"] == "admin-1"
        details = event["details"]
        assert details["tenant_id"] == TENANT_A and details["alert_type"] == alert["alert_type"]
        assert "note" not in str(details) and "seen" not in str(details)
        assert_no_money(details)
    assert events[0]["details"]["from_status"] == "open"
    assert events[0]["details"]["to_status"] == "acknowledged"
    assert events[1]["details"]["proposal_id"] == leaks[0]["proposal_id"]
    assert "proposal_id" not in events[0]["details"]

    # Wrong state and unknown id: 409 / 404, no audit.
    with pytest.raises(AppException) as conflict:
        await service.transition_alert(TENANT_A, "admin-1", neg["alert_id"], "approve")
    assert conflict.value.status_code == 409
    with pytest.raises(AppException) as missing:
        await service.transition_alert(TENANT_B, "admin-1", neg["alert_id"], "acknowledge")
    assert missing.value.status_code == 404
    assert len([e for e in telemetry.events if e["event_type"] == "margin_alert_resolved"]) == 3


def test_margin_service_transition_alert_is_public():
    assert hasattr(margin_service_module.MarginService, "transition_alert")


# ---------------------------------------------------------------------------
# LearningPolicyAgent ignores margin.review.* (design "Testing")
# ---------------------------------------------------------------------------


async def test_learning_policy_agent_ignores_published_margin_review_proposal(repo):
    from unittest.mock import AsyncMock, MagicMock

    from Agents.overlay.learning_policy_agent import LearningPolicyAgent

    await _below_floor_deliveries(repo, TENANT_A, 3)
    h = build_guard(repo)
    _, proposals = await h.guard.monitor_cycle()
    assert h.bus.published == proposals and len(proposals) == 1

    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    es.index_document = AsyncMock()
    es.update_document = AsyncMock()
    lpa = LearningPolicyAgent(
        signal_bus=MagicMock(), es_service=es, activity_log_service=MagicMock(),
        ws_manager=MagicMock(), confirmation_protocol=MagicMock(),
        autonomy_config_service=MagicMock(), feature_flag_service=MagicMock(),
        feedback_service=MagicMock(),
    )
    lpa._log_experiment = AsyncMock()
    assert await lpa.evaluate(list(h.bus.published)) == []
    lpa._log_experiment.assert_not_called()
