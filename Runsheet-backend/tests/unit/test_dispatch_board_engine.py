"""Dispatch Board engine (plan task 9; design K4.1, K4.3, K6, K8.1; P1, P2, P7).

The engine is pure: every test builds a ``ValidationContext`` by hand. Post-
publish rules are lane checks (``lane_checks``) over the engine's result, so
the started / pinned tests read both.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from Agents.support.compartment_models import Compartment
from Agents.support.fuel_distribution_models import FuelGrade
from fuel.services import dispatch_board_engine as engine
from fuel.services.dispatch_board_models import (
    BoardDraft,
    CommandBody,
    Lane,
    LaneContent,
    LanePublish,
    Load,
    PublishedPlan,
)
from fuel.services.dispatch_validation import ValidationContext, lane_checks
from tests.unit._dispatch_board_fakes import order

T = "tenant-1"
DAY = date(2026, 10, 8)
NOW = datetime(2026, 10, 8, 15, tzinfo=timezone.utc)


def comp(truck: str, cid: str, cap: float = 10000.0) -> Compartment:
    return Compartment(compartment_id=cid, truck_id=truck, capacity_liters=cap, allowed_grades=[FuelGrade.AGO], position_index=int(cid[-1]), tenant_id=T)


def make_ctx(orders: List[Dict[str, Any]] = (), *, trucks=("T1", "T2", "T3"), cap: float = 10000.0, **extra) -> ValidationContext:
    counter = itertools.count(1)
    ctx = ValidationContext(tenant_id=T, service_date=DAY, timezone="America/Chicago", now=NOW, today=DAY)
    ctx.orders = {o["order_id"]: o for o in orders}
    ctx.order_locations = {o["order_id"]: {"lat": o["ship_to_lat"], "lon": o["ship_to_lon"]} for o in orders if o.get("ship_to_lat") is not None}
    ctx.compartments = {t: [comp(t, "C1", cap), comp(t, "C2", cap)] for t in trucks}
    ctx.truck_positions = {t: {"lat": 41.80, "lon": -87.70} for t in trucks}
    ctx.drivers = {d: {"driver_id": d, "tenant_id": T, "status": "active"} for d in ("d1", "d2", "d3")}
    ctx.qualification = {d: {"eligible": True, "reasons": []} for d in ctx.drivers}
    ctx.certification = {t: {"eligible": True, "reasons": []} for t in trucks}
    ctx.hos_verdicts = {d: {"outcome": "passed"} for d in ctx.drivers}
    ctx.new_id = lambda: f"L{next(counter)}"
    for k, v in extra.items():
        setattr(ctx, k, v)
    return ctx


def cmd(type_: str, versions: Dict[str, int], **fields):
    return CommandBody.model_validate(
        {"type": type_, "client_command_id": str(uuid.uuid4()), "expected_lane_versions": versions, **fields}
    ).root


def empty() -> BoardDraft:
    return BoardDraft(tenant_id=T, service_date=DAY, timezone="America/Chicago")


def versions(draft: BoardDraft, *trucks) -> Dict[str, int]:
    return {t: draft.lanes[t].version if t in draft.lanes else 0 for t in trucks}


def apply(draft, command, ctx):
    result = engine.apply(draft, command, ctx)
    # Mimic the commit's version bump so follow-up commands see new versions.
    for t in result.touched:
        if t in result.draft.lanes:
            result.draft.lanes[t].version = (draft.lanes[t].version if t in draft.lanes else 0) + 1
    return result


def with_lanes(ctx, *trucks) -> BoardDraft:
    d = empty()
    for t in trucks:
        d = apply(d, cmd("add_lane", {t: 0}, truck_id=t), ctx).draft
    return d


def stops_of(draft, truck) -> List[List[str]]:
    return [[s.order_id for s in load.stops] for load in draft.lanes[truck].loads]


# ---- add / remove lane ---------------------------------------------------


def test_add_lane_and_errors():
    ctx = make_ctx()
    d = with_lanes(ctx, "T1")
    assert "T1" in d.lanes and d.lanes["T1"].version == 1
    for command, reason in (
        (cmd("add_lane", {"T1": 1}, truck_id="T1"), "lane_exists"),
        (cmd("add_lane", {"TX": 0}, truck_id="TX"), "unknown_truck"),
        (cmd("add_lane", {}, truck_id="T2"), "missing_expected_version"),
    ):
        with pytest.raises(engine.EngineError) as info:
            engine.apply(d, command, ctx)
        assert info.value.reason == reason


def test_add_lane_limit():
    trucks = [f"T{i}" for i in range(61)]
    ctx = make_ctx(trucks=trucks)
    d = empty()
    for t in trucks[:60]:
        d.lanes[t] = Lane(truck_id=t, version=1)
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("add_lane", {"T60": 0}, truck_id="T60"), ctx)
    assert info.value.reason == "limit_exceeded"


def test_add_lane_allowed_while_compartments_unavailable():
    ctx = make_ctx(trucks=())
    ctx.unavailable.add("compartments")
    assert "T9" in engine.apply(empty(), cmd("add_lane", {"T9": 0}, truck_id="T9"), ctx).draft.lanes


def test_remove_lane_returns_orders_to_tray():
    o = order("o1")
    ctx = make_ctx([o])
    d = with_lanes(ctx, "T1")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1"], truck_id="T1"), ctx).draft
    assert d.order_index == {"o1": "T1"}
    d = apply(d, cmd("remove_lane", versions(d, "T1"), truck_id="T1"), ctx).draft
    assert "T1" not in d.lanes and d.order_index == {} and d.order_ids == []


def test_remove_lane_refused_for_ever_published_lane():
    ctx = make_ctx()
    d = with_lanes(ctx, "T1")
    d.lanes["T1"].publish = LanePublish(state="failed", published_version=3)
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("remove_lane", versions(d, "T1"), truck_id="T1"), ctx)
    assert info.value.reason == "lane_published"


# ---- pairing -------------------------------------------------------------


def test_pair_driver_moves_pairing_between_conflicting_lanes():
    o1, o2 = order("o1"), order("o2")
    ctx = make_ctx([o1, o2])
    d = with_lanes(ctx, "T1", "T2")
    d = apply(d, cmd("pair_driver", versions(d, "T1"), truck_id="T1", driver_id="d1"), ctx).draft
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("pair_driver", versions(d, "T2"), truck_id="T2", driver_id="d1"), ctx)
    assert info.value.reason == "missing_expected_version"
    result = apply(d, cmd("pair_driver", versions(d, "T1", "T2"), truck_id="T2", driver_id="d1"), ctx)
    assert result.draft.lanes["T2"].driver_id == "d1" and result.draft.lanes["T1"].driver_id is None
    assert set(result.touched) == {"T1", "T2"}


def test_pair_driver_keeps_both_lanes_when_windows_do_not_overlap():
    ctx = make_ctx([order("o1"), order("o2")])
    d = with_lanes(ctx, "T1", "T2")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1"], truck_id="T1"), ctx).draft
    d = apply(d, cmd("assign_orders", versions(d, "T2"), order_ids=["o2"], truck_id="T2"), ctx).draft
    load2 = d.lanes["T2"].loads[0].load_id
    d = apply(d, cmd("set_load_shift", versions(d, "T2"), load_id=load2, shift_id="night"), ctx).draft
    d = apply(d, cmd("pair_driver", versions(d, "T1"), truck_id="T1", driver_id="d1"), ctx).draft
    result = apply(d, cmd("pair_driver", versions(d, "T2"), truck_id="T2", driver_id="d1"), ctx)
    assert result.draft.lanes["T1"].driver_id == "d1" and result.draft.lanes["T2"].driver_id == "d1"
    assert engine.cross_lane_violation(result.draft) is None


def test_pair_unknown_driver_and_unpair():
    ctx = make_ctx()
    d = with_lanes(ctx, "T1")
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("pair_driver", versions(d, "T1"), truck_id="T1", driver_id="ghost"), ctx)
    assert info.value.reason == "unknown_driver"
    d = apply(d, cmd("pair_driver", versions(d, "T1"), truck_id="T1", driver_id="d2"), ctx).draft
    d = apply(d, cmd("pair_driver", versions(d, "T1"), truck_id="T1", driver_id=None), ctx).draft
    assert d.lanes["T1"].driver_id is None


# ---- assign / move / unassign --------------------------------------------


def test_assign_into_new_load_then_explicit_index():
    orders = [order(f"o{i}") for i in range(1, 4)]
    ctx = make_ctx(orders)
    d = with_lanes(ctx, "T1")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1", "o2"], truck_id="T1", target={"load_id": "new"}), ctx).draft
    load_id = d.lanes["T1"].loads[0].load_id
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o3"], truck_id="T1", target={"load_id": load_id, "index": 1}), ctx).draft
    assert stops_of(d, "T1") == [["o1", "o3", "o2"]]
    load = d.lanes["T1"].loads[0]
    assert load.planned_start is not None and all(s.eta for s in load.stops)
    assert load.stops[0].snapshot.product_code == "DIESEL_2"
    assert {a.order_id for a in load.allocations} == {"o1", "o2", "o3"}
    assert d.order_ids == ["o1", "o2", "o3"]


def test_best_fit_uses_existing_load_with_capacity_else_new_load():
    big = [order(f"b{i}", gallons_requested=2400.0) for i in range(1, 5)]  # ~9,085 L each
    ctx = make_ctx(big)
    d = with_lanes(ctx, "T1")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["b1"], truck_id="T1"), ctx).draft
    assert len(d.lanes["T1"].loads) == 1
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["b2"], truck_id="T1"), ctx).draft
    assert stops_of(d, "T1") == [["b1", "b2"]] or stops_of(d, "T1") == [["b2", "b1"]]
    # A third ~9,085 L order doesn't fit 20,000 L with the 10 % buffer: new load.
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["b3"], truck_id="T1"), ctx).draft
    assert len(d.lanes["T1"].loads) == 2 and stops_of(d, "T1")[1] == ["b3"]
    assert d.lanes["T1"].loads[1].planned_start >= d.lanes["T1"].loads[0].planned_end


def test_assign_errors():
    ctx = make_ctx([order("o1")])
    d = with_lanes(ctx, "T1")
    for fields, reason in (
        ({"order_ids": ["nope"], "truck_id": "T1"}, "unknown_order"),
        ({"order_ids": ["o1"], "truck_id": "T9"}, "unknown_truck"),
        ({"order_ids": ["o1"], "truck_id": "T1", "target": {"index": 0}}, "index_requires_load"),
        ({"order_ids": ["o1"], "truck_id": "T1", "target": {"load_id": "new", "index": 3}}, "index_out_of_range"),
        ({"order_ids": ["o1"], "truck_id": "T1", "target": {"load_id": "L99"}}, "unknown_load"),
    ):
        with pytest.raises(engine.EngineError) as info:
            engine.apply(d, cmd("assign_orders", versions(d, "T1"), **fields), ctx)
        assert info.value.reason == reason


def test_stop_limit_per_load():
    orders = [order(f"o{i}", gallons_requested=1.0) for i in range(31)]
    ctx = make_ctx(orders)
    d = with_lanes(ctx, "T1")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=[f"o{i}" for i in range(25)], truck_id="T1", target={"load_id": "new"}), ctx).draft
    load_id = d.lanes["T1"].loads[0].load_id
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=[f"o{i}" for i in range(25, 31)], truck_id="T1", target={"load_id": load_id, "index": 0}), ctx)
    assert info.value.reason == "limit_exceeded"


def test_assign_of_an_order_on_another_lane_is_a_move_that_needs_both_versions():
    ctx = make_ctx([order("o1")])
    d = with_lanes(ctx, "T1", "T2")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1"], truck_id="T1"), ctx).draft
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("assign_orders", versions(d, "T2"), order_ids=["o1"], truck_id="T2"), ctx)
    assert info.value.reason == "missing_expected_version"
    result = apply(d, cmd("assign_orders", versions(d, "T1", "T2"), order_ids=["o1"], truck_id="T2"), ctx)
    assert result.draft.order_index == {"o1": "T2"} and result.draft.lanes["T1"].loads == []
    assert set(result.touched) == {"T1", "T2"}


def test_move_stops_reorder_and_cross_lane():
    orders = [order(f"o{i}") for i in range(1, 4)]
    ctx = make_ctx(orders)
    d = with_lanes(ctx, "T1", "T2")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1", "o2", "o3"], truck_id="T1", target={"load_id": "new"}), ctx).draft
    load_id = d.lanes["T1"].loads[0].load_id
    d = apply(d, cmd("move_stops", versions(d, "T1"), order_ids=["o3"], truck_id="T1", target={"load_id": load_id, "index": 0}), ctx).draft
    assert stops_of(d, "T1") == [["o3", "o1", "o2"]]
    d = apply(d, cmd("move_stops", versions(d, "T1", "T2"), order_ids=["o1"], truck_id="T2", target={"load_id": "new"}), ctx).draft
    assert stops_of(d, "T2") == [["o1"]] and stops_of(d, "T1") == [["o3", "o2"]]
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("move_stops", versions(d, "T1"), order_ids=["zzz"], truck_id="T1"), ctx)
    assert info.value.reason == "order_not_on_board"


def _published(draft: BoardDraft, truck: str, ctx, *, driver: str = "d1") -> BoardDraft:
    """Mark lane ``truck`` published as it is now."""
    lane = draft.lanes[truck]
    lane.driver_id = driver
    lane.publish = LanePublish(
        state="published",
        published_version=lane.version,
        published_content=LaneContent(driver_id=driver, loads=[l.model_copy(deep=True) for l in lane.loads], shelf=list(lane.shelf)),
        published_hash=engine.content_hash(lane),
        plans={l.load_id: PublishedPlan(plan_id=f"bp-{l.load_id}-r1", route_id=f"br-{l.load_id}-r1", run_id=f"bp-{l.load_id}-r1", revision=1) for l in lane.loads},
    )
    return draft


def test_unassign_returns_to_tray_or_shelf_for_dispatched_orders():
    o1, o2 = order("o1"), order("o2")
    ctx = make_ctx([o1, o2])
    d = with_lanes(ctx, "T1")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1", "o2"], truck_id="T1", target={"load_id": "new"}), ctx).draft
    d = _published(d, "T1", ctx)
    run = d.lanes["T1"].publish.plans[d.lanes["T1"].loads[0].load_id].run_id
    ctx.orders["o1"].update(status="dispatched", assigned_run_id=run)
    d = apply(d, cmd("unassign_orders", versions(d, "T1"), order_ids=["o1", "o2"]), ctx).draft
    assert d.lanes["T1"].shelf == ["o1"] and d.order_index == {"o1": "T1"}
    assert d.lanes["T1"].loads[0].stops == []  # published load stays (retired on re-publish)
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("unassign_orders", versions(d, "T1"), order_ids=["o1"]), ctx)
    assert info.value.reason == "dispatched_order_cannot_unassign"


def test_shelved_order_can_be_placed_again():
    o1 = order("o1")
    ctx = make_ctx([o1])
    d = with_lanes(ctx, "T1", "T2")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1"], truck_id="T1"), ctx).draft
    d = _published(d, "T1", ctx)
    ctx.orders["o1"].update(status="dispatched", assigned_run_id=next(iter(d.lanes["T1"].publish.plans.values())).run_id)
    d = apply(d, cmd("unassign_orders", versions(d, "T1"), order_ids=["o1"]), ctx).draft
    d = apply(d, cmd("move_stops", versions(d, "T1", "T2"), order_ids=["o1"], truck_id="T2", target={"load_id": "new"}), ctx).draft
    assert d.lanes["T1"].shelf == [] and stops_of(d, "T2") == [["o1"]]


# ---- loads, terminal, allocation, shift ----------------------------------


def test_move_load_within_and_across_lanes():
    ctx = make_ctx([order("o1"), order("o2")])
    d = with_lanes(ctx, "T1", "T2")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1"], truck_id="T1", target={"load_id": "new"}), ctx).draft
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o2"], truck_id="T1", target={"load_id": "new"}), ctx).draft
    first, second = (l.load_id for l in d.lanes["T1"].loads)
    d = apply(d, cmd("move_load", versions(d, "T1"), load_id=second, truck_id="T1", index=0), ctx).draft
    assert [l.load_id for l in d.lanes["T1"].loads] == [second, first]
    d = apply(d, cmd("set_allocation", versions(d, "T1"), load_id=first, order_id="o1", shares=[{"compartment_id": "C1", "liters": 1000}]), ctx).draft
    d = apply(d, cmd("move_load", versions(d, "T1", "T2"), load_id=first, truck_id="T2", index=0), ctx).draft
    assert d.lanes["T2"].loads[0].load_id == first and d.lanes["T2"].loads[0].allocation_overrides == {}
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("move_load", versions(d, "T1", "T2"), load_id=second, truck_id="T2", index=5), ctx)
    assert info.value.reason == "index_out_of_range"


def test_set_terminal_shift_and_allocation():
    ctx = make_ctx([order("o1")], terminals={"TERM1"}, terminal_locations={"TERM1": {"lat": 41.7, "lon": -87.6}})
    d = with_lanes(ctx, "T1")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1"], truck_id="T1"), ctx).draft
    load_id = d.lanes["T1"].loads[0].load_id
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("set_terminal", versions(d, "T1"), load_id=load_id, terminal_id="NOPE"), ctx)
    assert info.value.reason == "unknown_terminal"
    before = d.lanes["T1"].loads[0].stops[0].eta
    d = apply(d, cmd("set_terminal", versions(d, "T1"), load_id=load_id, terminal_id="TERM1"), ctx).draft
    assert d.lanes["T1"].loads[0].terminal_id == "TERM1"
    assert d.lanes["T1"].loads[0].stops[0].eta > before  # 45-minute lift
    d = apply(d, cmd("set_load_shift", versions(d, "T1"), load_id=load_id, shift_id="night"), ctx).draft
    assert d.lanes["T1"].loads[0].planned_start.hour == 18
    liters = engine.order_liters(ctx.orders["o1"])
    d = apply(d, cmd("set_allocation", versions(d, "T1"), load_id=load_id, order_id="o1", shares=[{"compartment_id": "C2", "liters": liters}]), ctx).draft
    assert [(a.compartment_id, a.liters) for a in d.lanes["T1"].loads[0].allocations] == [("C2", liters)]
    for shares, reason in (
        ([{"compartment_id": "C9", "liters": 10}], "unknown_compartment"),
        ([{"compartment_id": "C1", "liters": liters * 1.02}], "invalid_shares"),
        ([{"compartment_id": "C1", "liters": 1}, {"compartment_id": "C1", "liters": 1}], "invalid_shares"),
    ):
        with pytest.raises(engine.EngineError) as info:
            engine.apply(d, cmd("set_allocation", versions(d, "T1"), load_id=load_id, order_id="o1", shares=shares), ctx)
        assert info.value.reason == reason
    d = apply(d, cmd("set_allocation", versions(d, "T1"), load_id=load_id, order_id="o1", shares=None), ctx).draft
    assert d.lanes["T1"].loads[0].allocation_overrides == {}


def test_accept_suggestion_whole_subset_and_new_lane():
    ctx = make_ctx([order(f"o{i}") for i in range(1, 4)])
    ctx.suggestions = {
        "plan-1": {
            "loads": [
                {"load_key": "plan-1", "truck_id": "T1", "terminal_id": None, "order_ids": ["o1", "o2"]},
                {"load_key": "plan-1b", "truck_id": "T3", "terminal_id": None, "order_ids": ["o3"]},
            ]
        }
    }
    d = with_lanes(ctx, "T1")
    whole = apply(d, cmd("accept_suggestion", versions(d, "T1", "T3"), suggestion_id="plan-1"), ctx).draft
    assert stops_of(whole, "T1") == [["o1", "o2"]] and stops_of(whole, "T3") == [["o3"]]  # E16
    assert whole.lanes["T1"].loads[0].source == "suggestion"
    subset = apply(d, cmd("accept_suggestion", versions(d, "T1"), suggestion_id="plan-1", load_ids=["plan-1"]), ctx).draft
    assert "T3" not in subset.lanes
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("accept_suggestion", {}, suggestion_id="nope"), ctx)
    assert info.value.reason == "unknown_suggestion"


def test_discard_lane_changes_restores_published_content():
    ctx = make_ctx([order("o1"), order("o2")])
    d = with_lanes(ctx, "T1")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1"], truck_id="T1"), ctx).draft
    d = _published(d, "T1", ctx)
    with pytest.raises(engine.EngineError) as info:
        engine.apply(d, cmd("discard_lane_changes", versions(d, "T1"), truck_id="T1"), ctx)
    assert info.value.reason == "lane_not_modified"
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o2"], truck_id="T1", target={"load_id": "new"}), ctx).draft
    assert engine.content_hash(d.lanes["T1"]) != d.lanes["T1"].publish.published_hash
    d = apply(d, cmd("discard_lane_changes", versions(d, "T1"), truck_id="T1"), ctx).draft
    assert engine.content_hash(d.lanes["T1"]) == d.lanes["T1"].publish.published_hash
    assert d.order_index == {"o1": "T1"}


def test_restore_lanes_removes_absent_lane_and_rejects_duplicates():
    ctx = make_ctx([order("o1")])
    d = with_lanes(ctx, "T1", "T2")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1"], truck_id="T1"), ctx).draft
    assert "T2" not in engine.restore_lanes(d, {"T2": None}, ctx).draft.lanes
    with pytest.raises(engine.EngineError) as info:
        engine.restore_lanes(d, {"T2": engine.lane_content_dump(d.lanes["T1"])}, ctx)
    assert info.value.reason == "order_on_two_lanes"


def test_cross_lane_violation_detects_both_rules():
    d = empty()
    d.lanes["T1"] = Lane(truck_id="T1", version=1, shelf=["o1"], driver_id="d1")
    d.lanes["T2"] = Lane(truck_id="T2", version=1, shelf=["o1"])
    assert engine.cross_lane_violation(d) == "order_exclusivity"
    d.lanes["T2"].shelf = []
    d.lanes["T2"].driver_id = "d1"
    assert engine.cross_lane_violation(d) == "driver_double_booked"


# ---- started loads and pinned stops (K8.1, Q12, freeze rule 11 (e)) ------


def _started_setup(completed_stops: int):
    o1, o2 = order("o1"), order("o2")
    ctx = make_ctx([o1, o2])
    d = with_lanes(ctx, "T1")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1"], truck_id="T1"), ctx).draft
    d = _published(d, "T1", ctx)
    load_id = d.lanes["T1"].loads[0].load_id
    plan = d.lanes["T1"].publish.plans[load_id]
    ctx.orders["o1"].update(status="dispatched", assigned_run_id=plan.run_id)
    ctx.executions = {plan.plan_id: completed_stops}
    return ctx, d, load_id


def test_load_with_zero_completed_stops_accepts_an_added_order():
    ctx, d, load_id = _started_setup(0)
    assert not engine.load_started(d.lanes["T1"], load_id, ctx)
    d2 = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o2"], truck_id="T1", target={"load_id": load_id}), ctx).draft
    checks = lane_checks(ctx, d2.lanes["T1"], draft=d2)
    assert not [c for c in checks if c.reason_code == "load_started"]


def test_load_with_one_completed_stop_blocks_an_added_order():
    ctx, d, load_id = _started_setup(1)
    assert engine.load_started(d.lanes["T1"], load_id, ctx)
    d2 = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o2"], truck_id="T1", target={"load_id": load_id}), ctx).draft
    blocks = [c for c in lane_checks(ctx, d2.lanes["T1"], draft=d2) if c.outcome == "block"]
    assert [c.reason_code for c in blocks] == ["load_started"]
    assert blocks[0].scope.order_id == "o2"


def test_best_fit_skips_started_loads():
    ctx, d, load_id = _started_setup(1)
    d2 = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o2"], truck_id="T1"), ctx).draft
    assert stops_of(d2, "T1") == [["o1"], ["o2"]]


def test_started_load_freezes_terminal_allocation_and_driver():
    ctx, d, load_id = _started_setup(1)
    d2 = apply(d, cmd("set_terminal", versions(d, "T1"), load_id=load_id, terminal_id="TERM9"), ctx).draft
    assert "load_started" in {c.reason_code for c in lane_checks(ctx, d2.lanes["T1"], draft=d2)}
    liters = engine.order_liters(ctx.orders["o1"])
    d3 = apply(d, cmd("set_allocation", versions(d, "T1"), load_id=load_id, order_id="o1", shares=[{"compartment_id": "C2", "liters": liters}]), ctx).draft
    assert "load_started" in {c.reason_code for c in lane_checks(ctx, d3.lanes["T1"], draft=d3)}
    d4 = apply(d, cmd("pair_driver", versions(d, "T1"), truck_id="T1", driver_id="d2"), ctx).draft
    blocks = [c for c in lane_checks(ctx, d4.lanes["T1"], draft=d4) if c.outcome == "block"]
    assert [c.reason_code for c in blocks] == ["load_started"]


def test_driver_change_on_not_started_published_load_is_allowed():
    ctx, d, _load_id = _started_setup(0)
    d4 = apply(d, cmd("pair_driver", versions(d, "T1"), truck_id="T1", driver_id="d2"), ctx).draft
    assert not [c for c in lane_checks(ctx, d4.lanes["T1"], draft=d4) if c.outcome == "block"]


def test_freeze_rule_11e_pinned_stop_on_wrong_lane_must_move_back():
    """After a rollback a pinned order can sit on lane B; it may only go home."""
    o1, o2 = order("o1"), order("o2")
    ctx = make_ctx([o1, o2])
    d = with_lanes(ctx, "T1", "T2", "T3")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1", "o2"], truck_id="T1", target={"load_id": "new"}), ctx).draft
    d = _published(d, "T1", ctx)
    home_load = d.lanes["T1"].loads[0].load_id
    run = d.lanes["T1"].publish.plans[home_load].run_id
    # The dispatcher moved o1 to T2 while it was dispatched; then the driver started it.
    d = apply(d, cmd("move_stops", versions(d, "T1", "T2"), order_ids=["o1"], truck_id="T2", target={"load_id": "new"}), ctx).draft
    ctx.orders["o1"].update(status="in_transit", assigned_run_id=run)
    ctx.orders["o2"].update(status="dispatched", assigned_run_id=run)

    b_blocks = [c for c in lane_checks(ctx, d.lanes["T2"], draft=d) if c.outcome == "block"]
    assert [c.reason_code for c in b_blocks] == ["stop_pinned"]
    assert b_blocks[0].message == "Started on T1. Move it back."
    assert b_blocks[0].fix_link.kind == "move_back"
    assert (b_blocks[0].fix_link.truck_id, b_blocks[0].fix_link.load_id) == ("T1", home_load)
    assert "stop_pinned" in {c.reason_code for c in lane_checks(ctx, d.lanes["T1"], draft=d)}

    # Moving it somewhere else is blocked on the destination.
    away = apply(d, cmd("move_stops", versions(d, "T2", "T3"), order_ids=["o1"], truck_id="T3", target={"load_id": "new"}), ctx).draft
    assert "stop_pinned" in {c.reason_code for c in lane_checks(ctx, away.lanes["T3"], draft=away)}

    # Moving it back onto the load its run names clears every block on both lanes.
    back = apply(d, cmd("move_stops", versions(d, "T1", "T2"), order_ids=["o1"], truck_id="T1", target={"load_id": home_load, "index": 0}), ctx).draft
    for truck in ("T1", "T2"):
        assert not [c for c in lane_checks(ctx, back.lanes[truck], draft=back) if c.outcome == "block"]


def test_pinned_stop_reorder_within_its_load_is_allowed():
    o1, o2 = order("o1"), order("o2")
    ctx = make_ctx([o1, o2])
    d = with_lanes(ctx, "T1")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1", "o2"], truck_id="T1", target={"load_id": "new"}), ctx).draft
    d = _published(d, "T1", ctx)
    load_id = d.lanes["T1"].loads[0].load_id
    run = d.lanes["T1"].publish.plans[load_id].run_id
    ctx.orders["o1"].update(status="delivered", assigned_run_id=run)
    ctx.orders["o2"].update(status="dispatched", assigned_run_id=run)
    d2 = apply(d, cmd("move_stops", versions(d, "T1"), order_ids=["o2"], truck_id="T1", target={"load_id": load_id, "index": 0}), ctx).draft
    assert not [c for c in lane_checks(ctx, d2.lanes["T1"], draft=d2) if c.outcome == "block"]


# ---- properties ----------------------------------------------------------

ORDER_IDS = [f"o{i}" for i in range(8)]
TRUCKS = ["T1", "T2", "T3"]

_command_specs = st.lists(
    st.one_of(
        st.tuples(st.just("assign_orders"), st.lists(st.sampled_from(ORDER_IDS), min_size=1, max_size=3, unique=True), st.sampled_from(TRUCKS)),
        st.tuples(st.just("unassign_orders"), st.lists(st.sampled_from(ORDER_IDS), min_size=1, max_size=2, unique=True), st.none()),
        st.tuples(st.just("pair_driver"), st.sampled_from(["d1", "d2", "d3"]), st.sampled_from(TRUCKS)),
    ),
    min_size=1,
    max_size=10,
)


def _run_specs(specs):
    ctx = make_ctx([order(o, gallons_requested=100.0) for o in ORDER_IDS])
    d = with_lanes(ctx, *TRUCKS)
    for kind, arg, truck in specs:
        all_versions = versions(d, *TRUCKS)
        if kind == "assign_orders":
            command = cmd(kind, all_versions, order_ids=arg, truck_id=truck)
        elif kind == "unassign_orders":
            command = cmd(kind, all_versions, order_ids=arg)
        else:
            command = cmd(kind, all_versions, truck_id=truck, driver_id=arg)
        try:
            d = apply(d, command, ctx).draft
        except engine.EngineError:
            continue
        yield ctx, d, command


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_command_specs)
def test_p1_exclusivity(specs):
    for _ctx, d, _c in _run_specs(specs):
        seen = [o for lane in d.lanes.values() for o in engine.lane_order_ids(lane)]
        assert len(seen) == len(set(seen))
        assert d.order_ids == sorted(d.order_index)
        assert all(d.order_index[o] == t for t, lane in d.lanes.items() for o in engine.lane_order_ids(lane))
        assert engine.cross_lane_violation(d) is None


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_command_specs)
def test_p2_undo_round_trip(specs):
    ctx = make_ctx([order(o, gallons_requested=100.0) for o in ORDER_IDS])
    d = with_lanes(ctx, *TRUCKS)
    for kind, arg, truck in specs:
        all_versions = versions(d, *TRUCKS)
        if kind == "assign_orders":
            command = cmd(kind, all_versions, order_ids=arg, truck_id=truck)
        elif kind == "unassign_orders":
            command = cmd(kind, all_versions, order_ids=arg)
        else:
            command = cmd(kind, all_versions, truck_id=truck, driver_id=arg)
        try:
            result = apply(d, command, ctx)
        except engine.EngineError:
            continue
        after = result.draft
        before_contents = {t: engine.lane_content_dump(d.lanes.get(t)) for t in result.touched}
        after_contents = {t: engine.lane_content_dump(after.lanes.get(t)) for t in result.touched}
        reverted = engine.restore_lanes(after, before_contents, ctx).draft
        for t in TRUCKS:
            assert engine.content_hash(reverted.lanes[t]) == engine.content_hash(d.lanes[t])
        reapplied = engine.restore_lanes(reverted, after_contents, ctx).draft
        for t in TRUCKS:
            assert engine.content_hash(reapplied.lanes[t]) == engine.content_hash(after.lanes[t])
        d = after


@settings(max_examples=60, deadline=None)
@given(
    version=st.integers(0, 1000),
    stale=st.booleans(),
    state=st.sampled_from(["draft", "publishing", "published", "failed"]),
    shift=st.sampled_from(["day", "night", "all"]),
    driver=st.sampled_from([None, "d1", "d2"]),
)
def test_p7_content_hash(version, stale, state, shift, driver):
    ctx = make_ctx([order("o1"), order("o2")])
    d = with_lanes(ctx, "T1")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1", "o2"], truck_id="T1", target={"load_id": "new"}), ctx).draft
    lane = d.lanes["T1"]
    base = engine.content_hash(lane)
    noisy = lane.model_copy(deep=True)
    noisy.version = version
    noisy.checks_stale = stale
    noisy.checks_computed_at = NOW
    noisy.publish = LanePublish(state=state, published_hash="x", published_version=version)
    for load in noisy.loads:
        load.planned_start = None
        load.allocations = []
        for stop in load.stops:
            stop.eta = None
    assert engine.content_hash(noisy) == base
    changed = lane.model_copy(deep=True)
    changed.loads[0].shift_id = shift
    changed.driver_id = driver
    expect_same = shift == lane.loads[0].shift_id and driver == lane.driver_id
    assert (engine.content_hash(changed) == base) == expect_same
    reordered = lane.model_copy(deep=True)
    reordered.loads[0].stops.reverse()
    assert engine.content_hash(reordered) != base
