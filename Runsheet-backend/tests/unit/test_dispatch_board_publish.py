"""BoardPublishService (plan task 15; design K7.1-K7.5, K8.4 groups; P10, P11).

Runs the real loading-plan executor, plan dispatch, plan execution service and
order repository over the board's fake store (``_dispatch_board_publish_world``).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from Agents.support.mvp_es_mappings import MVP_LOAD_PLANS_MAPPING, MVP_ROUTES_MAPPING
from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services import dispatch_board_engine as engine
from fuel.services import dispatch_board_publish as pub
from fuel.services.dispatch_board_models import (
    BoardDraft,
    Lane,
    LaneContent,
    LanePublish,
    Load,
    OrderSnapshot,
    PublishedPlan,
    Stop,
)
from services.unit_conversion import GAL_TO_L
from tests.unit._dispatch_board_fakes import DRAFTS, ORDERS, T, TODAY, TZ
from tests.unit._dispatch_board_publish_world import EXECUTIONS, OPERATIONAL, PLANS, ROUTES, World
from tests.unit.test_mvp_mapping_board_fields import _covered, _declared, _paths


@pytest.fixture
def w() -> World:
    return World()


async def expect(code, coro):
    with pytest.raises(AppException) as info:
        await coro
    assert info.value.error_code == code, info.value.details
    return info.value


def assert_declared(doc, mapping):
    declared = _declared(mapping["mappings"]["properties"] if "mappings" in mapping else mapping["properties"])
    missing = [p for p in _paths(doc) if not _covered(p, declared)]
    assert missing == []


# ---- happy path and K7.3a shapes (P10) ------------------------------------


async def test_multi_load_publish_writes_k73a_documents_and_dispatches(w):
    w.seed("o1", "o2", "o3")
    await w.lane("T1", "o1", "o2", driver_id="d1")
    await w.command("assign_orders", lanes=("T1",), order_ids=["o3"], truck_id="T1", target={"load_id": "new"})
    lane = w.lane_doc("T1")
    assert len(lane.loads) == 2
    response = await w.run("T1")
    assert response["groups"] == [{"truck_ids": ["T1"], "kind": "first_publish", "added_lanes": []}]
    lane = w.lane_doc("T1")
    assert lane.publish.state == "published" and lane.publish.attempt is None and lane.publish.lease_until is None
    assert lane.publish.published_hash == engine.content_hash(lane)
    for seq, load in enumerate(lane.loads, start=1):
        plan_id, route_id = pub.board_ids(load.load_id, 1)
        assert lane.publish.plans[load.load_id] == PublishedPlan(plan_id=plan_id, route_id=route_id, run_id=plan_id, revision=1)
        plan, route = w.plan(plan_id), w.route(route_id)
        assert_declared(plan, MVP_LOAD_PLANS_MAPPING)
        assert_declared(route, MVP_ROUTES_MAPPING)
        assert plan["status"] == "dispatched" and plan["source"] == "dispatch_board" and plan["run_id"] == plan_id
        assert plan["board_draft_id"] == f"{T}:{TODAY.isoformat()}" and plan["board_load_id"] == load.load_id
        assert plan["load_seq"] == seq and plan["revision"] == 1 and plan["supersedes_plan_id"] is None
        assert plan["driver_id"] == "d1" and plan["service_date"] == TODAY.isoformat()
        stops = [s.order_id for s in load.stops]
        assert sorted(a["order_id"] for a in plan["assignments"]) == sorted(stops)
        for a in plan["assignments"]:
            assert a["station_id"] == f"cust-{a['order_id']}" and a["product_code"] == a["fuel_grade"] == "DIESEL_2"
        assert route["status"] == "dispatched" and route["plan_id"] == plan_id and route["run_id"] == plan_id
        assert [s["sequence"] for s in route["stops"]] == list(range(1, len(stops) + 1))
        assert [s["order_ids"] for s in route["stops"]] == [[o] for o in stops]
        assert [s["station_id"] for s in route["stops"]] == [f"tank-{o}" for o in stops]
        for stop in route["stops"]:
            assert "eta" not in stop or stop["eta"]  # omitted, never null, when unavailable
            planned = sum(a["quantity_liters"] for a in plan["assignments"] if a["order_id"] == stop["order_ids"][0])
            assert stop["drop"] == {"DIESEL_2": pytest.approx(planned)}
        assert len(w.executions_of(plan_id)) == 1
    for order_id in ("o1", "o2", "o3"):
        assert w.order(order_id)["status"] == "dispatched"
        assert w.links(order_id)[1:] == ("T1", "d1")
    assert {c[2]["plan_id"] for c in w.driver_ws.of("assignment")} == {pub.board_ids(l.load_id, 1)[0] for l in lane.loads}
    # The executor's action_id=None path writes no approval entry.
    assert w.store.docs["agent_approval_queue"] == {}


async def test_route_stop_eta_is_written_when_available(w):
    lane = Lane(truck_id="T1", version=1, driver_id="d1")
    load = Load(load_id="L9", stops=[Stop(order_id="o1", snapshot=OrderSnapshot(customer_id="c1"), eta=w.h.now)])
    doc = pub.build_route_doc(tenant_id=T, service_date=TODAY, lane=lane, load=load, revision=1, now=w.h.now)
    assert doc["stops"][0]["eta"] == w.h.now.isoformat() and doc["stops"][0]["station_id"] == "c1"
    assert_declared(doc, MVP_ROUTES_MAPPING)


async def test_dry_run_writes_nothing_and_returns_the_k71_shape(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    mark = len(w.store.ops)
    preview = await w.dry("T1")
    assert set(preview) >= {"ready", "not_ready", "groups", "loads", "notifications", "open_warnings"}
    assert preview["groups"] == [{"truck_ids": ["T1"], "kind": "first_publish", "added_lanes": []}]
    assert preview["loads"] == [{"truck_id": "T1", "load_id": w.lane_doc("T1").loads[0].load_id, "class": "new", "info": []}]
    assert preview["notifications"] == [
        {"driver_id": "d1", "name": "Driver d1", "revoke_order_ids": [], "assign_order_ids": ["o1"], "route_updated": False}
    ]
    assert [w_["truck_id"] for w_ in preview["open_warnings"]] == ["T1"]
    assert preview["ready"] is False and preview["not_ready"] == [{"truck_id": "T1", "reasons": ["warning_unacknowledged"]}]
    writes = [op for op in w.store.ops[mark:] if op[3] and op[0] in ("index_document", "update_document", "atomic_update", "create_document")]
    assert writes == []
    reasons = {x["warning_id"]: "Checked with ops" for x in preview["open_warnings"]}
    assert (await w.dry("T1", reasons=reasons))["ready"] is True


async def test_open_warning_without_reason_is_not_ready_and_reason_is_recorded_as_ack(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    exc = await expect(ErrorCode.BOARD_PUBLISH_NOT_READY, w.start("T1", ack=False))
    assert exc.details["lanes"] == [{"truck_id": "T1", "reasons": ["warning_unacknowledged"]}]
    assert w.lane_doc("T1").publish.state == "draft" and w.store.docs[PLANS] == {}
    reasons = await w.reasons_for("T1")
    await w.run("T1", reasons=reasons)
    acked = w.h.draft().acknowledged
    assert set(acked) == set(reasons) and all(a.actor_user_id == "user-1" and a.truck_id == "T1" for a in acked.values())


# ---- snapshot refresh (review finding 12) ---------------------------------


async def test_changed_gallons_publish_with_new_allocations(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    w.store.poke(ORDERS, "o1", gallons_requested=800.0)
    await w.run("T1")
    lane = w.lane_doc("T1")
    assert lane.loads[0].stops[0].snapshot.gallons_requested == 800.0
    plan = w.plan(pub.board_ids(lane.loads[0].load_id, 1)[0])
    total = sum(a["quantity_liters"] for a in plan["assignments"])
    assert total == pytest.approx(sum(a.liters for a in lane.loads[0].allocations))
    assert total >= 800 * GAL_TO_L - 1  # allocations recomputed from the new quantity, not the 500 gal snapshot
    assert w.order("o1")["status"] == "dispatched"


async def test_changed_product_blocks_with_order_identity_changed(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    w.store.poke(ORDERS, "o1", product_code="GASOLINE_REG")
    version = w.lane_doc("T1").version
    exc = await expect(ErrorCode.BOARD_PUBLISH_NOT_READY, w.start("T1", reasons={}))
    assert "order_identity_changed" in exc.details["lanes"][0]["reasons"]
    lane = w.lane_doc("T1")
    assert lane.version == version and lane.publish.state == "draft" and w.store.docs[PLANS] == {}


async def test_stale_allocation_override_blocks(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    load_id = w.lane_doc("T1").loads[0].load_id
    shares = [{"compartment_id": "C1", "liters": round(500 * GAL_TO_L, 2)}]
    await w.command("set_allocation", lanes=("T1",), load_id=load_id, order_id="o1", shares=shares)
    w.store.poke(ORDERS, "o1", gallons_requested=700.0)
    preview = await w.dry("T1")
    assert "override_stale" in preview["not_ready"][0]["reasons"]


# ---- failure stages, retry, lease ------------------------------------------


async def test_plan_write_failure_is_failed_without_writes(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    w.store.fail_on("atomic_update", PLANS)
    await w.run("T1")
    result = w.lane_doc("T1").publish.last_result
    assert w.lane_doc("T1").publish.state == "failed"
    assert (result.stage, result.reason, result.writes_made, result.retryable) == ("plan_write", "write_failed", False, True)
    assert w.order("o1")["status"] == "confirmed"


async def test_apply_failure_records_executor_reason_and_writes_made(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    seen = []

    def change_after_claim(op, index, doc_id):
        if (op, index) == ("atomic_update", PLANS) and not seen:
            seen.append(1)
            w.store.poke(ORDERS, "o1", gallons_requested=900.0)

    w.store.hooks.append(change_after_claim)
    await w.run("T1")
    result = w.lane_doc("T1").publish.last_result
    assert (result.stage, result.reason, result.writes_made) == ("apply", "order_changed_since_plan", False)
    assert result.failures and result.failures[0]["order_id"] == "o1"


async def test_dispatch_failure_then_retry_completes_without_duplicates(w):
    w.seed("o1", "o2")
    await w.lane("T1", "o1", "o2", driver_id="d1")
    reasons = await w.reasons_for("T1")

    def deactivate(op, index, doc_id):
        if (op, index) == ("atomic_update", PLANS):
            w.h.drivers.drivers["d1"]["status"] = "off_duty"

    w.store.hooks.append(deactivate)
    await w.run("T1", reasons=reasons)
    w.store.hooks.clear()
    lane = w.lane_doc("T1")
    result = lane.publish.last_result
    assert lane.publish.state == "failed"
    assert (result.stage, result.reason, result.writes_made, result.retryable) == ("dispatch", "board_driver_unavailable", True, True)
    plan_id = pub.board_ids(lane.loads[0].load_id, 1)[0]
    assert {w.order(o)["status"] for o in ("o1", "o2")} == {"scheduled"}
    assert {w.links(o)[0] for o in ("o1", "o2")} == {plan_id}
    w.h.drivers.drivers["d1"]["status"] = "active"
    await w.run("T1", reasons=reasons)
    lane = w.lane_doc("T1")
    assert lane.publish.state == "published"
    assert {w.order(o)["status"] for o in ("o1", "o2")} == {"dispatched"}
    assert len(w.store.docs[PLANS]) == 1 and len(w.store.docs[ROUTES]) == 1 and len(w.executions_of(plan_id)) == 1
    assert len(w.driver_ws.of("assignment")) == 1


async def test_lease_taken_over_stops_the_worker_silently(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    await w.start("T1")
    doc = w.store.docs[DRAFTS][f"{T}:{TODAY.isoformat()}"]
    doc["lanes"]["T1"]["publish"]["attempt_id"] = "another-publish"
    await w.publish.wait_idle()
    lane = w.lane_doc("T1")
    assert lane.publish.attempt_id == "another-publish" and lane.publish.state == "publishing"
    assert w.store.docs[PLANS] == {} and w.order("o1")["status"] == "confirmed"


async def test_crashed_publish_reads_as_interrupted_and_can_be_reclaimed(w, monkeypatch):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    reasons = await w.reasons_for("T1")
    monkeypatch.setattr(w.publish, "_spawn", lambda coro: coro.close())  # the pod dies before the worker runs
    await w.start("T1", reasons=reasons)
    exc = await expect(ErrorCode.BOARD_PUBLISH_IN_PROGRESS, w.start("T1", reasons=reasons))
    assert exc.details["reason"] == "publishing"
    w.h.now = w.h.now + timedelta(seconds=200)
    snap = await w.h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    view = next(l for l in snap["lanes"] if l["truck_id"] == "T1")
    assert view["state"] == "failed"
    assert view["publish"]["last_result"]["reason"] == "interrupted"
    assert view["publish"]["last_result"]["writes_made"] == "unknown" and view["publish"]["last_result"]["retryable"] is True
    monkeypatch.undo()
    await w.run("T1", reasons=reasons)
    assert w.lane_doc("T1").publish.state == "published"


async def test_same_client_request_id_returns_the_same_publish(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    cid = str(uuid.uuid4())
    first = await w.run("T1", cid=cid)
    second = await w.start("T1", cid=cid)
    assert second["publish_id"] == first["publish_id"] and second["replayed"] is True
    assert len(w.driver_ws.of("assignment")) == 1
    status = await w.publish.status(tenant_id=T, service_date=TODAY, publish_id=first["publish_id"])
    assert status["done"] is True and status["lanes"][0]["state"] == "published"
    await expect(ErrorCode.RESOURCE_NOT_FOUND, w.publish.status(tenant_id=T, service_date=TODAY, publish_id="nope"))


async def test_version_mismatch_and_missing_lane_and_past_day(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    from fuel.services.dispatch_board_models import PublishBody

    body = PublishBody.model_validate({"client_request_id": str(uuid.uuid4()), "lanes": [{"truck_id": "T1", "expected_version": 0}]})
    exc = await expect(ErrorCode.BOARD_LANE_CONFLICT, w.publish.handle(tenant_id=T, user_id="u", service_date=TODAY, body=body, tz=TZ))
    assert exc.details["reason"] == "version_changed"
    body = PublishBody.model_validate({"client_request_id": str(uuid.uuid4()), "lanes": [{"truck_id": "T9", "expected_version": 0}]})
    exc = await expect(ErrorCode.BOARD_PUBLISH_NOT_READY, w.publish.handle(tenant_id=T, user_id="u", service_date=TODAY, body=body, tz=TZ))
    assert exc.details["lanes"] == [{"truck_id": "T9", "reasons": ["lane_not_found"]}]
    exc = await expect(
        ErrorCode.DISPATCH_BOARD_READ_ONLY,
        w.publish.handle(tenant_id=T, user_id="u", service_date=TODAY - timedelta(days=1), body=body, tz=TZ),
    )


async def test_already_published_lane_does_no_work(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    await w.run("T1")
    mark = len(w.store.ops)
    response = await w.start("T1")
    assert response["publish_id"] is None and response["already_published"] == ["T1"]
    assert [op for op in w.store.ops[mark:] if op[3] and op[1] in OPERATIONAL] == []


async def test_lane_without_driver_or_loads_or_with_shelf_is_not_ready(w):
    w.seed("o1")
    await w.lane("T1", "o1")
    await w.lane("T2", driver_id="d2")
    preview = await w.dry("T1", "T2")
    reasons = {e["truck_id"]: e["reasons"] for e in preview["not_ready"]}
    assert "no_driver" in reasons["T1"] and "no_loads" in reasons["T2"]


async def test_failure_in_one_group_leaves_other_groups_publishing(w):
    w.seed("o1", "o2")
    await w.lane("T1", "o1", driver_id="d1")
    await w.lane("T2", "o2", driver_id="d2")
    reasons = await w.reasons_for("T1", "T2")

    def deactivate(op, index, doc_id):
        if (op, index, doc_id) == ("atomic_update", PLANS, pub.board_ids(w.lane_doc("T1").loads[0].load_id, 1)[0]):
            w.h.drivers.drivers["d1"]["status"] = "off_duty"

    w.store.hooks.append(deactivate)
    response = await w.run("T1", "T2", reasons=reasons)
    assert [g["truck_ids"] for g in response["groups"]] == [["T1"], ["T2"]]
    assert w.lane_doc("T1").publish.state == "failed" and w.lane_doc("T2").publish.state == "published"
    assert w.order("o2")["status"] == "dispatched"


async def test_first_publish_group_never_calls_redispatch(w, monkeypatch):
    calls = []

    async def spy(**kw):
        calls.append(kw)
        return "published"

    monkeypatch.setattr(w.redispatch, "run_group", spy)
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    await w.run("T1")
    assert calls == [] and w.lane_doc("T1").publish.state == "published"


async def test_freeze_rule_7_lane_holding_dispatched_order_never_runs_first_publish(w, monkeypatch):
    w.seed("o1", "o2")
    await w.lane("T1", "o1", "o2", driver_id="d1")
    await w.run("T1")
    await w.lane("T2", driver_id="d2")
    await w.move(["o1"], "T2", lanes=("T1", "T2"), target={"load_id": "new"})
    first = []
    original = w.publish._first_publish

    async def spy(*args, **kw):
        first.append(args)
        return await original(*args, **kw)

    monkeypatch.setattr(w.publish, "_first_publish", spy)
    response = await w.run("T2")
    assert response["groups"] == [{"truck_ids": ["T2", "T1"], "kind": "redispatch", "added_lanes": ["T1"]}]
    assert first == []
    assert w.lane_doc("T2").publish.state == "published" and w.lane_doc("T1").publish.state == "published"


async def test_publish_audit_and_metrics_carry_ids_only(w):
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    await w.run("T1")
    audits = w.h.telemetry.audits
    assert audits and audits[0][0] == "dispatch_board" and audits[0][4] == "publish"
    text = repr(audits) + repr(w.h.telemetry.metrics)
    assert "Acme" not in text and "Depot" not in text
    names = {m[0] for m in w.h.telemetry.metrics}
    assert {"board.publish.lane.count", "board.publish.lane.ms"} <= names


async def test_publish_broadcasts_progress_and_lane_updates(w):
    events = []

    class WS:
        async def broadcast_board_event(self, tenant_id, day, event_type, data):
            events.append(event_type)

    w.h.service._ws = WS()
    w.seed("o1")
    await w.lane("T1", "o1", driver_id="d1")
    events.clear()
    await w.run("T1")
    assert "board_publish_progress" in events and "board_lanes_updated" in events


# ---- publish groups (K8.4, P11) --------------------------------------------


def _lane(truck, loads, *, plans=None, published=None, driver=None):
    lane = Lane(truck_id=truck, version=1, driver_id=driver, loads=loads)
    if plans:
        lane.publish = LanePublish(
            state="published",
            plans=plans,
            published_content=published,
            published_hash=engine.content_hash(published) if published else None,
            published_version=1,
        )
    return lane


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    n_lanes=st.integers(2, 5),
    homes=st.lists(st.integers(0, 4), min_size=1, max_size=8),
    places=st.lists(st.integers(0, 4), min_size=1, max_size=8),
    dispatched=st.lists(st.booleans(), min_size=1, max_size=8),
    requested=st.sets(st.integers(0, 4), min_size=1),
)
def test_p11_publish_groups_partition_and_keep_moves_together(n_lanes, homes, places, dispatched, requested):
    trucks = [f"T{i}" for i in range(n_lanes)]
    n = min(len(homes), len(places), len(dispatched))
    orders = {}
    published_loads = {t: [] for t in trucks}
    current_loads = {t: [] for t in trucks}
    for i in range(n):
        oid = f"o{i}"
        home, place = trucks[homes[i] % n_lanes], trucks[places[i] % n_lanes]
        is_dispatched = dispatched[i]
        if is_dispatched:
            published_loads[home].append(oid)
            orders[oid] = {"order_id": oid, "status": "dispatched", "assigned_run_id": f"bp-P{home}-r1"}
        else:
            orders[oid] = {"order_id": oid, "status": "confirmed", "assigned_run_id": None}
        current_loads[place].append(oid)
    lanes = {}
    for t in trucks:
        stops = [Stop(order_id=o, snapshot=OrderSnapshot()) for o in current_loads[t]]
        loads = [Load(load_id=f"C{t}", stops=stops)] if stops else []
        if published_loads[t]:
            pload = Load(load_id=f"P{t}", stops=[Stop(order_id=o, snapshot=OrderSnapshot()) for o in published_loads[t]])
            plans = {f"P{t}": PublishedPlan(plan_id=f"bp-P{t}-r1", route_id=f"br-P{t}-r1", run_id=f"bp-P{t}-r1", revision=1)}
            loads.append(Load(load_id=f"P{t}"))
            lanes[t] = _lane(t, loads, plans=plans, published=LaneContent(loads=[pload]))
        else:
            lanes[t] = _lane(t, loads)
    draft = BoardDraft(tenant_id=T, service_date=TODAY, timezone=TZ, lanes=lanes)
    engine.rebuild_index(draft)
    req = [trucks[i % n_lanes] for i in sorted(requested)]
    req = list(dict.fromkeys(req))
    groups = pub.publish_groups(draft, req, orders)
    claimed = [t for g in groups for t in g.truck_ids]
    # Every requested lane exactly once; the partition never repeats a lane.
    assert len(claimed) == len(set(claimed)) and set(req) <= set(claimed)
    group_of = {t: i for i, g in enumerate(groups) for t in g.truck_ids}
    for t in claimed:
        if any((orders.get(o) or {}).get("status") == "dispatched" for o in engine.lane_order_ids(draft.lanes[t])):
            assert groups[group_of[t]].kind == "redispatch"
    for oid, order in orders.items():
        if order["status"] != "dispatched":
            continue
        dest = draft.order_index.get(oid)
        source = order["assigned_run_id"][3:-3].lstrip("P")
        if dest in group_of or source in group_of:
            assert dest in group_of and source in group_of and group_of[dest] == group_of[source]
    # The candidate superset never misses a claimed lane.
    assert set(claimed) <= set(pub.candidate_lanes(draft, req))
