"""Agent suggestions on the board (plan task 20; design K9; R16.1-R16.5, R16.7, E7, E16).

* Read: only while ``overlay.compartment_loading`` is ``active_gated`` or
  ``active_auto`` (shadow hidden, Q6); board plans (``source=dispatch_board``)
  are never suggestions; one plan per truck (the newest) for the day; route
  order only while ``overlay.route_planning`` is active; pending
  ``apply_loading_plan`` approvals joined by ``parameters.plan_id``.
* Why text and the ``ReplanDiff`` diff producer.
* ``accept_suggestion`` through the normal command path (whole, subset),
  never approving the approval entry (R16.7).
* Dismissals and the reject endpoint calling ``ApprovalQueueService.reject``.
* E7: accepted on the board, then approved in the queue, blocks with
  ``order_committed_elsewhere``.
"""
from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Agents.approval_queue_service import ApprovalExpiredError
from Agents.support.replan_diff_models import ReplanDiff
from errors.codes import ErrorCode
from errors.exceptions import AppException
from errors.handlers import register_exception_handlers
from fuel.api import dispatch_board_endpoints as board_api
from fuel.services.dispatch_board_engine import GAL_TO_L
from fuel.services.dispatch_board_models import Lane, Load, OrderSnapshot, Stop
from fuel.services.dispatch_board_suggestions import (
    COMPARTMENT_FLAG,
    ROUTE_FLAG,
    BoardSuggestionService,
    diff,
    suggested_order_ids,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.unit._dispatch_board_fakes import NOW, ORDERS, OTHER, PRIORITIES, T, TODAY, TOMORROW, TZ, Harness

PLANS = "mvp_load_plans"
ROUTES = "mvp_routes"
FORECASTS = "mvp_tank_forecasts"


class Flags:
    """Overlay states keyed by ``(flag_key, tenant_id)``; unset reads ``disabled``."""

    def __init__(self, states: Optional[Dict[str, str]] = None, *, board: str = "active_gated") -> None:
        self.states = dict(states or {})
        self.board = board

    async def get_overlay_state(self, flag_key, tenant_id):
        if flag_key == "dispatch_board":
            return self.board
        return self.states.get(flag_key, "disabled")

    async def get_overlay_state_strict(self, flag_key, tenant_id):
        return await self.get_overlay_state(flag_key, tenant_id)


class Approvals:
    def __init__(self, items: Optional[List[Dict[str, Any]]] = None) -> None:
        self.items = items or []
        self.rejects: List[tuple] = []
        self.approves: List[Any] = []
        self.reject_error: Optional[BaseException] = None

    async def list_pending(self, tenant_id, page=1, size=20, **kwargs):
        return {"items": [copy.deepcopy(i) for i in self.items if i["tenant_id"] == tenant_id], "total": len(self.items)}

    async def reject(self, action_id, reviewer_id, reason="", *, tenant_id=None):
        self.rejects.append((action_id, reviewer_id, reason, tenant_id))
        if self.reject_error is not None:
            raise self.reject_error
        return {"action_id": action_id, "status": "rejected"}

    async def approve(self, *args, **kwargs):  # pragma: no cover - must never run (R16.7)
        self.approves.append((args, kwargs))


ACTIVE = {COMPARTMENT_FLAG: "active_gated", ROUTE_FLAG: "active_gated"}


def plan_doc(plan_id: str, truck_id: str, order_ids: List[str], *, tenant_id: str = T, created=NOW, **extra: Any) -> Dict[str, Any]:
    doc = {
        "plan_id": plan_id,
        "tenant_id": tenant_id,
        "truck_id": truck_id,
        "status": "proposed",
        "run_id": f"run-{plan_id}",
        "terminal_id": None,
        "created_at": created.isoformat(),
        "total_utilization_pct": 80.0,
        "assignments": [
            {"compartment_id": f"C{i + 1}", "order_id": o, "station_id": f"cust-{o}", "fuel_grade": "AGO", "quantity_liters": round(450 * GAL_TO_L, 2)}
            for i, o in enumerate(order_ids)
        ],
    }
    doc.update(extra)
    return doc


def route_doc(plan_id: str, truck_id: str, order_ids: List[str], *, tenant_id: str = T) -> Dict[str, Any]:
    return {
        "route_id": f"route-{plan_id}",
        "plan_id": plan_id,
        "tenant_id": tenant_id,
        "truck_id": truck_id,
        "timestamp": NOW.isoformat(),
        "stops": [
            {"station_id": f"cust-{o}", "order_ids": [o], "sequence": i + 1, "eta": (NOW + timedelta(hours=i + 1)).isoformat()}
            for i, o in enumerate(order_ids)
        ],
    }


class World:
    def __init__(self, states: Optional[Dict[str, str]] = None) -> None:
        self.h = Harness()
        self.flags = Flags(ACTIVE if states is None else states)
        self.approvals = Approvals(
            [{"action_id": "act-1", "tool_name": "apply_loading_plan", "status": "pending", "tenant_id": T, "parameters": {"plan_id": "plan-1"}}]
        )
        self.service = BoardSuggestionService(
            es_service=self.h.store, feature_flags=self.flags, board_service=self.h.service, approval_queue=self.approvals
        )
        self.h.service.set_suggestion_reader(self.service)
        for oid in ("o1", "o2", "o3", "o4"):
            self.h.seed_order(oid)
        # The agent's plan for T1: assignments o1, o2; the route visits o2 first.
        self.h.store.seed(PLANS, "plan-1", plan_doc("plan-1", "T1", ["o1", "o2"]))
        self.h.store.seed(ROUTES, "route-plan-1", route_doc("plan-1", "T1", ["o2", "o1"]))
        self.h.store.seed(PRIORITIES, "prio-1", {
            "tenant_id": T, "created_at": NOW.isoformat(),
            "priorities": [{"order_id": "o1", "priority_score": 87.0, "priority_bucket": "critical"}],
        })
        self.h.store.seed(FORECASTS, "fc-1", {"tenant_id": T, "customer_tank_id": "tank-o1", "hours_to_runout_p50": 9.4, "created_at": NOW.isoformat()})

    async def snapshot(self, day=TODAY) -> Dict[str, Any]:
        return await self.h.service.snapshot(T, day, mode="active_gated", tz=TZ)

    async def suggestions(self, day=TODAY) -> List[Dict[str, Any]]:
        return (await self.snapshot(day))["suggestions"]


# ---------------------------------------------------------------------------
# Read and gating (R16.1, R16.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["shadow", "disabled", None])
async def test_shadow_disabled_or_unset_compartment_mode_hides_suggestions(mode):
    states = {ROUTE_FLAG: "active_gated"}
    if mode is not None:
        states[COMPARTMENT_FLAG] = mode
    w = World(states)
    assert await w.suggestions() == []
    assert await w.service.for_context(T, TODAY, tz=TZ) == {}


@pytest.mark.parametrize("mode", ["active_gated", "active_auto"])
async def test_active_modes_show_the_agent_plan_with_route_order_and_approval(mode):
    w = World({COMPARTMENT_FLAG: mode, ROUTE_FLAG: mode})
    (s,) = await w.suggestions()
    assert s["suggestion_id"] == s["plan_id"] == "plan-1" and s["truck_id"] == "T1"
    assert s["loads"] == [{"load_key": "plan-1", "truck_id": "T1", "terminal_id": None, "order_ids": ["o2", "o1"]}]
    assert s["approval_action_id"] == "act-1" and s["route_id"] == "route-plan-1"
    assert s["agent_name"] == "Compartment loading agent" and s["route_agent_name"] == "Route planning agent"


async def test_route_order_needs_the_route_planning_mode():
    w = World({COMPARTMENT_FLAG: "active_gated", ROUTE_FLAG: "shadow"})
    (s,) = await w.suggestions()
    assert s["loads"][0]["order_ids"] == ["o1", "o2"] and s["route_id"] is None and s["route_agent_name"] is None


async def test_board_plans_are_never_suggestions():
    w = World()
    w.h.store.seed(PLANS, "bp-L9-r1", plan_doc("bp-L9-r1", "T2", ["o3"], source="dispatch_board", status="draft"))
    w.h.store.seed(ROUTES, "route-bp", {**route_doc("plan-1", "T1", ["o4"]), "route_id": "route-bp", "source": "dispatch_board", "timestamp": (NOW + timedelta(hours=1)).isoformat()})
    suggestions = await w.suggestions()
    assert [s["plan_id"] for s in suggestions] == ["plan-1"]
    assert suggestions[0]["route_id"] == "route-plan-1"  # the newer board route is ignored
    assert "bp-L9-r1" not in await w.service.for_context(T, TODAY, tz=TZ)


async def test_one_plan_per_truck_for_the_day_on_a_tenant_truck():
    w = World()
    w.h.store.seed(PLANS, "plan-old", plan_doc("plan-old", "T1", ["o3"], created=NOW - timedelta(hours=2)))
    w.h.store.seed(PLANS, "plan-yday", plan_doc("plan-yday", "T2", ["o3"], created=NOW - timedelta(days=1)))
    w.h.store.seed(PLANS, "plan-tmrw", plan_doc("plan-tmrw", "T3", ["o4"], service_date=TOMORROW.isoformat()))
    w.h.store.seed(PLANS, "plan-sched", plan_doc("plan-sched", "T2", ["o3"], status="scheduled"))
    w.h.store.seed(PLANS, "plan-foreign-truck", plan_doc("plan-foreign-truck", "T9", ["o3"]))
    w.h.store.seed(PLANS, "plan-other-tenant", plan_doc("plan-other-tenant", "T2", ["o3"], tenant_id=OTHER))
    assert [s["plan_id"] for s in await w.suggestions()] == ["plan-1"]
    assert [s["plan_id"] for s in await w.suggestions(TOMORROW)] == ["plan-tmrw"]


async def test_past_days_and_lane_reads_skip_suggestions():
    w = World()
    assert (await w.h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ, lanes=[]))["suggestions"] == []
    assert await w.suggestions(TODAY - timedelta(days=1)) == []


async def test_a_read_failure_degrades_the_snapshot_instead_of_failing_it():
    w = World()

    async def broken(*args, **kwargs):
        raise ConnectionError("down")

    w.service.list_for_day = broken  # type: ignore[method-assign]
    snap = await w.snapshot()
    assert snap["suggestions"] == [] and "suggestions" in snap["degraded_sources"]


# ---------------------------------------------------------------------------
# Why and diff (R16.2)
# ---------------------------------------------------------------------------


async def test_why_text_has_priority_runout_and_fill():
    w = World()
    (s,) = await w.suggestions()
    why = s["why"]
    assert why["text"] == "Priority critical (87). Tank runs out in 9 h. Fills 80% of the truck."
    assert (why["priority_bucket"], why["priority_score"], why["runout_hours"], why["fill_pct"]) == ("critical", 87.0, 9.4, 80.0)


async def test_why_falls_back_to_the_window_without_a_forecast_or_priority():
    w = World()
    w.h.store.remove(FORECASTS, "fc-1")
    w.h.store.remove(PRIORITIES, "prio-1")
    (s,) = await w.suggestions()
    # Orders' windows end 23:00 UTC = 18:00 in Chicago.
    assert s["why"]["text"] == "Meets the window ending 18:00. Fills 80% of the truck."


def _stop(order_id: str, *, gallons: float = 500.0, eta=None) -> Stop:
    return Stop(order_id=order_id, snapshot=OrderSnapshot(product_code="DIESEL_2", gallons_requested=gallons), eta=eta)


def test_diff_producer_against_replan_diff_vocabulary():
    lane = Lane(truck_id="T1", version=3, loads=[Load(load_id="L1", shift_id="day", stops=[_stop("o1", eta=NOW), _stop("o4")])])
    plan = plan_doc("plan-9", "T1", ["o3", "o1", "o5"])
    route = route_doc("plan-9", "T1", ["o3", "o1", "o5"])
    orders = {"o3": {"gallons_requested": 450.0, "product_code": "DIESEL_2"}, "o5": {"gallons_requested": 300.0, "product_code": "GAS_87"}}
    result = diff(lane, plan, route, order_index={"o1": "T1", "o4": "T1", "o3": "T2"}, orders=orders)
    assert isinstance(result, ReplanDiff)
    assert result.original_route_id == "board:T1" and result.patched_route_id == "route-plan-9"
    assert [(a.stop_id, a.index, a.product_code) for a in result.added_stops] == [("o5", 3, "GAS_87")]
    assert result.added_stops[0].gallons == 450.0  # the plan's allocation
    assert [(r.stop_id, r.from_truck_id, r.to_truck_id) for r in result.reassigned_stops] == [("o3", "T2", "T1")]
    assert {(r.stop_id, r.before_index, r.after_index) for r in result.reordered_stops} == {("o1", 0, 2), ("o4", 1, 0)}
    assert result.removed_stops == []
    assert [(q.stop_id, q.before_gallons, q.after_gallons) for q in result.quantity_changes] == [("o1", 500.0, 450.0), ("o5", 300.0, 450.0)]
    assert [(e.stop_id, e.shift_minutes) for e in result.eta_shifts] == [("o1", 120.0)]
    assert ReplanDiff.model_validate_json(result.model_dump_json()) == result


def test_diff_for_a_truck_without_a_lane_is_all_added():
    plan = plan_doc("plan-9", "T7", ["o1", "o2"])
    result = diff(None, plan, None, order_index={}, orders={})
    assert [a.stop_id for a in result.added_stops] == ["o1", "o2"]
    assert not (result.reordered_stops or result.reassigned_stops or result.removed_stops or result.eta_shifts)
    assert result.patched_route_id == "plan-9"


def test_suggested_order_ids_route_first_then_plan_only_orders():
    plan = plan_doc("p", "T1", ["o1", "o2", "o3"])
    assert suggested_order_ids(plan, route_doc("p", "T1", ["o3", "o1"])) == ["o3", "o1", "o2"]
    assert suggested_order_ids(plan, None) == ["o1", "o2", "o3"]


async def test_snapshot_diff_shows_orders_moved_from_another_lane():
    w = World()
    await w.h.lane_with("T2", "o1")
    (s,) = await w.suggestions()
    assert [(r["stop_id"], r["from_truck_id"]) for r in s["diff"]["reassigned_stops"]] == [("o1", "T2")]
    assert [a["stop_id"] for a in s["diff"]["added_stops"]] == ["o2"]


# ---------------------------------------------------------------------------
# Accept (R16.3, R16.7, E16)
# ---------------------------------------------------------------------------


def _metric(w: World, action: str) -> int:
    return len([m for m in w.h.telemetry.metrics if m[0] == "board.suggestion.count" and m[2].get("action") == action])


async def test_accept_whole_creates_the_lane_and_never_approves():
    w = World()
    plan_before = w.h.store.doc(PLANS, "plan-1")
    result = await w.h.run("accept_suggestion", suggestion_id="plan-1", versions={"T1": 0}, input_modality="suggestion")
    lane = w.h.draft().lanes["T1"]
    assert [[s.order_id for s in l.stops] for l in lane.loads] == [["o2", "o1"]]
    assert lane.loads[0].source == "suggestion" and lane.loads[0].suggestion_id == "plan-1"
    assert [l["truck_id"] for l in result["lanes"]] == ["T1"]
    assert _metric(w, "accept") == 1
    # R16.7: no approval call, the agent plan and the orders are untouched.
    assert w.approvals.approves == [] and w.approvals.rejects == []
    assert w.h.store.doc(PLANS, "plan-1") == plan_before
    assert {w.h.store.doc(ORDERS, o)["status"] for o in ("o1", "o2")} == {"confirmed"}


async def test_accept_moves_orders_off_other_lanes_and_needs_their_versions():
    w = World()
    await w.h.lane_with("T2", "o1", "o3")
    with pytest.raises(AppException) as info:
        await w.h.run("accept_suggestion", suggestion_id="plan-1", versions={"T1": 0})
    assert info.value.details["reason"] == "missing_expected_version"
    await w.h.run("accept_suggestion", suggestion_id="plan-1", versions=w.h.versions("T1", "T2"))
    d = w.h.draft()
    assert [s.order_id for l in d.lanes["T2"].loads for s in l.stops] == ["o3"]
    assert [s.order_id for l in d.lanes["T1"].loads for s in l.stops] == ["o2", "o1"]


async def test_accept_subset_by_load_ids():
    w = World()
    await w.h.run("accept_suggestion", suggestion_id="plan-1", load_ids=["plan-1"], versions={"T1": 0})
    assert _metric(w, "partial") == 1
    with pytest.raises(AppException) as info:
        await w.h.run("accept_suggestion", suggestion_id="plan-1", load_ids=["nope"], versions={"T1": 1})
    assert info.value.status_code == 422 and info.value.details["reason"] == "unknown_load"


async def test_a_block_refuses_the_whole_accept():
    w = World()
    w.h.store.docs[ORDERS]["o1"]["status"] = "on_hold"
    with pytest.raises(AppException) as info:
        await w.h.run("accept_suggestion", suggestion_id="plan-1", versions={"T1": 0})
    assert info.value.error_code == ErrorCode.BOARD_COMMAND_BLOCKED and info.value.details["reason"] == "on_hold"
    assert w.h.store.draft() is None or "T1" not in w.h.store.draft().lanes
    assert _metric(w, "accept") == 0


async def test_accept_of_a_hidden_suggestion_is_unknown():
    w = World({COMPARTMENT_FLAG: "shadow", ROUTE_FLAG: "active_gated"})
    with pytest.raises(AppException) as info:
        await w.h.run("accept_suggestion", suggestion_id="plan-1", versions={"T1": 0})
    assert info.value.details["reason"] == "unknown_suggestion"


async def test_accept_without_a_reader_is_unknown_and_a_reader_failure_is_503():
    w = World()
    w.h.service.set_suggestion_reader(None)
    with pytest.raises(AppException) as info:
        await w.h.run("accept_suggestion", suggestion_id="plan-1", versions={"T1": 0})
    assert info.value.details["reason"] == "unknown_suggestion"

    class Broken:
        async def for_context(self, *args, **kwargs):
            raise ConnectionError("down")

    w.h.service.set_suggestion_reader(Broken())
    with pytest.raises(AppException) as info:
        await w.h.run("accept_suggestion", suggestion_id="plan-1", versions={"T1": 0})
    assert info.value.status_code == 503


async def test_e7_queue_approval_after_board_accept_blocks_with_committed_elsewhere():
    w = World()
    await w.h.run("accept_suggestion", suggestion_id="plan-1", versions={"T1": 0})
    # Someone approves the agent's entry in the queue: the executor schedules o1 on the agent's run.
    w.h.store.docs[ORDERS]["o1"].update(status="scheduled", assigned_run_id="run-plan-1", assigned_asset_id="T1")
    w.h.now = NOW + timedelta(seconds=61)  # checks older than 60 s are recomputed (K5)
    snap = await w.snapshot()
    lane = next(l for l in snap["lanes"] if l["truck_id"] == "T1")
    blocks = [c for c in lane["checks"] if c["reason_code"] == "order_committed_elsewhere"]
    assert len(blocks) == 1 and blocks[0]["outcome"] == "block"
    # K2.3 fix links have no plan kind: the link opens the order, whose run names the plan.
    assert (blocks[0]["fix_link"]["kind"], blocks[0]["fix_link"]["id"]) == ("order", "o1")


# ---------------------------------------------------------------------------
# Dismiss and reject (R16.4)
# ---------------------------------------------------------------------------


async def test_reject_dismisses_and_rejects_the_pending_approval():
    w = World()
    events: List[tuple] = []

    class WS:
        async def broadcast_board_event(self, tenant_id, day, event_type, data):
            events.append((tenant_id, day, event_type, data))

    w.h.service._ws = WS()
    result = await w.service.reject(tenant_id=T, user_id="user-1", service_date=TODAY, plan_id="plan-1", reason="Too early", tz=TZ)
    assert result == {"plan_id": "plan-1", "dismissed": True, "approval": {"action_id": "act-1", "rejected": True, "reason": None}}
    assert w.approvals.rejects == [("act-1", "user-1", "Too early", T)]
    assert "plan-1" in w.h.draft().dismissed_suggestions
    assert w.h.draft().dismissed_suggestions["plan-1"].actor_user_id == "user-1"
    assert w.h.draft().draft_version == 0  # not a lane change
    assert events == [(T, TODAY.isoformat(), "board_suggestions_changed", {"service_date": TODAY.isoformat()})]
    assert _metric(w, "reject") == 1
    # Gone from the board and no longer acceptable.
    assert await w.suggestions() == []
    with pytest.raises(AppException) as info:
        await w.h.run("accept_suggestion", suggestion_id="plan-1", versions={"T1": 0})
    assert info.value.details["reason"] == "unknown_suggestion"


async def test_reject_failure_is_reported_and_the_dismissal_stays():
    w = World()
    w.approvals.reject_error = ApprovalExpiredError("act-1")
    result = await w.service.reject(tenant_id=T, user_id="user-1", service_date=TODAY, plan_id="plan-1", tz=TZ)
    assert result["approval"] == {"action_id": "act-1", "rejected": False, "reason": "approval_expired"}
    assert w.approvals.rejects == [("act-1", "user-1", "", T)]
    assert "plan-1" in w.h.draft().dismissed_suggestions


async def test_reject_without_a_pending_approval_only_dismisses():
    w = World()
    w.approvals.items = []
    result = await w.service.reject(tenant_id=T, user_id="user-1", service_date=TODAY, plan_id="plan-1", tz=TZ)
    assert result["approval"] is None and w.approvals.rejects == []
    # Twice is harmless.
    await w.service.reject(tenant_id=T, user_id="user-2", service_date=TODAY, plan_id="plan-1", tz=TZ)
    assert w.h.draft().dismissed_suggestions["plan-1"].actor_user_id == "user-1"


@pytest.mark.parametrize("plan_id", ["missing", "plan-other", "bp-L1-r1"])
async def test_reject_of_an_unknown_foreign_or_board_plan_is_404(plan_id):
    w = World()
    w.h.store.seed(PLANS, "plan-other", plan_doc("plan-other", "T1", ["o3"], tenant_id=OTHER))
    w.h.store.seed(PLANS, "bp-L1-r1", plan_doc("bp-L1-r1", "T1", ["o3"], source="dispatch_board"))
    with pytest.raises(AppException) as info:
        await w.service.reject(tenant_id=T, user_id="user-1", service_date=TODAY, plan_id=plan_id, tz=TZ)
    assert info.value.status_code == 404
    assert w.h.store.draft() is None


async def test_reject_on_a_past_day_is_read_only():
    w = World()
    with pytest.raises(AppException) as info:
        await w.service.reject(tenant_id=T, user_id="user-1", service_date=TODAY - timedelta(days=1), plan_id="plan-1", tz=TZ)
    assert info.value.error_code == ErrorCode.DISPATCH_BOARD_READ_ONLY


def test_reject_endpoint_calls_the_service_with_the_session_user():
    w = World()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(board_api.router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(tenant_id=T, user_id="user-2", has_pii_access=False, roles=["dispatcher"])
    board_api.configure_dispatch_board_endpoints(board_service=w.h.service, feature_flag_service=w.flags, suggestion_service=w.service)
    try:
        client = TestClient(app)
        resp = client.post(f"/api/fuel/board/{TODAY.isoformat()}/suggestions/plan-1/reject", json={"reason": "Wrong truck"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["approval"]["rejected"] is True
        assert w.approvals.rejects == [("act-1", "user-2", "Wrong truck", T)]
        # Shadow board: read-only, nothing written.
        w.flags.board = "shadow"
        assert client.post(f"/api/fuel/board/{TODAY.isoformat()}/suggestions/plan-1/reject", json={}).status_code == 409
        assert len(w.approvals.rejects) == 1
    finally:
        board_api.configure_dispatch_board_endpoints(board_service=None, feature_flag_service=None)
