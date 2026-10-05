"""Loading-plan approvals execute through the approval queue (design K7, K9, K10).

Real ``ApprovalQueueService`` + ``ConfirmationProtocol`` + ``LoadingPlanExecutor``
+ ``FuelOrderRepository`` + ``OrderService`` over ``InMemoryDocStore``
(``tests.unit._loading_plan_fakes.ApprovalHarness``), a fake activity log and a
spy WS manager. Covers T-U9 (supersede and holding rules), T-U10 (statuses,
sticky ``writes_made``, races), T-U11 (audit identity, tenant isolation),
T-U12/T-U12c (activity, WS, logging) and the approve-level T-U6 shadow cases.
Nothing here touches Redis or Postgres.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from Agents.approval_queue_service import (
    ApprovalExpiredError,
    ApprovalForbiddenError,
    LoadingPlanExecutionError,
    LoadingPlanOverlapError,
    _holds_orders,
)
from tests.unit._loading_plan_fakes import (
    APPROVALS,
    EVENTS,
    ORDERS,
    PLANS,
    ApprovalHarness,
    FakeFeatureFlagService,
    order_fixture,
)

T = "tenant-1"
SCHEDULES = [[0], [1], [2], [0, 1], [1, 0, 2], [2, 0, 0, 1], [0, 0, 3]]


def _orders(*ids: str, **statuses: str) -> List[dict]:
    out = []
    for o in ids:
        status = statuses.get(o, "confirmed")
        extra = {"hold_reason": "credit"} if status == "on_hold" else {}
        out.append(order_fixture(o, status=status, tenant_id=T, **extra))
    return out


def _actor(event: dict) -> Optional[str]:
    return event.get("actor_user_id") or (event.get("event_payload") or {}).get("actor_user_id")


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _raises(coro, exc_type=Exception):
    with pytest.raises(exc_type) as info:
        await coro
    return info.value


class ScriptedFF:
    """Flag service whose strict read follows a script, one step per call.

    A step is a mode string, ``"raise"`` or ``("wait", event, step)``; the
    last step repeats.
    """

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls = 0

    async def get_overlay_state_strict(self, flag_key: str, tenant_id: str) -> Optional[str]:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, tuple):
            _wait, event, step = step
            await event.wait()
        if step == "raise":
            raise ConnectionError("redis unavailable")
        return step


def _partial_fault(h: ApprovalHarness, order_id: str = "o2") -> None:
    """Fail the guarded transition of ``order_id`` once (its 2nd atomic_update)."""
    h.store.fail_on("atomic_update", ORDERS, order_id, nth=2, exc=RuntimeError("SECRET store text"))


# ---------------------------------------------------------------------------
# T-U9 supersede and holding rules
# ---------------------------------------------------------------------------


async def test_t_u9_applied_plan_then_disjoint_plan_applies():
    h = ApprovalHarness(_orders("o1", "o2", "o3"))
    h.add_plan("A", ["o1", "o2"])
    h.add_plan("B", ["o3"], run_id="run-later")
    assert (await h.approve("A"))["status"] == "executed"
    b = await h.approve("B")
    assert b["status"] == "executed"
    assert h.links("o3") == ("run-later", "truck-B")
    assert h.order("o3")["status"] == "scheduled"


async def test_t_u9_older_overlapping_plan_is_refused_and_stays_pending():
    h = ApprovalHarness(_orders("o1", "o2", "o3"))
    h.add_plan("C", ["o1", "o3"])  # older
    h.add_plan("A", ["o1", "o2"])
    await h.approve("A")  # expires C (pending, overlapping)
    assert h.status("C") == "expired"
    h.add_plan("D", ["o2", "o3"])
    calls = len(h.execute_calls)
    exc = await _raises(h.approve("D"), LoadingPlanOverlapError)
    assert isinstance(exc, ValueError)
    assert "conflicts with approved loading plan A" in str(exc)
    assert h.status("D") == "pending"
    assert len(h.execute_calls) == calls


@pytest.mark.parametrize(
    "status,result,blocks",
    [
        ("failed", {"attempt_id": "a0", "outcome": "failed", "writes_made": False}, False),
        ("shadowed", {"attempt_id": "a0", "outcome": "shadow", "writes_made": False}, False),
        ("incomplete", {"attempt_id": "a0", "outcome": "incomplete", "writes_made": True}, True),
        ("executed", {"attempt_id": "a0", "outcome": "applied", "success": True}, True),
        ("approved", {"attempt_id": "a0", "state": "in_progress", "claimed_at": _now()}, True),
        # Legacy shapes without execution_result.attempt_id never hold.
        ("executed", {"success": True, "result": "Unknown tool apply_loading_plan — no mutation executed"}, False),
        ("executed", {"success": True, "result": "Mutation apply_loading_plan approved but ES not wired for tenant t"}, False),
        ("approved", None, False),
    ],
)
async def test_t_u9_holding_rules(status, result, blocks):
    h = ApprovalHarness(_orders("o1", "o2"))
    other = h.add_plan("X", ["o1"], status=status)
    if result is not None:
        other["execution_result"] = result
        h.store.seed(APPROVALS, "X", other)
    h.add_plan("P", ["o1", "o2"])
    assert _holds_orders(h.entry("X")) is blocks
    if blocks:
        await _raises(h.approve("P"), LoadingPlanOverlapError)
        assert h.status("P") == "pending"
    else:
        assert (await h.approve("P"))["status"] == "executed"
        assert h.status("X") == status  # not pending, so never expired


async def test_t_u9_600_disjoint_executed_entries_do_not_hide_a_holder_sorting_last():
    h = ApprovalHarness(_orders("o1"))
    for i in range(600):
        h.add_plan(f"E{i:03d}", [f"x{i}"], status="executed", execution_result={"attempt_id": f"a{i}"})
    h.add_plan("HOLD", ["o1"], status="executed", execution_result={"attempt_id": "a-hold"})
    h.add_plan("P", ["o1"])
    await _raises(h.approve("P"), LoadingPlanOverlapError)


async def test_t_u9_paged_search_walks_past_500_entries_without_order_ids():
    """Entries without parameters.order_ids match the should clause; paging finds the holder."""
    h = ApprovalHarness(_orders("o1"))
    for i in range(600):
        e = h.add_plan(f"L{i:03d}", [f"x{i}"], status="executed", execution_result={"attempt_id": f"a{i}"})
        del e["parameters"]["order_ids"]
        h.store.seed(APPROVALS, e["action_id"], e)
    holder = h.add_plan("HOLD", ["o1"], status="incomplete", execution_result={"attempt_id": "a", "writes_made": True})
    del holder["parameters"]["order_ids"]
    h.store.seed(APPROVALS, "HOLD", holder)
    h.add_plan("P", ["o1"])
    await _raises(h.approve("P"), LoadingPlanOverlapError)
    assert len(h.store.calls("search_documents", APPROVALS)) >= 2


async def test_t_u9_entry_without_order_ids_is_found_through_assignments():
    h = ApprovalHarness(_orders("o1", "o2"))
    old = h.add_plan("OLD", ["o1"])
    del old["parameters"]["order_ids"]
    h.store.seed(APPROVALS, "OLD", old)
    await h.approve(h.add_plan("P", ["o1", "o2"])["action_id"])
    assert h.status("OLD") == "expired"
    assert h.result("OLD")["superseded_by"] == "P"


# ---------------------------------------------------------------------------
# T-U10 statuses
# ---------------------------------------------------------------------------


async def test_t_u10_executed_reapprove_returns_stored_entry_without_executor():
    h = ApprovalHarness(_orders("o1", "o2"))
    h.add_plan("A", ["o1", "o2"])
    first = await h.approve("A")
    result = first["execution_result"]
    for key in ("success", "plan_id", "run_id", "truck_id", "order_ids", "replay"):
        assert key in result
    assert result["success"] is True and result["replay"] is False
    assert result["order_ids"] == ["o1", "o2"]
    calls, mark = len(h.execute_calls), h.mark()
    again = await h.approve("A")
    assert again == h.entry("A")
    assert len(h.execute_calls) == calls
    assert h.writes_since(mark) == []


async def test_t_u10_failed_approval_refuses_approve_allows_reject_without_feedback():
    h = ApprovalHarness(_orders("o1", "o2", o2="on_hold"))
    h.add_plan("A", ["o1", "o2"])
    exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.reason == "order_not_loadable" and exc.result.writes_made is False
    assert h.status("A") == "failed"
    again = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert again.result.reason == "order_not_loadable"
    assert (await h.reject("A"))["status"] == "rejected"
    h.feedback.record_rejection.assert_not_awaited()


async def test_t_u10_incomplete_with_writes_retries_to_completion_and_refuses_reject():
    h = ApprovalHarness(_orders("o1", "o2", "o3"))
    h.add_plan("A", ["o1", "o2", "o3"])
    _partial_fault(h)
    exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.outcome == "incomplete" and "SECRET" not in exc.result.message
    assert h.status("A") == "incomplete" and h.result("A")["writes_made"] is True
    await _raises(h.reject("A"), RuntimeError)
    done = await h.approve("A")
    assert done["status"] == "executed"
    assert all(h.order(o)["status"] == "scheduled" for o in ("o1", "o2", "o3"))


async def test_t_u10_committed_then_raised_claim_released_keeps_approval_unwritten():
    # Review pass 2 finding 1: the claim commits, the call raises, the
    # release clears it. Nothing is linked, so the holding entry stays
    # rejectable and the order is free for the next run.
    h = ApprovalHarness(_orders("o1", "o2"))
    h.add_plan("A", ["o1", "o2"])
    h.store.fail_on("atomic_update", ORDERS, "o1", nth=1, after_commit=True)
    exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.outcome == "failed" and exc.result.writes_made is False
    assert h.links("o1") == (None, None) and h.links("o2") == (None, None)
    assert h.result("A")["writes_made"] is False
    assert h.plan("plan-A")["execution_status"] == "failed"
    assert (await h.reject("A"))["status"] == "rejected"


async def test_t_u10_committed_claim_with_failed_release_refuses_reject_then_retries():
    h = ApprovalHarness(_orders("o1", "o2"))
    h.add_plan("A", ["o1", "o2"])
    h.store.fail_on("atomic_update", ORDERS, "o1", nth=1, after_commit=True)
    h.store.fail_on("atomic_update", ORDERS, "o1", nth=2)  # the release
    exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.outcome == "incomplete" and exc.result.writes_made is True
    assert h.status("A") == "incomplete" and h.result("A")["writes_made"] is True
    assert h.links("o1") == ("run-A", "truck-A")
    assert h.plan("plan-A")["execution_status"] == "incomplete"
    await _raises(h.reject("A"), RuntimeError)
    assert h.status("A") == "incomplete"
    done = await h.approve("A")
    assert done["status"] == "executed"
    assert all(h.order(o)["status"] == "scheduled" for o in ("o1", "o2"))


async def test_t_u10_fresh_approved_entry_is_in_progress_with_no_writes():
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"], status="approved", reviewed_by="user-0",
               execution_result={"state": "in_progress", "attempt_id": "a0", "claimed_at": _now(), "writes_made": False})
    mark = h.mark()
    exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.reason == "execution_in_progress" and exc.result.retryable is True
    assert h.writes_since(mark) == [] and h.execute_calls == []


async def test_t_u10_stale_approved_entry_is_reclaimed():
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"], status="approved", reviewed_by="user-0", reviewed_at=_ago(400),
               execution_result={"state": "in_progress", "attempt_id": "a0", "claimed_at": _ago(300), "writes_made": False})
    done = await h.approve("A", user="user-2")
    assert done["status"] == "executed"
    assert done["reviewed_by"] == "user-0"
    assert done["execution_result"]["attempt_id"] != "a0"
    assert done["execution_result"]["actor_user_id"] == "user-2"


async def test_t_u10_unparseable_claimed_at_counts_as_stale(caplog):
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"], status="approved", reviewed_by="user-0",
               execution_result={"state": "in_progress", "attempt_id": "a0", "claimed_at": "garbage"})
    with caplog.at_level(logging.WARNING, logger="Agents.approval_queue_service"):
        assert (await h.approve("A"))["status"] == "executed"
    assert any("no parseable claimed_at" in r.getMessage() for r in caplog.records)


async def test_t_u10_legacy_approved_entry_refuses_and_can_be_rejected():
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"], status="approved", reviewed_by="user-0")
    exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.reason == "legacy_approval" and exc.result.retryable is False
    assert h.execute_calls == []
    assert (await h.reject("A"))["status"] == "rejected"


async def test_t_u10_expired_but_pending_is_marked_expired_without_executor():
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"], expiry_time=_ago(60))
    await _raises(h.approve("A"), ApprovalExpiredError)
    assert h.status("A") == "expired"
    assert h.result("A") == {"reason": "expired_before_approval"}
    assert len(h.ws.of("approval_expired")) == 1
    assert len(h.activity.of("approval_expired")) == 1
    assert h.execute_calls == [] and h.links("o1") == (None, None)


async def test_t_u10_unwired_executor_leaves_entry_pending_then_applies_once_wired(caplog):
    h = ApprovalHarness(_orders("o1", "o2"), wired=False)
    h.add_plan("Q", ["o1"])  # overlapping pending competitor
    h.add_plan("A", ["o1", "o2"])
    with caplog.at_level(logging.ERROR, logger="Agents.approval_queue_service"):
        exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.reason == "executor_unavailable" and exc.result.retryable is True
    assert any(r.levelno == logging.ERROR and "not wired" in r.getMessage() for r in caplog.records)
    entry = h.entry("A")
    assert entry["status"] == "pending" and entry["reviewed_by"] is None
    assert entry["execution_result"]["reason"] == "executor_unavailable"
    assert entry["execution_result"]["outcome"] == "failed"
    assert not _holds_orders(entry)
    assert h.status("Q") == "pending"  # guard not run
    assert len(h.activity.of("loading_plan_execution")) == 1
    assert h.ws.types() == ["approval_execution_updated"]
    h.protocol.set_loading_plan_executor(h.executor)
    assert (await h.approve("A"))["status"] == "executed"


async def test_t_u10_retry_with_unreadable_mode_keeps_incomplete_and_sticky_writes():
    ff = ScriptedFF("active_gated", "raise")
    h = ApprovalHarness(_orders("o1", "o2"), ff=ff)
    h.add_plan("A", ["o1", "o2"])
    _partial_fault(h)
    await _raises(h.approve("A"), LoadingPlanExecutionError)
    attempt = h.result("A")["attempt_id"]
    exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.reason == "mode_unavailable"
    r = h.result("A")
    assert h.status("A") == "incomplete"
    assert r["reason"] == "mode_unavailable" and r["writes_made"] is True and r["attempt_id"] == attempt
    assert _holds_orders(h.entry("A"))
    await _raises(h.reject("A"), RuntimeError)


async def test_t_u10_retry_with_executor_unwired_keeps_incomplete_and_sticky_writes():
    h = ApprovalHarness(_orders("o1", "o2"))
    h.add_plan("A", ["o1", "o2"])
    _partial_fault(h)
    await _raises(h.approve("A"), LoadingPlanExecutionError)
    attempt = h.result("A")["attempt_id"]
    h.protocol.set_loading_plan_executor(None)
    exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.reason == "executor_unavailable"
    r = h.result("A")
    assert h.status("A") == "incomplete" and r["writes_made"] is True and r["attempt_id"] == attempt
    await _raises(h.reject("A"), RuntimeError)


async def test_t_u10_reclaim_racing_a_live_attempt(caplog):
    h = ApprovalHarness(_orders("o1", "o2"), lock_timeout=0.2)
    h.add_plan("A", ["o1", "o2"])
    paused, release = asyncio.Event(), asyncio.Event()

    async def pause_after_plan_claim(op, index, doc_id):
        if (op, index, doc_id) == ("atomic_update", ORDERS, "o1") and not paused.is_set():
            paused.set()
            await release.wait()

    h.store.hooks.append(pause_after_plan_claim)
    original = asyncio.create_task(h.approve("A", user="user-1"))
    await paused.wait()
    # Age the approval's claim past the lease, then reclaim.
    entry = h.entry("A")
    entry["execution_result"]["claimed_at"] = _ago(300)
    h.store.seed(APPROVALS, "A", entry)
    exc = await _raises(h.approve("A", user="user-2"), LoadingPlanExecutionError)
    assert exc.result.outcome == "in_progress"
    assert h.status("A") == "incomplete" and h.result("A")["writes_made"] is True
    await _raises(h.reject("A"), RuntimeError)
    release.set()
    with caplog.at_level(logging.WARNING, logger="Agents.approval_queue_service"):
        await _raises(original, LoadingPlanExecutionError)
    assert any("lost" in r.getMessage() for r in caplog.records)
    assert h.status("A") == "incomplete"  # the reclaimer's record stands


async def test_t_u10_reclaimed_attempt_with_authoritative_failed_plan_is_dismissable():
    h = ApprovalHarness(_orders("o1", "o2", o2="on_hold"))
    h.add_plan("A", ["o1", "o2"], status="approved", reviewed_by="user-0",
               plan_overrides={"execution_status": "failed", "execution_attempt_id": "pa0"},
               execution_result={"state": "in_progress", "attempt_id": "a0", "claimed_at": _ago(300), "writes_made": False})
    exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.outcome == "failed" and exc.result.writes_made is False
    assert h.status("A") == "failed"
    assert (await h.reject("A"))["status"] == "rejected"


async def test_t_u10_concurrent_retry_vs_unresolvable_mode_keeps_newer_record():
    gate = asyncio.Event()
    ff = ScriptedFF(("wait", gate, "raise"), "active_gated")
    h = ApprovalHarness(_orders("o1", "o2"), ff=ff)
    h.add_plan("A", ["o1", "o2"], status="incomplete", reviewed_by="user-0",
               execution_result={"attempt_id": "a0", "outcome": "incomplete", "writes_made": False})
    _partial_fault(h)
    r2 = asyncio.create_task(h.approve("A", user="user-2"))
    await asyncio.sleep(0)
    while ff.calls < 1:
        await asyncio.sleep(0)
    await _raises(h.approve("A", user="user-1"), LoadingPlanExecutionError)  # R1
    a1 = h.result("A")["attempt_id"]
    assert a1 != "a0" and h.result("A")["writes_made"] is True
    logged, broadcast = len(h.activity.entries), len(h.ws.events)
    gate.set()
    await _raises(r2, RuntimeError)
    assert len(h.activity.entries) == logged and len(h.ws.events) == broadcast
    assert h.result("A")["attempt_id"] == a1 and h.result("A")["writes_made"] is True
    await _raises(h.reject("A"), RuntimeError)


async def test_t_u10_concurrent_retry_resolving_late_is_a_noop_cas():
    gate = asyncio.Event()
    ff = ScriptedFF(("wait", gate, "active_gated"), "active_gated")
    h = ApprovalHarness(_orders("o1", "o2"), ff=ff)
    h.add_plan("A", ["o1", "o2"], status="incomplete", reviewed_by="user-0",
               execution_result={"attempt_id": "a0", "outcome": "incomplete", "writes_made": False})
    _partial_fault(h)
    r2 = asyncio.create_task(h.approve("A", user="user-2"))
    while ff.calls < 1:
        await asyncio.sleep(0)
    await _raises(h.approve("A", user="user-1"), LoadingPlanExecutionError)
    a1 = h.result("A")["attempt_id"]
    calls = len(h.execute_calls)
    gate.set()
    await _raises(r2, RuntimeError)
    assert len(h.execute_calls) == calls
    assert h.result("A")["attempt_id"] == a1


async def test_t_u10_record_reread_finds_rejected_entry(caplog):
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"])

    def reject_underneath(op, index, doc_id):
        if (op, index) == ("atomic_update", ORDERS) and h.status("A") == "approved":
            h.store.poke(APPROVALS, "A", status="rejected")

    h.store.hooks.append(reject_underneath)
    with caplog.at_level(logging.ERROR, logger="Agents.approval_queue_service"):
        exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.reason == "approval_released_during_execution"
    assert exc.result.retryable is False
    assert any("released while attempt" in r.getMessage() for r in caplog.records)


async def test_t_u10_record_race_lost_to_a_reclaim_keeps_reclaimer_result(caplog):
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"])

    def reclaim_underneath(op, index, doc_id):
        r = h.result("A")
        if (op, index) == ("atomic_update", ORDERS) and r.get("attempt_id") not in (None, "other"):
            h.store.poke(APPROVALS, "A", execution_result={**r, "attempt_id": "other"})

    h.store.hooks.append(reclaim_underneath)
    with caplog.at_level(logging.WARNING, logger="Agents.approval_queue_service"):
        exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.reason == "execution_in_progress"
    assert h.status("A") == "approved" and h.result("A")["attempt_id"] == "other"
    assert any("lost" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("schedule", SCHEDULES)
async def test_t_u10_sweeper_race_never_expires_an_approved_entry(schedule):
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"])
    real_search = h.store.search_documents

    async def search(index, query, size=10):
        if "range" in str(query):  # the sweeper sees A as already past expiry
            await real_search(index, query, size)
            return {"hits": {"hits": [{"_source": h.entry("A")}], "total": {"value": 1}}}
        return await real_search(index, query, size)

    h.store.search_documents = search
    h.store.set_yield_schedule(schedule, repeat=True)
    approved, swept = await asyncio.gather(
        h.approve("A"), h.svc.expire_stale(), return_exceptions=True
    )
    h.store.set_yield_schedule(None)
    status = h.status("A")
    if isinstance(approved, dict):
        assert status == "executed" and swept == 0
        assert h.ws.of("approval_expired") == []
    else:
        assert status == "expired" and swept == 1
        assert h.execute_calls == []


async def test_t_u10_stale_approved_listing_reclaim_and_reject():
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"], status="approved", reviewed_by="user-0",
               execution_result={"state": "in_progress", "attempt_id": "a0", "claimed_at": _ago(300), "writes_made": False})
    other = h.add_plan("J", [], status="approved")
    other["tool_name"] = "cancel_job"
    h.store.seed(APPROVALS, "J", other)
    listed = await h.svc.list_pending(T, include_unresolved=True)
    ids = [e["action_id"] for e in listed["items"]]
    assert "A" in ids and "J" not in ids
    assert "A" not in [e["action_id"] for e in (await h.svc.list_pending(T))["items"]]
    await _raises(h.reject("A"), RuntimeError)
    assert (await h.approve("A"))["status"] == "executed"


async def test_t_u10_reject_of_incomplete_refused_when_writes_flip_under_the_lock():
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"], status="incomplete",
               execution_result={"attempt_id": "a0", "outcome": "incomplete", "writes_made": False})

    def flip(op, index, doc_id):
        if (op, index, doc_id) == ("atomic_update", APPROVALS, "A"):
            h.store.poke(APPROVALS, "A", execution_result={"attempt_id": "a1", "writes_made": True})

    h.store.hooks.append(flip)
    await _raises(h.reject("A"), RuntimeError)
    assert h.status("A") == "incomplete"


# ---------------------------------------------------------------------------
# T-U11 audit and identity
# ---------------------------------------------------------------------------


async def test_t_u11_actor_is_the_session_user_not_reviewer_id():
    h = ApprovalHarness(_orders("o1", "o2"))
    h.add_plan("A", ["o1", "o2"])
    done = await h.approve("A", user="session-user", reviewer_id="spoofed")
    assert done["reviewed_by"] == "session-user"
    assert h.plan("plan-A")["applied_by"] == "session-user"
    for oid in ("o1", "o2"):
        events = h.order_events(oid)
        assert len(events) == 2  # order_assigned + order_scheduled
        assert {_actor(e) for e in events} == {"session-user"}
    (entry,) = h.activity.of("loading_plan_execution")
    assert entry["user_id"] == "session-user"
    (approved,) = h.activity.of("approval_approved")
    assert approved["user_id"] == "session-user"


async def test_t_u11_no_session_user_is_forbidden_with_no_writes():
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"])
    mark = h.mark()
    await _raises(h.approve("A", user=None, reviewer_id="someone"), ApprovalForbiddenError)
    assert h.writes_since(mark) == [] and h.status("A") == "pending"


async def test_t_u11_cross_tenant_approve_and_reject_look_missing():
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"])
    mark = h.mark()
    missing = await _raises(h.approve("NOPE", tenant_id="tenant-2"), ValueError)
    foreign = await _raises(h.approve("A", tenant_id="tenant-2"), ValueError)
    assert str(foreign) == str(missing).replace("NOPE", "A")
    foreign_reject = await _raises(h.reject("A", tenant_id="tenant-2"), ValueError)
    assert str(foreign_reject) == "Approval entry A not found"
    assert h.writes_since(mark) == [] and h.status("A") == "pending"


async def test_t_u11_retry_by_another_user_keeps_reviewed_by():
    h = ApprovalHarness(_orders("o1", "o2"))
    h.add_plan("A", ["o1", "o2"])
    _partial_fault(h)
    await _raises(h.approve("A", user="user-A"), LoadingPlanExecutionError)
    before = {e["event_id"] for e in h.store.events()}
    done = await h.approve("A", user="user-B")
    assert done["reviewed_by"] == "user-A"
    assert done["execution_result"]["actor_user_id"] == "user-B"
    assert h.plan("plan-A")["applied_by"] == "user-B"
    retry_events = [e for e in h.store.events() if e["event_id"] not in before]
    assert retry_events and {_actor(e) for e in retry_events} == {"user-B"}
    users = [e["user_id"] for e in h.activity.of("loading_plan_execution")]
    assert users == ["user-A", "user-B"]


# ---------------------------------------------------------------------------
# T-U12 activity and WS; T-U12c logging
# ---------------------------------------------------------------------------


async def test_t_u12_one_execution_entry_per_invocation_with_stale_reason():
    h = ApprovalHarness(_orders("o1", "o2"))
    entry = h.add_plan("A", ["o1", "o2"])
    entry["parameters"]["order_snapshots"] = {"o2": {"gallons_requested": 999.0}}
    h.store.seed(APPROVALS, "A", entry)
    exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    assert exc.result.reason == "order_changed_since_plan"
    assert h.result("A")["reason"] == "order_changed_since_plan"
    (logged,) = h.activity.of("loading_plan_execution")
    assert logged["outcome"] == "failed"
    assert logged["details"]["reason"] == "order_changed_since_plan"
    assert logged["details"]["failures"][0]["order_id"] == "o2"
    assert logged["details"]["action_id"] == "A"
    (approved,) = h.activity.of("approval_approved")
    assert approved["details"]["execution_success"] is False
    assert approved["details"]["executed"] is False


async def test_t_u12_outcomes_map_to_activity_and_ws_carries_execution_result():
    h = ApprovalHarness(_orders("o1", "o2", "o3"))
    h.add_plan("A", ["o1", "o2"])
    _partial_fault(h)
    await _raises(h.approve("A"), LoadingPlanExecutionError)
    await h.approve("A")
    await h.approve("A")  # executed: short-circuit, no entry
    outcomes = [e["outcome"] for e in h.activity.of("loading_plan_execution")]
    assert outcomes == ["incomplete", "executed"]
    updates = h.ws.of("approval_execution_updated")
    assert [u["status"] for u in updates] == ["incomplete", "executed"]
    assert all("execution_result" in u and u["tenant_id"] == T for u in updates)
    assert h.ws.types()[0] == "approval_approved"
    (approved,) = h.activity.of("approval_approved")  # first approval only
    assert approved["details"]["execution_success"] is False


async def test_t_u12_shadow_execution_success_is_false():
    h = ApprovalHarness(_orders("o1"), ff=FakeFeatureFlagService("shadow"))
    h.add_plan("A", ["o1"])
    done = await h.approve("A")
    assert done["status"] == "shadowed"
    (approved,) = h.activity.of("approval_approved")
    assert approved["details"] == {"action_id": "A", "executed": False, "execution_success": False}
    assert h.activity.of("loading_plan_execution")[0]["outcome"] == "shadow"


async def test_t_u12_activity_log_failure_does_not_change_the_response():
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"])
    h.activity.fail = True
    done = await h.approve("A")
    assert done["status"] == "executed"
    assert h.ws.types() == ["approval_approved", "approval_execution_updated"]


async def test_t_u12c_logging(caplog):
    h = ApprovalHarness(_orders("o1", "o2", "o3", o3="on_hold"))
    h.add_plan("A", ["o1", "o2"])
    h.add_plan("B", ["o3"])
    with caplog.at_level(logging.INFO):
        await h.approve("A")
        await _raises(h.approve("B"), LoadingPlanExecutionError)
    msgs = [(r.name, r.levelno, r.getMessage()) for r in caplog.records]
    starts = [m for n, l, m in msgs if n == "fuel.services.loading_plan_executor" and "loading plan: start" in m]
    finishes = [m for n, l, m in msgs if n == "fuel.services.loading_plan_executor" and "loading plan: finish" in m]
    assert any("plan=plan-A" in s and f"tenant={T}" in s and "attempt=" in s for s in starts)
    assert any("plan=plan-A" in f and "applied=2" in f and "skipped=0" in f for f in finishes)
    refusals = [m for n, l, m in msgs if l == logging.WARNING and "preflight refused" in m]
    assert refusals and "o3" in refusals[0] and "order_not_loadable" in refusals[0]


async def test_t_u12c_unexpected_append_event_error_is_logged_with_ids_not_returned(caplog):
    h = ApprovalHarness(_orders("o1"))
    h.add_plan("A", ["o1"])
    h.store.fail_on("index_document", EVENTS, exc=RuntimeError("SECRET-db-text"))
    with caplog.at_level(logging.INFO):
        exc = await _raises(h.approve("A"), LoadingPlanExecutionError)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
    assert errors
    text = errors[0].getMessage()
    assert f"tenant={T}" in text and "plan=plan-A" in text and "action=A" in text and "attempt=" in text
    assert "SECRET" not in exc.result.message and "SECRET" not in str(exc)
    assert "SECRET" not in str(h.result("A"))


# ---------------------------------------------------------------------------
# T-U6 approve-level shadow and mode cases
# ---------------------------------------------------------------------------


async def test_t_u6_shadow_approval_leaves_overlapping_pending_plan_untouched():
    h = ApprovalHarness(_orders("o1", "o2"), ff=FakeFeatureFlagService("shadow"))
    h.add_plan("Q", ["o1"])
    h.add_plan("P", ["o1", "o2"])
    mark = h.mark()
    assert (await h.approve("P"))["status"] == "shadowed"
    q = h.entry("Q")
    assert q["status"] == "pending" and "superseded_by" not in (q.get("execution_result") or {})
    assert h.writes_since(mark, ORDERS, EVENTS, PLANS) == []


async def test_t_u6_unresolvable_mode_leaves_entry_pending_without_hold():
    h = ApprovalHarness(_orders("o1", "o2"), ff=FakeFeatureFlagService(raises=ConnectionError("down")))
    h.add_plan("Q", ["o1"])
    h.add_plan("P", ["o1", "o2"])
    exc = await _raises(h.approve("P"), LoadingPlanExecutionError)
    assert exc.result.reason == "mode_unavailable" and exc.result.retryable is True
    p = h.entry("P")
    assert p["status"] == "pending" and p["execution_result"]["reason"] == "mode_unavailable"
    assert not _holds_orders(p) and h.status("Q") == "pending"
    assert len(h.activity.of("loading_plan_execution")) == 1
    assert h.ws.types() == ["approval_execution_updated"]
    assert h.execute_calls == []


@pytest.mark.parametrize("schedule", SCHEDULES)
async def test_t_u6_concurrent_overlap_with_unresolvable_mode(schedule):
    ff = FakeFeatureFlagService(raises=ConnectionError("down"))
    h = ApprovalHarness(_orders("o1", "o2", "o3"), ff=ff)
    h.add_plan("P", ["o1", "o2"])
    h.add_plan("Q", ["o2", "o3"])
    h.store.set_yield_schedule(schedule, repeat=True)
    results = await asyncio.gather(h.approve("P"), h.approve("Q"), return_exceptions=True)
    h.store.set_yield_schedule(None)
    assert all(isinstance(r, (LoadingPlanExecutionError, RuntimeError)) for r in results)
    for a in ("P", "Q"):
        assert h.status(a) == "pending" and not _holds_orders(h.entry(a))
        assert not h.entry(a).get("reviewed_by")
    assert h.execute_calls == []
    ff.raises = None
    approved = []
    for a in ("P", "Q"):
        try:
            approved.append((await h.approve(a))["status"])
        except ValueError:
            pass
    assert approved == ["executed"]


async def test_t_u6_flip_after_resolve_uses_the_resolved_mode():
    h = ApprovalHarness(_orders("o1"), ff=FakeFeatureFlagService(modes=["shadow", "active_gated"]))
    h.add_plan("P", ["o1"])
    mark = h.mark()
    assert (await h.approve("P"))["status"] == "shadowed"
    assert h.execute_calls[0]["mode"] == "shadow"
    assert h.writes_since(mark, ORDERS, EVENTS, PLANS) == []


async def test_t_u6_two_concurrent_shadow_approvals_write_nothing():
    h = ApprovalHarness(_orders("o1", "o2"), ff=FakeFeatureFlagService("shadow"))
    h.add_plan("P", ["o1", "o2"])
    h.add_plan("Q", ["o2"])
    mark = h.mark()
    p, q = await asyncio.gather(h.approve("P"), h.approve("Q"))
    assert p["status"] == q["status"] == "shadowed"
    assert h.writes_since(mark, ORDERS, EVENTS, PLANS) == []


@pytest.mark.parametrize("schedule", SCHEDULES)
async def test_t_u6_interleaved_shadow_approvals_write_nothing(schedule):
    """While one shadow attempt is in progress its approved entry holds (K7), so
    an interleaved overlapping approve may be refused instead; nothing is written."""
    h = ApprovalHarness(_orders("o1", "o2"), ff=FakeFeatureFlagService("shadow"))
    h.add_plan("P", ["o1", "o2"])
    h.add_plan("Q", ["o2"])
    mark = h.mark()
    h.store.set_yield_schedule(schedule, repeat=True)
    results = await asyncio.gather(h.approve("P"), h.approve("Q"), return_exceptions=True)
    h.store.set_yield_schedule(None)
    for action_id, r in zip(("P", "Q"), results):
        if isinstance(r, BaseException):
            assert isinstance(r, (LoadingPlanOverlapError, RuntimeError)), r
            assert h.status(action_id) == "pending"
        else:
            assert r["status"] == "shadowed"
    assert any(not isinstance(r, BaseException) for r in results)
    assert h.writes_since(mark, ORDERS, EVENTS, PLANS) == []


async def test_t_u6_partial_then_shadow_then_active():
    ff = FakeFeatureFlagService("active_gated")
    h = ApprovalHarness(_orders("o1", "o2", "o3"), ff=ff)
    h.add_plan("P", ["o1", "o2"])
    _partial_fault(h)
    await _raises(h.approve("P"), LoadingPlanExecutionError)
    ff.mode = "shadow"
    exc = await _raises(h.approve("P"), LoadingPlanExecutionError)
    assert exc.result.reason == "shadow_mode"
    r = h.result("P")
    assert h.status("P") == "incomplete" and r["reason"] == "shadow_mode" and r["writes_made"] is True
    h.add_plan("R", ["o2", "o3"])
    await _raises(h.approve("R"), LoadingPlanOverlapError)
    ff.mode = "active_gated"
    assert (await h.approve("P"))["status"] == "executed"
