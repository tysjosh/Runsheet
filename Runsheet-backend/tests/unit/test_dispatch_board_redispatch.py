"""BoardRedispatchService (plan task 16; design K8.1-K8.6, freeze rules 1-11; P8, P9, P12).

Every scenario runs the real executor, plan dispatch, plan execution service,
order repository (relink CAS, K5a guard) and driver work service over the
board's fake store. ``on_phase`` hooks inject driver actions and crashes at the
recorded phase boundaries.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from driver.services.order_transition_service import DriverTransitionGateStack
from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.order_repository import OrderChangedConcurrentlyError
from fuel.services import dispatch_board_engine as engine
from fuel.services import dispatch_board_publish as pub
from fuel.services.dispatch_board_models import (
    Lane,
    LaneContent,
    LanePublish,
    Load,
    OrderSnapshot,
    PublishedPlan,
    Stop,
)
from tests.unit._dispatch_board_fakes import DRAFTS, ORDERS, T, TODAY
from tests.unit._dispatch_board_publish_world import EXECUTIONS, OPERATIONAL, PLANS, ROUTES, World

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def w() -> World:
    return World()


async def expect(code, coro):
    with pytest.raises(AppException) as info:
        await coro
    assert info.value.error_code == code, info.value.details
    return info.value


async def publish_a(w: World, *orders: str, truck: str = "T1", driver: str = "d1") -> str:
    """Publish ``truck`` with ``orders`` on one load; returns the load id."""
    w.seed(*orders)
    await w.lane(truck, *orders, driver_id=driver)
    await w.run(truck)
    lane = w.lane_doc(truck)
    assert lane.publish.state == "published", lane.publish.last_result
    return lane.loads[0].load_id


def stop_of(w: World, route_id: str, order_id: str) -> Dict[str, Any]:
    return next(s for s in w.route(route_id)["stops"] if order_id in s["order_ids"])


async def checkin(w: World, plan: PublishedPlan, order_id: str, *, driver=None) -> Dict[str, Any]:
    """``record_checkin`` (the driver-assignment check is covered by its own suite)."""
    stop = stop_of(w, plan.route_id, order_id)
    return await w.executions.record_checkin(
        plan_id=plan.plan_id, route_id=plan.route_id, station_id=stop["station_id"], sequence=stop["sequence"],
        actual_quantities={"DIESEL_2": 1000.0}, tenant_id=T, driver_id=driver, order_id=order_id,
    )


def strip(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in (doc or {}).items() if k not in ("updated_at",)}


def operational_docs(w: World) -> Dict[str, Dict[str, Any]]:
    out = {}
    for index in (ORDERS, PLANS, ROUTES, EXECUTIONS):
        for doc_id, doc in w.store.docs[index].items():
            clean = strip(doc)
            if index == ORDERS:
                clean.pop("last_event_timestamp", None)
                clean.pop("assigned_claim_id", None)
            out[f"{index}/{doc_id}"] = clean
    return out


def on_phase(w: World, actions: Dict[str, Any]) -> None:
    async def hook(name: str, info: Dict[str, Any]) -> None:
        action = actions.pop(name, None)
        if action is not None:
            result = action()
            if asyncio.iscoroutine(result):
                await result

    w.redispatch.on_phase = hook


def crash() -> None:
    raise asyncio.CancelledError()


async def run_crashing(w: World, *trucks: str, reasons=None) -> None:
    await w.start(*trucks, reasons=reasons)
    await w.publish.wait_idle()
    w.redispatch.on_phase = None


def expire(w: World) -> None:
    """Past the publish lease and the executor's plan lease."""
    w.advance(300)


# ---- classification (K8.2, P8) and amend sequences (P9) --------------------


def _published_lane(order_statuses: Dict[str, str], *, completed: int = 0, changed: bool = True, planned_start=None):
    stops = [Stop(order_id=o, snapshot=OrderSnapshot()) for o in order_statuses]
    published = Load(load_id="L1", stops=stops, planned_start=planned_start)
    current = Load(load_id="L1", stops=list(reversed(stops)) if changed else stops, planned_start=planned_start)
    lane = Lane(
        truck_id="T1", version=3, driver_id="d1", loads=[current],
        publish=LanePublish(
            state="published",
            plans={"L1": PublishedPlan(plan_id="bp-L1-r1", route_id="br-L1-r1", run_id="bp-L1-r1", revision=1)},
            published_content=LaneContent(driver_id="d1", loads=[published]),
            published_version=2,
        ),
    )
    orders = {o: {"order_id": o, "status": s} for o, s in order_statuses.items()}
    return lane, orders, {"bp-L1-r1": completed}


def test_i_not_started_past_planned_start_is_new_revision_with_driver_may_be_loading():
    lane, orders, execs = _published_lane({"o1": "dispatched", "o2": "dispatched"}, planned_start=NOW - timedelta(hours=1))
    change = pub.plan_changes([lane], orders, execs, NOW)["L1"]
    assert change.load_class == "new_revision" and change.info == ["driver_may_be_loading"]
    lane, orders, execs = _published_lane({"o1": "dispatched", "o2": "dispatched"}, planned_start=NOW + timedelta(hours=1))
    assert pub.plan_changes([lane], orders, execs, NOW)["L1"].info == []


def test_i_in_transit_or_completed_stop_makes_it_an_amend():
    lane, orders, execs = _published_lane({"o1": "in_transit", "o2": "dispatched"})
    assert pub.plan_changes([lane], orders, execs, NOW)["L1"].load_class == "amend"
    lane, orders, execs = _published_lane({"o1": "dispatched", "o2": "dispatched"}, completed=1)
    assert pub.plan_changes([lane], orders, execs, NOW)["L1"].load_class == "amend"


def test_unchanged_removed_and_new_classes():
    lane, orders, execs = _published_lane({"o1": "dispatched"}, changed=False)
    assert pub.plan_changes([lane], orders, execs, NOW)["L1"].load_class == "unchanged"
    lane.loads[0].stops = []
    assert pub.plan_changes([lane], orders, execs, NOW)["L1"].load_class == "removed"
    lane.loads.append(Load(load_id="L2", stops=[Stop(order_id="o9", snapshot=OrderSnapshot())]))
    assert pub.plan_changes([lane], orders, execs, NOW)["L2"].load_class == "new"
    lane, orders, execs = _published_lane({"o1": "dispatched"}, changed=False)
    lane.driver_id = "d2"  # a driver change on a not-started load is a new revision
    assert pub.plan_changes([lane], orders, execs, NOW)["L1"].load_class == "new_revision"


@settings(max_examples=150, deadline=None)
@given(
    statuses=st.lists(st.sampled_from(["dispatched", "in_transit", "delivered", "failed"]), min_size=1, max_size=5),
    completed=st.integers(0, 2),
    keep=st.lists(st.booleans(), min_size=5, max_size=5),
    reorder=st.booleans(),
    driver_change=st.booleans(),
    past=st.booleans(),
)
def test_p8_every_load_gets_exactly_one_class(statuses, completed, keep, reorder, driver_change, past):
    ids = [f"o{i}" for i in range(len(statuses))]
    lane, orders, execs = _published_lane(dict(zip(ids, statuses)), completed=completed, planned_start=NOW + timedelta(hours=-1 if past else 1))
    current = [s for s, k in zip(lane.publish.published_content.loads[0].stops, keep) if k]
    lane.loads[0].stops = list(reversed(current)) if reorder else current
    if driver_change:
        lane.driver_id = "d9"
    changes = pub.plan_changes([lane], orders, execs, NOW)
    assert set(changes) == {"L1"}
    change = changes["L1"]
    started = completed > 0 or any(s in engine.PINNED_STATUSES for s in statuses)
    same = [s.order_id for s in lane.loads[0].stops] == ids and not driver_change
    if not lane.loads[0].stops:
        assert change.load_class == ("amend" if started else "removed")
    elif same:
        assert change.load_class == "unchanged"
    else:
        assert change.load_class == ("amend" if started else "new_revision")
    assert ("driver_may_be_loading" in change.info) == (change.load_class == "new_revision" and past)


@settings(max_examples=200, deadline=None)
@given(
    statuses=st.lists(st.sampled_from(["pending", "pending", "completed", "failed"]), min_size=1, max_size=7),
    pinned=st.sets(st.integers(0, 6)),
    order=st.permutations(list(range(7))),
    keep=st.lists(st.booleans(), min_size=7, max_size=7),
    gap=st.integers(0, 3),
)
def test_p9_amend_keeps_fixed_sequences_and_never_reuses_numbers(statuses, pinned, order, keep, gap):
    n = len(statuses)
    route = [{"sequence": i + 1 + (gap if i else 0), "station_id": f"s{i}", "order_ids": [f"o{i}"], "drop": {}} for i in range(n)]
    records = [{"station_id": f"s{i}", "sequence": route[i]["sequence"], "status": statuses[i]} for i in range(n)]
    pinned_ids = {f"o{i}" for i in pinned if i < n}
    fixed = {f"o{i}" for i in range(n) if statuses[i] != "pending" or f"o{i}" in pinned_ids}
    target = [f"o{i}" for i in order if i < n and (keep[i] or f"o{i}" in fixed)]
    old_max = max(r["sequence"] for r in records)
    res = pub.amend_stops(records, route, target, pinned_ids)
    seqs = [int(r["sequence"]) for r in res.stops]
    assert len(seqs) == len(set(seqs))
    by_station = {r["station_id"]: r for r in records}
    for r in res.stops:
        original = by_station[r["station_id"]]
        oid = f"o{r['station_id'][1:]}"
        if oid in fixed:
            assert r["sequence"] == original["sequence"] and r["status"] == original["status"]
        else:
            assert r["sequence"] > old_max
    renumbered = [f"o{r['station_id'][1:]}" for r in res.stops if f"o{r['station_id'][1:]}" not in fixed]
    assert renumbered == [o for o in target if o not in fixed]
    again = pub.amend_stops(res.stops, pub.route_stops_for(res.stops, route), target, pinned_ids)
    assert again.changed is False and again.stops == res.stops


# ---- (a)-(c): amend and new revision through the driver work read ----------


async def test_a_c_amend_keeps_pinned_stop_and_driver_reads_new_order(w):
    load_id = await publish_a(w, "o1", "o2", "o3")
    plan = w.plan_of("T1")
    await w.driver_start("o1")
    await checkin(w, plan, "o1")
    w.store.poke(ORDERS, "o1", status="delivered")
    before = await w.work_read("d1", "o2")  # warm the cache
    assert [s["sequence"] for s in before["stops"]] == [1, 2, 3]
    await w.move(["o3"], "T1", lanes=("T1",), target={"load_id": load_id, "index": 1})
    w.driver_ws.calls.clear()
    await w.run("T1")
    assert w.lane_doc("T1").publish.state == "published"
    assert w.plan_of("T1").plan_id == plan.plan_id and w.plan_of("T1").revision == 2
    execution = w.executions_of(plan.plan_id)[0]
    stops = sorted(execution["stops"], key=lambda s: s["sequence"])
    assert [(s["sequence"], s["status"]) for s in stops] == [(1, "completed"), (4, "pending"), (5, "pending")]
    assert [s.get("order_id") for s in stops[1:]] == ["o3", "o2"]
    route = w.route(plan.route_id)
    assert [(s["sequence"], s["order_ids"]) for s in route["stops"]] == [(1, ["o1"]), (4, ["o3"]), (5, ["o2"])]
    assert route["revision"] == 2 and w.plan(plan.plan_id)["revision"] == 2
    after = await w.work_read("d1", "o2")
    assert [(s["sequence"], s["status"]) for s in after["stops"]] == [(1, "completed"), (4, "pending"), (5, "pending")]
    assert [c[0] for c in w.driver_ws.calls] == ["new_route"]
    assert w.driver_ws.calls[0][2]["revision"] == 2


async def test_b_new_revision_supersedes_old_documents(w):
    load_id = await publish_a(w, "o1", "o2")
    old = w.plan_of("T1")
    old_exec = w.executions_of(old.plan_id)[0]
    await w.move(["o2"], "T1", lanes=("T1",), target={"load_id": load_id, "index": 0})
    w.driver_ws.calls.clear()
    await w.run("T1")
    new = w.plan_of("T1")
    assert (new.plan_id, new.revision) == (pub.board_ids(load_id, 2)[0], 2)
    assert w.plan(old.plan_id)["status"] == "superseded" and w.plan(old.plan_id)["superseded_by_plan_id"] == new.plan_id
    assert w.route(old.route_id)["status"] == "superseded"
    assert w.store.doc(EXECUTIONS, old_exec["execution_id"])["status"] == "superseded"
    plan = w.plan(new.plan_id)
    assert plan["status"] == "dispatched" and plan["supersedes_plan_id"] == old.plan_id and plan["revision"] == 2
    assert w.links("o1") == (new.plan_id, "T1", "d1")
    read = await w.work_read("d1", "o1")
    assert read["manifest_available"] is True and len(read["stops"]) == 2
    new_exec = w.executions_of(new.plan_id)
    assert len(new_exec) == 1 and new_exec[0]["status"] == "in_progress"
    # (h) A driver keeping an order under a new revision gets no revocation for it.
    assert [c[0] for c in w.driver_ws.calls] == ["assignment"]
    assert w.driver_ws.calls[0][2]["order_ids"] == ["o1", "o2"]


# ---- (d) and freeze rules 4, 8: call order ---------------------------------


async def test_d_freeze_rules_4_and_8_writes_then_invalidate_then_notify(w, monkeypatch):
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    dispatch_calls: List[Dict[str, Any]] = []
    original = w.dispatch.dispatch

    async def spy(**kw):
        dispatch_calls.append(kw)
        return await original(**kw)

    monkeypatch.setattr(w.dispatch, "dispatch", spy)
    mark = len(w.store.ops)
    await w.run("T2")
    ops = w.store.ops[mark:]
    last_write = max(i for i, op in enumerate(ops) if op[3] and op[1] in OPERATIONAL and op[0] != "notify")
    invalidations = [i for i, op in enumerate(ops) if op[0] == "invalidate"]
    notifications = [i for i, op in enumerate(ops) if op[0] == "notify"]
    assert invalidations and notifications
    assert last_write < min(invalidations) and max(invalidations) < min(notifications)
    assert dispatch_calls and all(c["notify"] is False for c in dispatch_calls)
    kinds = [c[0] for c in w.driver_ws.calls if c[0] != "assignment" or c[2]["plan_id"] != "bp-L1-r1"]
    assert kinds[0] == "assignment_revoked"  # revocations first


# ---- (e): failure at amend, forward recovery --------------------------------


async def test_e_amend_failure_after_retries_is_forward_and_retry_resumes(w, monkeypatch):
    load_id = await publish_a(w, "o1", "o2", "o3")
    plan = w.plan_of("T1")
    await w.driver_start("o1")
    await w.move(["o3"], "T1", lanes=("T1",), target={"load_id": load_id, "index": 1})
    execution_id = w.executions_of(plan.plan_id)[0]["execution_id"]
    w.store.fail_on("atomic_update", EXECUTIONS, execution_id, times=4)
    w.driver_ws.calls.clear()
    reasons = await w.reasons_for("T1")
    await w.run("T1", reasons=reasons)
    lane = w.lane_doc("T1")
    result = lane.publish.last_result
    assert lane.publish.state == "failed" and lane.publish.attempt is not None
    assert (result.stage, result.recovery, result.writes_made, result.retryable) == ("amend", "forward", True, True)
    assert lane.publish.attempt.phase == "amend" and w.driver_ws.calls == []
    view = w.h.service.lane_view(lane)
    assert view.state == "recovering"
    exc = await expect(ErrorCode.BOARD_PUBLISH_IN_PROGRESS, w.move(["o2"], "T1", lanes=("T1",), target={"load_id": load_id, "index": 2}))
    assert exc.details["reason"] == "recovery_pending"
    calls = []
    original = pub.plan_changes
    monkeypatch.setattr(pub, "plan_changes", lambda *a, **k: calls.append(1) or original(*a, **k))
    attempt_id = lane.publish.attempt.attempt_id
    await w.run("T1", reasons={})
    assert calls == []  # no reclassification
    lane = w.lane_doc("T1")
    assert lane.publish.state == "published" and lane.publish.attempt is None
    assert [c[0] for c in w.driver_ws.calls] == ["new_route"]
    route = w.route(plan.route_id)
    assert [s["order_ids"][0] for s in sorted(route["stops"], key=lambda s: s["sequence"])] == ["o1", "o3", "o2"]
    assert lane.publish.last_result.publish_id != attempt_id


async def test_forward_pending_logs_warning_until_retry(caplog):
    w = World(forward_warning_interval_s=0.01)
    load_id = await publish_a(w, "o1", "o2")
    await w.driver_start("o1")
    await w.move(["o2"], "T1", lanes=("T1",), target={"load_id": load_id, "index": 0})
    plan = w.plan_of("T1")
    w.store.fail_on("atomic_update", EXECUTIONS, w.executions_of(plan.plan_id)[0]["execution_id"], times=4)
    with caplog.at_level(logging.WARNING, logger="fuel.services.dispatch_board_publish"):
        await w.run("T1", reasons=await w.reasons_for("T1"))
        await asyncio.sleep(0.05)
    assert any("still pending" in r.getMessage() for r in caplog.records)
    await w.run("T1", reasons={})
    await asyncio.sleep(0.03)
    assert not w.redispatch._tasks  # the watcher stops once the attempt completes
    await w.redispatch.close()


async def test_e30_retry_must_cover_the_whole_group(w):
    await publish_a(w, "o1", "o2")
    await w.driver_start("o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    plan = w.plan_of("T1")
    w.store.fail_on("atomic_update", EXECUTIONS, w.executions_of(plan.plan_id)[0]["execution_id"], times=4)
    await w.run("T2", reasons=await w.reasons_for("T2"))
    assert w.lane_doc("T2").publish.attempt.recovery == "forward"
    exc = await expect(ErrorCode.BOARD_PUBLISH_NOT_READY, w.start("T2", reasons={}))
    assert exc.details["reason"] == "retry_whole_group" and set(exc.details["truck_ids"]) == {"T1", "T2"}
    await w.run("T2", "T1", reasons={})
    assert w.lane_doc("T1").publish.state == w.lane_doc("T2").publish.state == "published"


async def test_dry_run_of_a_recovery_group_lists_each_load_under_its_own_truck(w):
    """Review P2-4: a recovery attempt's loads map to their lanes through the draft."""
    await publish_a(w, "o1", "o2")
    await w.driver_start("o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    plan = w.plan_of("T1")
    w.store.fail_on("atomic_update", EXECUTIONS, w.executions_of(plan.plan_id)[0]["execution_id"], times=4)
    await w.run("T2", reasons=await w.reasons_for("T2"))
    assert w.lane_doc("T2").publish.attempt.recovery == "forward"
    preview = await w.dry("T2", "T1")
    by_load = {entry["load_id"]: entry["truck_id"] for entry in preview["loads"]}
    assert by_load == {w.lane_doc("T1").loads[0].load_id: "T1", w.lane_doc("T2").loads[0].load_id: "T2"}


async def test_replay_of_a_redispatch_publish_rebuilds_its_groups(w):
    """Review P2-3: the replayed 202 carries ``groups`` like the first answer (K7.1)."""
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    cid = "5b0c7f0e-6c55-4c1f-9d7e-6a4b8f3c2d10"
    first = await w.run("T2", cid=cid)
    replay = await w.start("T2", cid=cid)
    assert replay["replayed"] is True and replay["publish_id"] == first["publish_id"]
    assert [(sorted(g["truck_ids"]), g["kind"]) for g in replay["groups"]] == [(["T1", "T2"], "redispatch")]
    assert [(sorted(g["truck_ids"]), g["kind"]) for g in first["groups"]] == [(["T1", "T2"], "redispatch")]


# ---- (f), (g): moves across lanes --------------------------------------------


async def test_f_move_into_never_published_lane_publishing_only_that_lane(w):
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    preview = await w.dry("T2")
    assert preview["groups"] == [{"truck_ids": ["T2", "T1"], "kind": "redispatch", "added_lanes": ["T1"]}]
    assert not any("order_committed_elsewhere" in e["reasons"] for e in preview["not_ready"])
    w.driver_ws.calls.clear()
    await w.run("T2")
    b_plan, a_plan = w.plan_of("T2"), w.plan_of("T1")
    assert w.links("o1") == (b_plan.plan_id, "T2", "d2") and w.order("o1")["status"] == "dispatched"
    assert a_plan.revision == 2 and [a["order_id"] for a in w.plan(a_plan.plan_id)["assignments"]] == ["o2"]
    revoked = w.driver_ws.of("assignment_revoked")
    assert revoked == [("assignment_revoked", "d1", {"order_ids": ["o1"], "plan_ids": ["bp-L1-r1"], "reason": "reassigned_by_dispatcher", "service_date": TODAY.isoformat()})]
    assigned = {c[1]: c[2]["order_ids"] for c in w.driver_ws.of("assignment")}
    assert assigned == {"d2": ["o1"], "d1": ["o2"]}
    events = [e for e in w.store.events("o1") if e["event_type"] == "order_reassigned"]
    assert len(events) == 1 and events[0]["event_payload"]["to"]["truck_id"] == "T2"


async def test_g_move_into_new_load_on_published_lane(w):
    await publish_a(w, "o1", "o2")
    await publish_a(w, "o5", truck="T3", driver="d3")
    await w.move(["o1"], "T3", lanes=("T1", "T3"), target={"load_id": "new"})
    response = await w.run("T3")
    assert response["groups"][0]["truck_ids"] == ["T3", "T1"]
    lane = w.lane_doc("T3")
    new_load = lane.loads[1].load_id
    assert w.links("o1") == (pub.board_ids(new_load, 1)[0], "T3", "d3")
    assert w.plan_of("T3", 0).revision == 1  # C's first load is unchanged
    assert w.order("o5")["assigned_run_id"] == w.plan_of("T3", 0).run_id


# ---- (h), freeze rule 9: check-ins vs retire --------------------------------


async def test_h_freeze_rule_9_checkin_before_retire_rolls_back(w):
    load_id = await publish_a(w, "o1", "o2")
    plan = w.plan_of("T1")
    await w.move(["o2"], "T1", lanes=("T1",), target={"load_id": load_id, "index": 0})
    before = operational_docs(w)
    on_phase(w, {"retire": lambda: checkin(w, plan, "o1")})
    w.driver_ws.calls.clear()
    await w.run("T1")
    lane = w.lane_doc("T1")
    result = lane.publish.last_result
    assert (result.stage, result.reason, result.rolled_back, result.writes_made) == ("retire", "load_started_concurrently", True, False)
    assert lane.publish.attempt is None and w.driver_ws.calls == []
    assert w.plan(plan.plan_id)["status"] == "dispatched"
    execution = w.executions_of(plan.plan_id)[0]
    assert execution["completed_stops"] == 1 and execution["status"] == "in_progress"
    staged = w.plan(pub.board_ids(load_id, 2)[0])
    assert staged is None or staged["status"] == "superseded"
    after = operational_docs(w)
    changed = {k for k in after if before.get(k) != after[k]}
    assert all(k.startswith(EXECUTIONS) or k.startswith(PLANS + "/" + pub.board_ids(load_id, 2)[0]) or k.startswith(ROUTES + "/" + pub.board_ids(load_id, 2)[1]) for k in changed)
    # Retry: the load is started now, so it amends.
    await w.run("T1")
    assert w.lane_doc("T1").publish.state == "published" and w.plan_of("T1").plan_id == plan.plan_id


async def test_h_freeze_rule_9_checkin_after_retire_is_refused(w):
    load_id = await publish_a(w, "o1", "o2")
    plan = w.plan_of("T1")
    await w.move(["o2"], "T1", lanes=("T1",), target={"load_id": load_id, "index": 0})
    errors = []

    async def late_checkin():
        try:
            await checkin(w, plan, "o1")
        except ValueError as exc:
            errors.append(str(exc))

    on_phase(w, {"stage_relink": late_checkin})
    await w.run("T1")
    assert errors and "not in 'dispatched' status (current: superseded)" in errors[0]
    assert w.lane_doc("T1").publish.state == "published" and w.plan_of("T1").revision == 2
    assert w.executions_of(plan.plan_id)[0]["completed_stops"] == 0


# ---- (j), freeze rules 10 and 11 (e): driver start between retire and relink --


async def _start_between_retire_and_relink(w: World):
    """A [o1, o2, o3]; o3 then o1 moved to never-published B; the driver starts o1
    after retire and before its relink (gate read first, write after retire)."""
    load_id = await publish_a(w, "o1", "o2", "o3")
    plan = w.plan_of("T1")
    await w.lane("T2", driver_id="d2")
    await w.move(["o3"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": w.lane_doc("T2").loads[0].load_id, "index": 1})
    gate = DriverTransitionGateStack(board_plan_reader=w.store.get_document)
    seen: Dict[str, Any] = {}

    async def gate_read():
        seen["order"] = await w.repo.get_current(T, "o1")
        await gate.evaluate(tenant_id=T, driver_id="d1", order=seen["order"], target_status="in_transit")

    async def driver_write():
        await w.order_service.apply_status_transition(
            order=seen["order"], new_status="in_transit", actor_user_id="d1", guard_stored_state=True,
        )

    on_phase(w, {"retire": gate_read, "stage_relink": driver_write})
    return load_id, plan


async def test_j_freeze_rule_10_driver_start_before_relink_rolls_back_cleanly(w):
    load_id, plan = await _start_between_retire_and_relink(w)
    w.driver_ws.calls.clear()
    await w.run("T2")
    assert w.order("o1")["status"] == "in_transit"
    for truck in ("T1", "T2"):
        result = w.lane_doc(truck).publish.last_result
        assert (result.stage, result.rolled_back, result.order_id, result.observed_status) == ("relink", True, "o1", "in_transit")
        assert w.lane_doc(truck).publish.attempt is None
    execution = w.executions_of(plan.plan_id)[0]
    assert execution["status"] == "in_progress" and "superseded_by_attempt" not in execution
    assert w.plan(plan.plan_id)["status"] == "dispatched" and w.route(plan.route_id)["status"] == "dispatched"
    assert w.links("o3") == (plan.run_id, "T1", "d1")  # relinked first, then put back
    assert w.links("o1") == (plan.run_id, "T1", "d1")
    b_load = w.lane_doc("T2").loads[0].load_id
    for staged in (pub.board_ids(load_id, 2), pub.board_ids(b_load, 1)):
        assert w.plan(staged[0])["status"] == "superseded" and w.route(staged[1])["status"] == "superseded"
    assert w.driver_ws.calls == []
    read = await w.work_read("d1", "o1")
    assert read["manifest_available"] is True and read["route_available"] is True
    assert (await checkin(w, plan, "o1"))["completed_stops"] == 1
    # Freeze rule 11 (e): B shows the move-back block; the move back commits; A then amends.
    # (The Phase 3 order listener marks the lane stale; here the 60 s check age does.)
    w.h.now = w.h.now + timedelta(seconds=61)
    snap = await w.h.service.snapshot(T, TODAY, mode="active_gated", tz="America/Chicago")
    b_view = next(l for l in snap["lanes"] if l["truck_id"] == "T2")
    pinned = [c for c in b_view["checks"] if c["reason_code"] == "stop_pinned"]
    assert pinned and pinned[0]["message"] == "Started on T1. Move it back."
    assert pinned[0]["fix_link"] == {"kind": "move_back", "id": "o1", "truck_id": "T1", "load_id": load_id}
    await w.move(["o1"], "T1", lanes=("T1", "T2"), target={"load_id": load_id, "index": 1})
    w.driver_ws.calls.clear()
    await w.run("T1")
    assert w.lane_doc("T1").publish.state == "published" and w.plan_of("T1").plan_id == plan.plan_id
    assert w.plan_of("T1").revision == 2  # amended in place
    assert w.links("o3")[1:] == ("T2", "d2")


async def test_j_variant_move_back_commits_with_a_persistent_block_on_a(w):
    load_id, _plan = await _start_between_retire_and_relink(w)
    await w.run("T2")
    w.h.qualification.ineligible = {"d1": ["medical_card_expired"]}
    w.h.validation.cache.clear()
    await w.move(["o1"], "T1", lanes=("T1", "T2"), target={"load_id": load_id, "index": 1})
    lane = w.lane_doc("T1")
    assert "o1" in [s.order_id for s in lane.loads[0].stops]
    assert any(c.check == "driver_qualification" and c.outcome == "block" for c in lane.checks)


# ---- (k), freeze rule 11 (d): crashes --------------------------------------


async def test_k_crash_during_stage_relink_rolls_back_first_then_publishes_fresh(w):
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    reasons = await w.reasons_for("T2")
    on_phase(w, {"stage_relink": crash})
    await run_crashing(w, "T2", reasons=reasons)
    assert w.lane_doc("T2").publish.state == "publishing"
    expire(w)
    assert w.h.service.lane_view(w.lane_doc("T2")).state == "recovering"
    await expect(ErrorCode.BOARD_PUBLISH_IN_PROGRESS, w.move(["o1"], "T1", lanes=("T1", "T2"), target={"load_id": w.lane_doc("T1").loads[0].load_id}))
    await expect(ErrorCode.BOARD_PUBLISH_NOT_READY, w.start("T2", reasons=reasons))
    await w.run("T2", "T1", reasons=reasons)
    assert w.lane_doc("T2").publish.state == w.lane_doc("T1").publish.state == "published"
    assert w.links("o1")[1:] == ("T2", "d2") and w.order("o1")["status"] == "dispatched"
    b_plan = w.plan_of("T2")
    assert len(w.executions_of(b_plan.plan_id)) == 1


async def test_k_crash_during_apply_resumes_forward_without_duplicates(w, monkeypatch):
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    reasons = await w.reasons_for("T2")
    original = w.dispatch.dispatch
    calls = {"n": 0}

    async def dispatch_then_crash(**kw):
        result = await original(**kw)
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(w.dispatch, "dispatch", dispatch_then_crash)
    await run_crashing(w, "T2", reasons=reasons)
    assert w.lane_doc("T2").publish.attempt.phase == "apply"
    expire(w)
    w.driver_ws.calls.clear()
    plan_changes_calls = []
    real = pub.plan_changes
    monkeypatch.setattr(pub, "plan_changes", lambda *a, **k: plan_changes_calls.append(1) or real(*a, **k))
    await w.run("T2", "T1", reasons={})
    assert plan_changes_calls == []
    assert w.lane_doc("T2").publish.state == "published"
    for truck in ("T1", "T2"):
        plan = w.plan_of(truck)
        assert len(w.executions_of(plan.plan_id)) == 1
    assert sorted(c[1] for c in w.driver_ws.of("assignment")) == ["d1", "d2"]


async def test_k_freeze_rule_11_d_crash_before_notify_resends_documents_payload_once(w):
    reference = World()
    for world, crash_it in ((reference, False), (w, True)):
        await publish_a(world, "o1", "o2")
        await world.lane("T2", driver_id="d2")
        await world.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
        world.driver_ws.calls.clear()
        reasons = await world.reasons_for("T2")
        if crash_it:
            on_phase(world, {"notify": crash})
            await run_crashing(world, "T2", reasons=reasons)
            assert world.driver_ws.calls == []
            expire(world)
            await world.run("T2", "T1", reasons={})
        else:
            await world.run("T2", reasons=reasons)
    assert w.driver_ws.calls == reference.driver_ws.calls
    assert len(w.driver_ws.of("assignment")) == 2


async def test_forward_resume_never_reruns_relinks_after_driver_started_relinked_order(w, monkeypatch):
    """Phase 0 review issue 3: a relink after start answers ``refused``; a resume
    from apply on never replays phase 2, so it can't misread that as a failure."""
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    original = w.dispatch.dispatch
    calls = {"n": 0}

    async def dispatch_then_crash(**kw):
        result = await original(**kw)
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(w.dispatch, "dispatch", dispatch_then_crash)
    await run_crashing(w, "T2", reasons=await w.reasons_for("T2"))
    assert w.plan(w.lane_doc("T2").publish.attempt.loads[w.lane_doc("T2").loads[0].load_id].plan_id)["status"] == "dispatched"
    await w.driver_start("o1")  # B's plan is dispatched: the driver legitimately starts o1
    relinks = []
    real_relink = w.repo.relink_dispatched_assignment

    async def relink_spy(*a, **k):
        relinks.append(a)
        return await real_relink(*a, **k)

    monkeypatch.setattr(w.repo, "relink_dispatched_assignment", relink_spy)
    assert await w.repo.relink_dispatched_assignment(
        T, "o1", from_run_id="bp-L1-r1", from_asset_id="T1", from_driver_id="d1",
        to_run_id=w.links("o1")[0], to_asset_id="T2", to_driver_id="d2", claim_id="x",
    ) == "refused"  # the primitive's answer for an in-transit order already on its target
    relinks.clear()
    expire(w)
    await w.run("T2", "T1", reasons={})
    assert relinks == []
    assert w.lane_doc("T1").publish.state == w.lane_doc("T2").publish.state == "published"
    assert w.order("o1")["status"] == "in_transit"


# ---- (l) idempotency of recovery ----------------------------------------------


async def test_l_rollback_and_forward_completion_are_idempotent(w):
    load_id, plan = await _start_between_retire_and_relink(w)
    captured: Dict[str, Any] = {}
    original = w.redispatch.rollback

    async def capture(run, *, failure):
        captured["run"] = run
        return await original(run, failure=failure)

    w.redispatch.rollback = capture  # type: ignore[assignment]
    await w.run("T2")
    once = operational_docs(w)
    run = captured["run"]

    async def no_lease(_run):  # the steps re-run directly, outside the (released) lease
        return None

    w.redispatch._renew = no_lease  # type: ignore[assignment]
    assert await w.redispatch._relink_back(run) is False
    await w.redispatch._retire_staged(run)
    await w.redispatch._restore_retired(run)
    assert operational_docs(w) == once
    # Forward completion twice: re-running apply and amend on a finished attempt changes nothing.
    w2 = World()
    load2 = await publish_a(w2, "o1", "o2", "o3")
    await w2.driver_start("o1")
    await w2.move(["o3"], "T1", lanes=("T1",), target={"load_id": load2, "index": 1})
    runs: Dict[str, Any] = {}
    real_finalize = w2.redispatch._finalize

    async def keep(run):
        runs["run"] = run
        await real_finalize(run)

    w2.redispatch._finalize = keep  # type: ignore[assignment]
    await w2.run("T1")
    done = operational_docs(w2)
    run2 = runs["run"]
    w2.redispatch._renew = no_lease  # type: ignore[assignment]
    await w2.redispatch._apply(run2)
    await w2.redispatch._amend(run2)
    assert operational_docs(w2) == done


# ---- (m) gate and guard, P12 ------------------------------------------------


async def test_m_gate_and_guard_around_phase_2_and_3(w):
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    gate = DriverTransitionGateStack(board_plan_reader=w.store.get_document)
    seen: Dict[str, Any] = {}
    outcomes: List[str] = []

    async def before_relink():
        seen["stale"] = await w.repo.get_current(T, "o1")

    async def after_relink():
        try:
            await w.order_service.apply_status_transition(order=copy.deepcopy(seen["stale"]), new_status="in_transit", guard_stored_state=True)
        except OrderChangedConcurrentlyError:
            outcomes.append("ORDER_CHANGED_CONCURRENTLY")
        fresh = await w.repo.get_current(T, "o1")
        try:
            await gate.evaluate(tenant_id=T, driver_id="d2", order=fresh, target_status="in_transit")
        except AppException as exc:
            outcomes.append(exc.error_code.value if hasattr(exc.error_code, "value") else str(exc.error_code))

    async def after_apply():
        fresh = await w.repo.get_current(T, "o1")
        await gate.evaluate(tenant_id=T, driver_id="d2", order=fresh, target_status="in_transit")
        await w.order_service.apply_status_transition(order=fresh, new_status="in_transit", guard_stored_state=True)
        outcomes.append("started")

    on_phase(w, {"stage_relink": before_relink, "apply": after_relink, "amend": after_apply})
    await w.run("T2")
    assert outcomes == ["ORDER_CHANGED_CONCURRENTLY", "BOARD_ROUTE_UPDATING", "started"]
    assert w.order("o1")["status"] == "in_transit" and w.lane_doc("T2").publish.state == "published"


def _p12_invariants(w: World) -> None:
    plans = w.store.docs[PLANS]
    routes = w.store.docs[ROUTES]
    for index in (PLANS, ROUTES, EXECUTIONS):
        for doc in w.store.docs[index].values():
            if doc.get("superseded_by_attempt"):
                assert doc["status"] == "superseded"
    for order_id, order in w.store.docs[ORDERS].items():
        if order["status"] in ("cancelled", "failed", "delivered") or not order.get("assigned_run_id"):
            continue
        plan = plans.get(order["assigned_run_id"])
        assert plan is not None and plan["status"] in ("dispatched", "scheduled", "completed"), (order_id, plan and plan["status"])
        route_ids = [r["route_id"] for r in routes.values() if r.get("plan_id") == plan["plan_id"]]
        assert any(order_id in (s.get("order_ids") or []) for r in route_ids for s in routes[r]["stops"])


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    fail_at=st.one_of(st.none(), st.integers(1, 30)),
    forward_hard=st.booleans(),
    driver_at=st.sampled_from([None, "retire", "stage_relink", "apply", "amend"]),
    driver_order=st.sampled_from(["o1", "o2", "o3"]),
    driver_action=st.sampled_from(["in_transit", "failed"]),
)
def test_p12_recovery_over_failure_points_and_admitted_driver_actions(fail_at, forward_hard, driver_at, driver_order, driver_action):
    async def scenario() -> None:
        w = World()
        await publish_a(w, "o1", "o2", "o3")
        await w.lane("T2", driver_id="d2")
        await w.move(["o2", "o3"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
        reasons = await w.reasons_for("T2")
        gate = DriverTransitionGateStack(board_plan_reader=w.store.get_document)

        async def driver():
            order = await w.repo.get_current(T, driver_order)
            if order["status"] != "dispatched":
                return
            try:
                if driver_action == "in_transit":
                    await gate.evaluate(tenant_id=T, driver_id=order.get("assigned_driver_id"), order=order, target_status="in_transit")
                await w.order_service.apply_status_transition(order=order, new_status=driver_action, guard_stored_state=True)
            except (AppException, OrderChangedConcurrentlyError):
                pass

        if driver_at:
            on_phase(w, {driver_at: driver})
        counter = {"n": 0}

        def faults(op, index, doc_id):
            if op in ("atomic_update", "index_document", "update_document") and index in OPERATIONAL:
                counter["n"] += 1
                if fail_at is not None and fail_at <= counter["n"] < fail_at + (4 if forward_hard else 1):
                    raise RuntimeError("injected")

        w.store.hooks.append(faults)
        await w.run("T2", reasons=reasons)
        w.store.hooks.clear()
        w.redispatch.on_phase = None
        assert ("dispatch_board.recovery_inconsistent", 1.0) not in [(m[0], m[1]) for m in w.h.telemetry.metrics]
        for _ in range(3):
            lanes = [w.lane_doc(t) for t in ("T1", "T2")]
            if all(l.publish.attempt is None for l in lanes):
                break
            expire(w)
            try:
                await w.run("T2", "T1", reasons={})
            except AppException as exc:
                # The Retry rolled back first; the fresh publish that follows may be
                # not ready (a started order now pinned on B must be moved back).
                assert exc.error_code == ErrorCode.BOARD_PUBLISH_NOT_READY, exc.details
        assert all(w.lane_doc(t).publish.attempt is None for t in ("T1", "T2"))
        _p12_invariants(w)

    asyncio.run(scenario())


# ---- (n) drop rule, (o) relink from recorded link -----------------------------


async def test_n_order_cancelled_between_executor_and_dispatch_is_dropped(w, monkeypatch):
    load_id = await publish_a(w, "o1", "o2")
    w.seed("o5")
    await w.command("assign_orders", lanes=("T1",), order_ids=["o5"], truck_id="T1", target={"load_id": load_id})
    original = w.dispatch.dispatch
    state = {"done": False}

    async def cancel_first(**kw):
        if not state["done"]:
            state["done"] = True
            order = await w.repo.get_current(T, "o5")
            await w.order_service.apply_status_transition(order=order, new_status="cancelled", reason="customer", guard_stored_state=True)
        return await original(**kw)

    monkeypatch.setattr(w.dispatch, "dispatch", cancel_first)
    await w.run("T1")
    lane = w.lane_doc("T1")
    assert lane.publish.state == "published" and lane.publish.last_result.dropped_orders == ["o5"]
    route = w.route(w.plan_of("T1").route_id)
    assert [s["sequence"] for s in route["stops"]] == [1, 2] and all("o5" not in s["order_ids"] for s in route["stops"])
    view = w.h.service.lane_view(lane)
    assert view.state == "modified"
    assert {w.order(o)["status"] for o in ("o1", "o2")} == {"dispatched"}


async def test_n_tray_order_changed_after_claim_is_dropped_and_executor_reruns(w):
    load_id = await publish_a(w, "o1", "o2")
    w.seed("o5")
    await w.command("assign_orders", lanes=("T1",), order_ids=["o5"], truck_id="T1", target={"load_id": load_id})
    on_phase(w, {"apply": lambda: w.store.poke(ORDERS, "o5", gallons_requested=999.0)})
    await w.run("T1")
    lane = w.lane_doc("T1")
    assert lane.publish.state == "published" and lane.publish.last_result.dropped_orders == ["o5"]
    plan = w.plan(w.plan_of("T1").plan_id)
    assert sorted(a["order_id"] for a in plan["assignments"]) == ["o1", "o2"]
    assert w.order("o5")["status"] == "confirmed" and w.order("o5")["assigned_run_id"] is None


async def test_o_freeze_rule_2_relink_from_recorded_published_link_only(w, monkeypatch):
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    calls = []
    real = w.repo.relink_dispatched_assignment

    async def spy(tenant_id, order_id, **kw):
        calls.append((order_id, kw))
        return await real(tenant_id, order_id, **kw)

    monkeypatch.setattr(w.repo, "relink_dispatched_assignment", spy)
    on_phase(w, {"stage_relink": lambda: w.store.poke(ORDERS, "o1", assigned_driver_id="d3")})
    await w.run("T2")
    order_id, kw = calls[0]
    assert (order_id, kw["from_run_id"], kw["from_asset_id"], kw["from_driver_id"]) == ("o1", "bp-L1-r1", "T1", "d1")
    assert w.order("o1")["status"] == "dispatched" and w.links("o1") == ("bp-L1-r1", "T1", "d3")
    result = w.lane_doc("T2").publish.last_result
    assert result.stage in ("relink", "restore")
    assert w.lane_doc("T2").publish.state == "failed"
    # An outside writer changed the link: the rollback says so loudly instead of guessing.
    assert ("dispatch_board.recovery_inconsistent", 1.0) in [(m[0], m[1]) for m in w.h.telemetry.metrics]


# ---- remaining freeze rules ---------------------------------------------------


async def test_freeze_rule_1_no_dispatched_to_scheduled_path(w):
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    mark = {o: len(w.store.events(o)) for o in ("o1", "o2")}
    await w.run("T2")
    for order_id, n in mark.items():
        new_events = [e["event_type"] for e in sorted(w.store.events(order_id), key=lambda e: e["event_timestamp"])][n:]
        assert "order_scheduled" not in new_events and w.order(order_id)["status"] == "dispatched"


async def test_freeze_rule_3_pinned_stop_and_started_allocations_unchanged(w):
    load_id = await publish_a(w, "o1", "o2", "o3")
    plan = w.plan_of("T1")
    before = {a["order_id"]: a for a in w.plan(plan.plan_id)["assignments"]}
    await w.driver_start("o1")
    record_before = next(s for s in w.executions_of(plan.plan_id)[0]["stops"] if s["sequence"] == 1)
    await w.move(["o3"], "T1", lanes=("T1",), target={"load_id": load_id, "index": 1})
    await w.run("T1")
    after = {a["order_id"]: a for a in w.plan(plan.plan_id)["assignments"]}
    assert after == before
    record_after = next(s for s in w.executions_of(plan.plan_id)[0]["stops"] if s["station_id"] == record_before["station_id"])
    assert record_after == record_before
    shares = [{"compartment_id": "C2", "liters": round(500 * 3.785411784, 2)}]
    exc = await expect(
        ErrorCode.BOARD_COMMAND_BLOCKED,
        w.command("set_allocation", lanes=("T1",), load_id=load_id, order_id="o2", shares=shares),
    )
    assert exc.details["reason"] == "load_started"


async def test_freeze_rule_5_group_processed_sequentially(w, monkeypatch):
    await publish_a(w, "o1", "o2")
    await publish_a(w, "o5", truck="T3", driver="d3")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    await w.move(["o5"], "T2", lanes=("T2", "T3"), target={"load_id": w.lane_doc("T2").loads[0].load_id})
    active = {"now": 0, "max": 0}
    real = w.executor.execute

    async def spy(**kw):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        try:
            await asyncio.sleep(0)
            return await real(**kw)
        finally:
            active["now"] -= 1

    monkeypatch.setattr(w.executor, "execute", spy)
    response = await w.run("T2")
    assert set(response["groups"][0]["truck_ids"]) == {"T1", "T2", "T3"}
    assert active["max"] == 1


def _record_outcomes(w: World, monkeypatch) -> List[str]:
    outcomes: List[str] = []
    real = w.redispatch.run_group

    async def spy(**kw):
        outcome = await real(**kw)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(w.redispatch, "run_group", spy)
    return outcomes


async def test_freeze_rule_5_slow_apply_renews_the_lease_so_a_retry_is_refused(w, monkeypatch):
    """Review P2-1: a live worker renews before each load, so a phase longer
    than the lease never reads as recovering and a Retry can't start a second actor."""
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    reasons = await w.reasons_for("T2")
    real = w.executor.execute
    seen: List[Any] = []

    async def slow(**kw):
        w.advance(100)  # two loads: 200 s inside one apply phase, past the 120 s lease
        if len(seen) == 1:
            lane = w.lane_doc("T2")
            seen.append((lane.publish.state, w.h.service.lane_view(lane).state))
            seen.append(await expect(ErrorCode.BOARD_PUBLISH_IN_PROGRESS, w.start("T2", "T1", reasons={})))
        else:
            seen.append(None)
        return await real(**kw)

    monkeypatch.setattr(w.executor, "execute", slow)
    await w.run("T2", reasons=reasons)
    assert seen[1] == ("publishing", "publishing")
    assert seen[2].details["reason"] == "publishing"
    assert w.lane_doc("T1").publish.state == w.lane_doc("T2").publish.state == "published"
    assert sorted(c[1] for c in w.driver_ws.of("assignment")) == ["d1", "d1", "d2"]  # d1 once at publish_a


async def test_freeze_rule_5_worker_past_its_lease_stops_after_a_takeover(w, monkeypatch):
    """Review P2-1: a worker that ran past its lease and lost the group to a Retry
    stops at its next renewal; only the new owner finishes, without duplicates."""
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    reasons = await w.reasons_for("T2")
    outcomes = _record_outcomes(w, monkeypatch)
    real = w.executor.execute
    taken: List[str] = []

    async def stall_then_takeover(**kw):
        if not taken:
            taken.append("x")
            expire(w)
            await w.start("T2", "T1", reasons={})  # accepted: the lease has passed
        return await real(**kw)

    monkeypatch.setattr(w.executor, "execute", stall_then_takeover)
    w.driver_ws.calls.clear()
    await w.run("T2", reasons=reasons)
    await w.publish.wait_idle()
    assert sorted(outcomes) == ["lease_lost", "published"]
    for truck in ("T1", "T2"):
        lane = w.lane_doc(truck)
        assert lane.publish.state == "published" and lane.publish.attempt is None
        assert len(w.executions_of(w.plan_of(truck).plan_id)) == 1
    assert sorted(c[1] for c in w.driver_ws.of("assignment")) == ["d1", "d2"]
    assert len(w.driver_ws.of("assignment_revoked")) == 1


async def test_freeze_rule_5_lost_lease_stops_relinks_before_the_next_one(w, monkeypatch):
    """Review P2-1: renewal before each relink, so a superseded worker can't land
    a late relink after the new owner's relink-back."""
    await publish_a(w, "o1", "o2", "o3")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1", "o2"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    reasons = await w.reasons_for("T2")
    outcomes = _record_outcomes(w, monkeypatch)
    real = w.repo.relink_dispatched_assignment
    calls: List[str] = []

    async def relink_then_lose_lease(*a, **k):
        calls.append(a[1])
        result = await real(*a, **k)
        doc = w.store.docs[DRAFTS][f"{T}:{TODAY.isoformat()}"]
        for truck in ("T1", "T2"):
            doc["lanes"][truck]["publish"]["attempt_id"] = "another-publish"
        return result

    monkeypatch.setattr(w.repo, "relink_dispatched_assignment", relink_then_lose_lease)
    await w.run("T2", reasons=reasons)
    assert len(calls) == 1 and outcomes == ["lease_lost"]
    assert w.lane_doc("T2").publish.attempt.phase == "stage_relink"


async def test_freeze_rule_6_not_started_change_is_a_new_revision_never_an_amend(w):
    load_id = await publish_a(w, "o1", "o2")
    await w.move(["o2"], "T1", lanes=("T1",), target={"load_id": load_id, "index": 0})
    await w.run("T1")
    assert w.plan_of("T1").plan_id == pub.board_ids(load_id, 2)[0]
    assert w.route(pub.board_ids(load_id, 1)[1])["revision"] == 1  # the old route was never rewritten
    assert [s["sequence"] for s in w.route(pub.board_ids(load_id, 2)[1])["stops"]] == [1, 2]


async def test_freeze_rule_10_rollback_restores_exact_pre_attempt_documents(w):
    load_id = await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    before = operational_docs(w)
    b_load = w.lane_doc("T2").loads[0].load_id
    w.store.fail_on("atomic_update", ORDERS, "o1", exc=RuntimeError("store down"))
    await w.run("T2")
    assert w.lane_doc("T2").publish.last_result.rolled_back is True
    after = operational_docs(w)
    staged = {f"{PLANS}/{pub.board_ids(load_id, 2)[0]}", f"{ROUTES}/{pub.board_ids(load_id, 2)[1]}",
              f"{PLANS}/{pub.board_ids(b_load, 1)[0]}", f"{ROUTES}/{pub.board_ids(b_load, 1)[1]}"}
    assert {k: v for k, v in after.items() if k not in staged} == before
    assert all(after[k]["status"] == "superseded" for k in staged)


async def test_freeze_rule_11_c_removing_the_only_pending_stop_finishes_the_load(w):
    await publish_a(w, "o1", "o2")
    plan = w.plan_of("T1")
    await w.driver_start("o1")
    await checkin(w, plan, "o1")
    w.store.poke(ORDERS, "o1", status="delivered")
    await w.lane("T2", driver_id="d2")
    await w.move(["o2"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    await w.run("T2")
    execution = w.executions_of(plan.plan_id)[0]
    assert execution["status"] == "completed" and execution["total_stops"] == 1 and execution["completed_stops"] == 1
    assert w.plan(plan.plan_id)["status"] == "completed"
    assert [d for d in w.store.docs["mvp_plan_outcomes"].values() if d.get("plan_id") == plan.plan_id]
    read = await w.work_read("d1", "o1")
    assert [s["status"] for s in read["stops"]] == ["completed"] and read["manifest_available"] is True
    assert w.links("o2")[1:] == ("T2", "d2")


# ---- driver changes, notification failure, kept completed stop ---------------


async def test_driver_change_on_not_started_load_is_a_new_revision(w):
    load_id = await publish_a(w, "o1", "o2")
    await w.command("pair_driver", lanes=("T1",), truck_id="T1", driver_id="d3")
    w.driver_ws.calls.clear()
    await w.run("T1")
    new = w.plan_of("T1")
    assert new.plan_id == pub.board_ids(load_id, 2)[0]
    assert {w.links(o) for o in ("o1", "o2")} == {(new.plan_id, "T1", "d3")}
    assert w.driver_ws.of("assignment_revoked")[0][1:] == ("d1", {"order_ids": ["o1", "o2"], "plan_ids": ["bp-L1-r1"], "reason": "reassigned_by_dispatcher", "service_date": TODAY.isoformat()})
    assert [c[1] for c in w.driver_ws.of("assignment")] == ["d3"]


async def test_driver_change_on_started_load_is_blocked(w):
    await publish_a(w, "o1", "o2")
    await w.driver_start("o1")
    exc = await expect(ErrorCode.BOARD_COMMAND_BLOCKED, w.command("pair_driver", lanes=("T1",), truck_id="T1", driver_id="d3"))
    assert exc.details["reason"] == "load_started"


async def test_notification_failure_never_fails_the_publish(w):
    await publish_a(w, "o1", "o2")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    w.driver_ws.fail_for = {"d1"}
    await w.run("T2")
    assert w.lane_doc("T2").publish.state == "published"
    assert w.lane_doc("T1").publish.last_result.notifications_failed == ["d1"]


async def test_amend_keeps_a_stop_completed_concurrently(w):
    await publish_a(w, "o1", "o2", "o3")
    plan = w.plan_of("T1")
    await w.driver_start("o1")
    await w.lane("T2", driver_id="d2")
    await w.move(["o3"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    on_phase(w, {"amend": lambda: checkin(w, plan, "o3")})
    await w.run("T2")
    lane = w.lane_doc("T1")
    assert lane.publish.state == "published" and lane.publish.last_result.kept_completed_stops == ["o3"]
    execution = w.executions_of(plan.plan_id)[0]
    assert any(s["status"] == "completed" and s["station_id"] == "tank-o3" for s in execution["stops"])
