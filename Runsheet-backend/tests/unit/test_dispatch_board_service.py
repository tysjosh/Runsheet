"""DispatchBoardService (plan task 11; design K4.2, K4.4, K4.5, K5, K6, K17.1; P6).

Runs over ``BoardStore`` (real ``atomic_update`` and insert-if-absent
semantics) with fakes for every validator.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services import dispatch_board_engine as engine
from fuel.services.dispatch_board_models import (
    BoardDraft,
    HistoryQuery,
    LaneContent,
    LanePublish,
    PublishedPlan,
    RedispatchAttempt,
    SnapshotQuery,
    ValidateBody,
    draft_doc_id,
)
from fuel.services.dispatch_board_service import DispatchBoardService
from tests.unit._dispatch_board_fakes import (
    COMMANDS,
    COMPARTMENTS,
    DRAFTS,
    NOW,
    ORDERS,
    PRIORITIES,
    T,
    TODAY,
    TOMORROW,
    TZ,
    Harness,
    driver,
)


@pytest.fixture
def h() -> Harness:
    return Harness()


def committed_logs(h: Harness):
    return [d for d in h.store.command_docs() if d["result"] == "committed"]


def refused_ids(h: Harness):
    return [k for k in h.store.docs[COMMANDS] if ":refused:" in k]


async def expect(code, coro):
    with pytest.raises(AppException) as info:
        await coro
    assert info.value.error_code == code, info.value.details
    return info.value


# ---- commands: the K4.2 sequence -----------------------------------------


async def test_command_commits_and_logs(h):
    h.seed_order("o1")
    await h.lane_with("T1", "o1", driver_id="d1")
    draft = h.draft()
    lane = draft.lanes["T1"]
    assert draft.draft_version == 3 and lane.version == 3
    assert lane.driver_id == "d1" and [s.order_id for s in lane.loads[0].stops] == ["o1"]
    assert draft.order_ids == ["o1"] and len(draft.applied_commands) == 3
    logs = committed_logs(h)
    assert len(logs) == 3 and {l["type"] for l in logs} == {"add_lane", "pair_driver", "assign_orders"}
    log = next(l for l in logs if l["type"] == "assign_orders")
    assert log["actor_user_id"] == "user-1" and log["actor_name"] == "ana"
    assert log["lanes"][0]["version_before"] == 2 and log["lanes"][0]["version_after"] == 3
    assert log["content_before"]["T1"]["loads"] == [] and log["content_after"]["T1"]["loads"][0]["stops"][0]["order_id"] == "o1"
    assert log["response"]["lanes"][0]["truck_id"] == "T1"
    # Nothing outside the draft and log changed (I4).
    assert h.store.doc(ORDERS, "o1")["status"] == "confirmed"
    assert {idx for _op, idx, _id in h.store.writes()} <= {DRAFTS, COMMANDS}


async def test_same_id_same_payload_replays_the_logged_response(h):
    cmd = h.command("add_lane", truck_id="T1", lanes=("T1",))
    first = await h.send(cmd)
    second = await h.send(cmd)
    assert second == first and h.draft().draft_version == 1


async def test_same_id_different_payload_is_idempotency_conflict(h):
    cid = str(uuid.uuid4())
    await h.send(h.command("add_lane", truck_id="T1", lanes=("T1",), cid=cid))
    other = h.command("add_lane", truck_id="T2", lanes=("T2",), cid=cid)
    await expect(ErrorCode.IDEMPOTENCY_CONFLICT, h.send(other))


async def test_crash_between_commit_and_log_then_retry_is_already_applied(h):
    cmd = h.command("add_lane", truck_id="T1", lanes=("T1",))
    h.store.fail_on("create_document", COMMANDS, f"{T}:{cmd.client_command_id}")
    degraded = await h.send(cmd)
    assert degraded["audit_degraded"] is True
    retry = await h.send(cmd)
    assert retry["already_applied"] is True
    assert retry["lanes"][0]["version"] == 1 and h.draft().draft_version == 1
    # Same id, different payload, still in applied_commands: conflict.
    other = h.command("add_lane", truck_id="T2", lanes=("T2",), cid=cmd.client_command_id)
    await expect(ErrorCode.IDEMPOTENCY_CONFLICT, h.send(other))


async def test_concurrent_duplicate_ids_commit_once(h):
    h.store.set_yield_schedule([0, 1, 2, 3, 1, 0, 2, 1], repeat=True)
    cmd = h.command("add_lane", truck_id="T1", lanes=("T1",))
    a, b = await asyncio.gather(h.send(cmd), h.send(cmd))
    assert len(committed_logs(h)) == 1 and h.draft().draft_version == 1
    logged = committed_logs(h)[0]["response"]
    for response in (a, b):
        if response.get("already_applied"):
            assert [l["version"] for l in response["lanes"]] == [l["version"] for l in logged["lanes"]]
        else:
            assert response == logged


async def test_version_precheck_conflict_returns_current_lanes(h):
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    exc = await expect(ErrorCode.BOARD_LANE_CONFLICT, h.run("pair_driver", truck_id="T1", driver_id="d1", versions={"T1": 0}))
    assert exc.status_code == 409 and exc.details["reason"] == "version_changed"
    assert exc.details["lanes"][0]["version"] == 1


async def test_transform_refuses_a_change_that_lands_after_validation(h):
    h.seed_order("o1")
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    doc_id = draft_doc_id(T, TODAY)

    async def bump(op, index, doc_id_):
        if op == "atomic_update" and index == DRAFTS:
            h.store.hooks.clear()
            stored = h.store.docs[DRAFTS][doc_id]
            stored["lanes"]["T1"]["version"] += 1

    h.store.hooks.append(bump)
    exc = await expect(ErrorCode.BOARD_LANE_CONFLICT, h.run("assign_orders", order_ids=["o1"], truck_id="T1", lanes=("T1",)))
    assert exc.details["reason"] == "version_changed"
    assert h.draft().order_ids == []
    assert len(refused_ids(h)) == 1


async def test_cross_lane_rule_rechecked_in_the_transform(h):
    h.seed_order("o1")
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    await h.run("add_lane", truck_id="T2", lanes=("T2",))
    doc_id = draft_doc_id(T, TODAY)

    async def sneak(op, index, doc_id_):
        if op == "atomic_update" and index == DRAFTS:
            h.store.hooks.clear()
            stored = h.store.docs[DRAFTS][doc_id]
            stored["lanes"]["T2"]["shelf"] = ["o1"]  # another writer put o1 on T2

    h.store.hooks.append(sneak)
    exc = await expect(ErrorCode.BOARD_LANE_CONFLICT, h.run("assign_orders", order_ids=["o1"], truck_id="T1", lanes=("T1",)))
    assert exc.details["reason"] == "cross_lane_rule"


async def test_blocked_command_is_refused_and_logged_under_the_refused_id(h):
    h.seed_order("o1", status="on_hold")
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    cmd = h.command("assign_orders", order_ids=["o1"], truck_id="T1", lanes=("T1",))
    exc = await expect(ErrorCode.BOARD_COMMAND_BLOCKED, h.send(cmd))
    assert exc.status_code == 422 and exc.details["reason"] == "on_hold"
    blocked = [c for c in exc.details["checks"]["T1"] if c["outcome"] == "block"]
    assert [(c["check"], c["reason_code"]) for c in blocked] == [("order_state", "on_hold")]
    assert h.draft().order_ids == []
    ids = refused_ids(h)
    assert len(ids) == 1 and ids[0].startswith(f"{T}:{cmd.client_command_id}:refused:")
    assert h.store.doc(COMMANDS, f"{T}:{cmd.client_command_id}") is None
    assert h.store.doc(COMMANDS, ids[0])["result"] == "blocked"


async def test_engine_error_is_422_with_reason(h):
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    exc = await expect(ErrorCode.VALIDATION_ERROR, h.run("assign_orders", order_ids=["nope"], truck_id="T1", lanes=("T1",)))
    assert exc.status_code == 422 and exc.details["reason"] == "unknown_order"
    exc = await expect(ErrorCode.VALIDATION_ERROR, h.run("remove_lane", truck_id="T1", versions={}))
    assert exc.details["reason"] == "missing_expected_version"


async def test_past_service_day_is_read_only_and_range_is_enforced(h):
    exc = await expect(ErrorCode.DISPATCH_BOARD_READ_ONLY, h.run("add_lane", truck_id="T1", lanes=("T1",), day=TODAY - timedelta(days=1)))
    assert exc.details == {"reason": "past_service_day"}
    exc = await expect(ErrorCode.VALIDATION_ERROR, h.run("add_lane", truck_id="T1", lanes=("T1",), day=TODAY + timedelta(days=15)))
    assert exc.details["reason"] == "date_out_of_range"


async def test_publishing_and_recovery_lanes_refuse_commands(h):
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    doc = h.store.docs[DRAFTS][draft_doc_id(T, TODAY)]
    doc["lanes"]["T1"]["publish"] = LanePublish(state="publishing", lease_until=NOW + timedelta(minutes=2)).model_dump(mode="json")
    exc = await expect(ErrorCode.BOARD_PUBLISH_IN_PROGRESS, h.run("pair_driver", truck_id="T1", driver_id="d1", lanes=("T1",)))
    assert exc.details["reason"] == "publishing"
    attempt = RedispatchAttempt(attempt_id="a1", group_truck_ids=["T1"])
    doc["lanes"]["T1"]["publish"] = LanePublish(state="failed", attempt=attempt).model_dump(mode="json")
    exc = await expect(ErrorCode.BOARD_PUBLISH_IN_PROGRESS, h.run("pair_driver", truck_id="T1", driver_id="d1", lanes=("T1",)))
    assert exc.status_code == 409 and exc.details["reason"] == "recovery_pending"


async def test_context_is_built_before_engine_apply(h, monkeypatch):
    h.seed_order("o1")
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    calls = []
    real_build, real_apply = h.validation.build_context, engine.apply

    async def build(*a, **k):
        calls.append("build_context")
        return await real_build(*a, **k)

    def apply_spy(*a, **k):
        calls.append("apply")
        return real_apply(*a, **k)

    monkeypatch.setattr(h.validation, "build_context", build)
    monkeypatch.setattr(engine, "apply", apply_spy)
    await h.run("assign_orders", order_ids=["o1"], truck_id="T1", lanes=("T1",))
    assert calls == ["build_context", "apply"]


async def test_store_down_for_orders_is_503(h):
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    h.store.seed(ORDERS, "o1", {"order_id": "o1", "tenant_id": T})
    h.orders.store = None  # get_current raises
    exc = await expect(ErrorCode.ELASTICSEARCH_UNAVAILABLE, h.run("assign_orders", order_ids=["o1"], truck_id="T1", lanes=("T1",)))
    assert exc.status_code == 503


async def test_order_on_tomorrows_draft_is_blocked_today(h):
    h.seed_order("o1")
    await h.lane_with("T1", "o1", day=TOMORROW)
    await h.run("add_lane", truck_id="T2", lanes=("T2",))
    exc = await expect(ErrorCode.BOARD_COMMAND_BLOCKED, h.run("assign_orders", order_ids=["o1"], truck_id="T2", lanes=("T2",)))
    assert exc.details["reason"] == "order_on_other_day"
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert "o1" not in [o["order_id"] for o in snap["trays"]["orders"]]


async def test_future_day_lane_with_blocked_driver_commits(h):
    h.hos.blocked = {"d1"}
    h.seed_order("o1", day=TOMORROW)
    await h.lane_with("T1", "o1", driver_id="d1", day=TOMORROW)
    lane = h.draft(TOMORROW).lanes["T1"]
    assert ("info", "hos_not_projected") in [(c.outcome, c.reason_code) for c in lane.checks]
    assert h.hos.calls == []
    h.seed_order("o2")
    await h.run("add_lane", truck_id="T2", lanes=("T2",))
    exc = await expect(ErrorCode.BOARD_COMMAND_BLOCKED, h.run("pair_driver", truck_id="T2", driver_id="d1", lanes=("T2",)))
    assert exc.details["reason"] == "hos_gate_blocked"


async def _pinned_away_from_home(h):
    """T1 published with o1+o2; after a rollback o1 sits on T2 and has started."""
    for o in ("o1", "o2"):
        h.seed_order(o)
    await h.lane_with("T1", "o1", "o2", driver_id="d1")
    await h.run("add_lane", truck_id="T2", lanes=("T2",))
    doc = h.store.docs[DRAFTS][draft_doc_id(T, TODAY)]
    lane = h.draft().lanes["T1"]
    load_id = lane.loads[0].load_id
    plan = PublishedPlan(plan_id=f"bp-{load_id}-r1", route_id=f"br-{load_id}-r1", run_id=f"bp-{load_id}-r1", revision=1)
    publish = LanePublish(state="published", published_version=lane.version, published_content=lane.content(), published_hash=engine.content_hash(lane), plans={load_id: plan})
    doc["lanes"]["T1"]["publish"] = publish.model_dump(mode="json")
    doc["lanes"]["T1"]["loads"][0]["stops"] = [s for s in doc["lanes"]["T1"]["loads"][0]["stops"] if s["order_id"] != "o1"]
    doc["lanes"]["T2"]["loads"] = [dict(doc["lanes"]["T1"]["loads"][0], load_id="LB", stops=[lane.loads[0].stops[0].model_dump(mode="json")])]
    doc["order_index"]["o1"] = "T2"
    h.store.poke(ORDERS, "o1", status="in_transit", assigned_run_id=plan.run_id)
    h.store.poke(ORDERS, "o2", status="dispatched", assigned_run_id=plan.run_id)
    return load_id


async def test_move_back_commits_despite_a_persistent_block_on_the_home_lane(h):
    """Review P1-2 freeze narrowing: a pure move-back that adds no new block commits."""
    load_id = await _pinned_away_from_home(h)
    # The home lane's driver lapsed mid-day; the driver can't change on a started lane.
    h.qualification.ineligible = {"d1": ["cdl_expired"]}
    h.validation.cache.clear()
    exc = await expect(ErrorCode.BOARD_COMMAND_BLOCKED, h.run("pair_driver", truck_id="T1", driver_id="d2", lanes=("T1",)))
    assert "load_started" in [c["reason_code"] for c in exc.details["checks"]["T1"] if c["outcome"] == "block"]
    # Any other change to T1 is still refused by the persistent block.
    h.seed_order("o3")
    exc = await expect(ErrorCode.BOARD_COMMAND_BLOCKED, h.run("assign_orders", order_ids=["o3"], truck_id="T1", target={"load_id": "new"}, lanes=("T1",)))
    assert "cdl_expired" in [c["reason_code"] for c in exc.details["checks"]["T1"] if c["outcome"] == "block"]
    # The move back commits; the old block stays on T1, B's stop_pinned is gone.
    result = await h.run("move_stops", order_ids=["o1"], truck_id="T1", target={"load_id": load_id, "index": 0}, lanes=("T1", "T2"))
    assert {l["truck_id"] for l in result["lanes"]} == {"T1", "T2"}
    draft = h.draft()
    assert draft.order_index["o1"] == "T1"
    assert ("block", "cdl_expired") in [(c.outcome, c.reason_code) for c in draft.lanes["T1"].checks]
    assert "stop_pinned" not in [c.reason_code for c in draft.lanes["T2"].checks]


async def test_move_back_that_adds_a_new_block_is_refused(h):
    load_id = await _pinned_away_from_home(h)
    # o1 has grown past T1's capacity: moving it home adds a compartment_fit
    # block to T1 that T1 didn't have before, so the move back is refused.
    h.store.poke(ORDERS, "o1", gallons_requested=50000.0)
    h.validation.cache.clear()
    exc = await expect(
        ErrorCode.BOARD_COMMAND_BLOCKED,
        h.run("move_stops", order_ids=["o1"], truck_id="T1", target={"load_id": load_id, "index": 0}, lanes=("T1", "T2")),
    )
    t1_blocks = [c["reason_code"] for c in exc.details["checks"]["T1"] if c["outcome"] == "block"]
    assert "total_overage" in t1_blocks
    assert h.draft().order_index["o1"] == "T2"


async def test_pair_driver_with_driver_repository_down_is_blocked(h):
    """Review P1-1: qualification can't run, so it blocks (K3.4)."""
    h.seed_order("o1")
    await h.lane_with("T1", "o1")

    async def down(tenant_id, driver_id):
        raise TimeoutError("driver store timed out")

    h.drivers.get = down
    h.validation.cache.clear()
    exc = await expect(ErrorCode.BOARD_COMMAND_BLOCKED, h.run("pair_driver", truck_id="T1", driver_id="d1", lanes=("T1",)))
    assert exc.status_code == 422
    blocked = [(c["check"], c["reason_code"]) for c in exc.details["checks"]["T1"] if c["outcome"] == "block"]
    assert ("driver_qualification", "check_unavailable") in blocked
    assert h.draft().lanes["T1"].driver_id is None


async def test_dyed_order_with_compartments_down_is_blocked(h):
    """Review P1-1: dyed_diesel blocks when compartment data is unavailable (K3.4)."""
    h.seed_order("o1", product_code="OFF_ROAD_DIESEL")
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    h.validation.cache.clear()
    h.store.fail_on("search_documents", COMPARTMENTS, times=100)
    exc = await expect(ErrorCode.BOARD_COMMAND_BLOCKED, h.run("assign_orders", order_ids=["o1"], truck_id="T1", target={"load_id": "new"}, lanes=("T1",)))
    assert exc.status_code == 422
    blocked = [(c["check"], c["reason_code"]) for c in exc.details["checks"]["T1"] if c["outcome"] == "block"]
    assert ("dyed_diesel", "check_unavailable") in blocked
    assert h.draft().order_ids == []


async def test_shadow_snapshot_writes_nothing(h):
    """Review P1-3: shadow recomputes stale checks for the response but saves nothing."""
    h.seed_order("o1")
    await h.lane_with("T1", "o1", driver_id="d1")
    doc_id = draft_doc_id(T, TODAY)
    h.store.docs[DRAFTS][doc_id]["lanes"]["T1"]["checks_stale"] = True
    before = len(h.store.writes(DRAFTS))
    snap = await h.service.snapshot(T, TODAY, mode="shadow", tz=TZ)
    assert snap["read_only"] is True and snap["lanes"][0]["truck_id"] == "T1"
    assert len(h.store.writes(DRAFTS)) == before
    assert h.store.docs[DRAFTS][doc_id]["lanes"]["T1"]["checks_stale"] is True
    await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert len(h.store.writes(DRAFTS)) == before + 1
    assert h.store.docs[DRAFTS][doc_id]["lanes"]["T1"]["checks_stale"] is False


async def test_freeze_rule_11e_move_back_commits(h):
    for o in ("o1", "o2"):
        h.seed_order(o)
    await h.lane_with("T1", "o1", "o2", driver_id="d1")
    await h.run("add_lane", truck_id="T2", lanes=("T2",))
    doc = h.store.docs[DRAFTS][draft_doc_id(T, TODAY)]
    lane = h.draft().lanes["T1"]
    load_id = lane.loads[0].load_id
    plan = PublishedPlan(plan_id=f"bp-{load_id}-r1", route_id=f"br-{load_id}-r1", run_id=f"bp-{load_id}-r1", revision=1)
    publish = LanePublish(state="published", published_version=lane.version, published_content=lane.content(), published_hash=engine.content_hash(lane), plans={load_id: plan})
    doc["lanes"]["T1"]["publish"] = publish.model_dump(mode="json")
    # As after a rollback: o1 sits on T2 and the driver started it.
    doc["lanes"]["T1"]["loads"][0]["stops"] = [s for s in doc["lanes"]["T1"]["loads"][0]["stops"] if s["order_id"] != "o1"]
    doc["lanes"]["T2"]["loads"] = [dict(doc["lanes"]["T1"]["loads"][0], load_id="LB", stops=[lane.loads[0].stops[0].model_dump(mode="json")])]
    doc["order_index"]["o1"] = "T2"
    h.store.poke(ORDERS, "o1", status="in_transit", assigned_run_id=plan.run_id)
    h.store.poke(ORDERS, "o2", status="dispatched", assigned_run_id=plan.run_id)
    await h.run("add_lane", truck_id="T3", lanes=("T3",))
    exc = await expect(ErrorCode.BOARD_COMMAND_BLOCKED, h.run("move_stops", order_ids=["o1"], truck_id="T3", target={"load_id": "new"}, lanes=("T2", "T3")))
    assert exc.details["reason"] == "stop_pinned"
    checks = exc.details["checks"]["T3"]
    pinned = next(c for c in checks if c["reason_code"] == "stop_pinned")
    assert pinned["message"] == "Started on T1. Move it back."
    assert pinned["fix_link"] == {"kind": "move_back", "id": "o1", "truck_id": "T1", "load_id": load_id}
    result = await h.run("move_stops", order_ids=["o1"], truck_id="T1", target={"load_id": load_id, "index": 0}, lanes=("T1", "T2"))
    assert {l["truck_id"] for l in result["lanes"]} == {"T1", "T2"}
    assert h.draft().order_index["o1"] == "T1"


# ---- acknowledgements (K4.4) ---------------------------------------------


async def test_acknowledge_open_warning_without_version_check(h):
    h.seed_order("o1")
    await h.lane_with("T1", "o1")  # no driver: warn no_driver
    lane = h.draft().lanes["T1"]
    warning = next(c for c in lane.checks if c.reason_code == "no_driver")
    result = await h.run("acknowledge_warning", truck_id="T1", warning_id=warning.warning_id, reason="Driver confirmed by phone", versions={})
    draft = h.draft()
    assert draft.acknowledged[warning.warning_id].reason == "Driver confirmed by phone"
    assert draft.acknowledged[warning.warning_id].actor_user_id == "user-1"
    assert draft.lanes["T1"].version == lane.version  # no lane bump
    assert result["lanes"][0]["version"] == lane.version
    log = next(l for l in committed_logs(h) if l["type"] == "acknowledge_warning")
    assert log["overrides"] == [{"warning_id": warning.warning_id, "reason_code": "no_driver", "reason": "Driver confirmed by phone"}]
    assert ("board.override.count", 1.0, {"tenant_id": T, "reason_code": "no_driver"}) in h.telemetry.metrics
    exc = await expect(ErrorCode.VALIDATION_ERROR, h.run("acknowledge_warning", truck_id="T1", warning_id="0" * 16, reason="whatever reason", versions={}))
    assert exc.details["reason"] == "warning_not_open"


# ---- undo / redo (K6) ----------------------------------------------------


async def test_revert_and_reapply_round_trip(h):
    h.seed_order("o1")
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    assign = h.command("assign_orders", order_ids=["o1"], truck_id="T1", lanes=("T1",))
    await h.send(assign)
    before_hash = engine.content_hash(h.draft().lanes["T1"])
    await h.run("revert", target_command_id=assign.client_command_id, lanes=("T1",))
    assert h.draft().order_ids == [] and h.draft().lanes["T1"].loads == []
    revert_log = next(l for l in committed_logs(h) if l["type"] == "revert")
    assert revert_log["target_command_id"] == assign.client_command_id
    await h.run("reapply", target_command_id=assign.client_command_id, lanes=("T1",))
    assert engine.content_hash(h.draft().lanes["T1"]) == before_hash
    undo_metrics = [m for m in h.telemetry.metrics if m[0] == "board.undo.count"]
    assert [m[2]["type"] for m in undo_metrics] == ["revert", "reapply"]


async def test_revert_of_add_lane_removes_the_lane(h):
    add = h.command("add_lane", truck_id="T1", lanes=("T1",))
    await h.send(add)
    await h.run("revert", target_command_id=add.client_command_id, lanes=("T1",))
    assert "T1" not in h.draft().lanes


async def test_revert_refusals(h):
    h.seed_order("o1")
    h.seed_order("o2")
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    assign = h.command("assign_orders", order_ids=["o1"], truck_id="T1", lanes=("T1",))
    await h.send(assign)
    exc = await expect(ErrorCode.BOARD_UNDO_STALE, h.run("revert", target_command_id=assign.client_command_id, lanes=("T1",), user="user-2"))
    assert exc.details["reason"] == "not_owner"
    await h.run("assign_orders", order_ids=["o2"], truck_id="T1", lanes=("T1",), user="user-2")
    exc = await expect(ErrorCode.BOARD_UNDO_STALE, h.run("revert", target_command_id=assign.client_command_id, lanes=("T1",)))
    assert exc.details["reason"] == "changed_by_other"
    exc = await expect(ErrorCode.BOARD_UNDO_STALE, h.run("revert", target_command_id=str(uuid.uuid4()), lanes=("T1",)))
    assert exc.details["reason"] == "not_found"


async def test_revert_refused_after_publish(h):
    h.seed_order("o1")
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    assign = h.command("assign_orders", order_ids=["o1"], truck_id="T1", lanes=("T1",))
    await h.send(assign)
    doc = h.store.docs[DRAFTS][draft_doc_id(T, TODAY)]
    doc["lanes"]["T1"]["publish"]["published_version"] = doc["lanes"]["T1"]["version"]
    doc["lanes"]["T1"]["publish"]["state"] = "published"
    exc = await expect(ErrorCode.BOARD_UNDO_STALE, h.run("revert", target_command_id=assign.client_command_id, lanes=("T1",)))
    assert exc.details["reason"] == "published_since"


# ---- snapshot (K5) -------------------------------------------------------


async def test_snapshot_shape_and_on_hold_tray_orders(h):
    h.seed_order("o1")
    h.seed_order("o2", status="on_hold")
    h.seed_order("o3", status="scheduled", assigned_run_id="agent-run")  # linked elsewhere: excluded
    h.seed_order("o4", status="dispatched")  # not a tray status
    h.seed_order("o5", day=TODAY + timedelta(days=3))  # another day's window
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert snap["service_date"] == TODAY.isoformat() and snap["timezone"] == TZ and snap["mode"] == "active_gated"
    assert snap["read_only"] is False and snap["draft_version"] == 0
    tray = {o["order_id"]: o for o in snap["trays"]["orders"]}
    assert set(tray) == {"o1", "o2"}
    assert tray["o2"]["draggable"] is False and tray["o2"]["block_reason"] == "on_hold"
    assert tray["o1"]["draggable"] is True and tray["o1"]["block_reason"] is None
    assert {t["truck_id"] for t in snap["trays"]["trucks"]} == {"T1", "T2", "T3"}
    assert {d["driver_id"] for d in snap["trays"]["drivers"]} == {"d1", "d2", "d3"}
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    exc = await expect(ErrorCode.BOARD_COMMAND_BLOCKED, h.run("assign_orders", order_ids=["o2"], truck_id="T1", lanes=("T1",)))
    assert exc.details["reason"] == "on_hold"


async def test_snapshot_read_only_states(h):
    snap = await h.service.snapshot(T, TODAY, mode="shadow", tz=TZ)
    assert snap["read_only"] is True and snap["read_only_reason"] == "shadow"
    past = await h.service.snapshot(T, TODAY - timedelta(days=2), mode="active_gated", tz=TZ)
    assert past["read_only"] is True and past["read_only_reason"] == "past_service_day"


async def test_tray_priority_join_and_order(h):
    h.seed_order("a", delivery_window_start=f"{TODAY}T14:00:00+00:00")
    h.seed_order("b", delivery_window_start=f"{TODAY}T13:00:00+00:00")
    h.seed_order("c", delivery_window_start=f"{TODAY}T16:00:00+00:00")
    h.seed_order("d", delivery_window_start=None, delivery_window_end=None)
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert [o["order_id"] for o in snap["trays"]["orders"]] == ["b", "a", "c", "d"]
    h.store.seed(PRIORITIES, "old", {"tenant_id": T, "created_at": (NOW - timedelta(hours=30)).isoformat(), "priorities": [{"order_id": "d", "priority_score": 99}]})
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert [o["order_id"] for o in snap["trays"]["orders"]] == ["b", "a", "c", "d"]
    h.store.seed(PRIORITIES, "new", {"tenant_id": T, "created_at": (NOW - timedelta(hours=1)).isoformat(), "priorities": [
        {"order_id": "c", "priority_score": 80, "priority_bucket": "high"},
        {"order_id": "c", "priority_score": 85, "priority_bucket": "critical"},
        {"order_id": "a", "priority_score": 40, "priority_bucket": "medium"},
        {"station_id": "s1", "priority_score": 100},
    ]})
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    tray = snap["trays"]["orders"]
    assert [o["order_id"] for o in tray] == ["c", "a", "b", "d"]
    assert (tray[0]["priority_score"], tray[0]["priority_bucket"]) == (85.0, "critical")
    assert snap["degraded_sources"] == []


async def test_priority_read_failure_falls_back_to_window_order(h):
    h.seed_order("a", delivery_window_start=f"{TODAY}T14:00:00+00:00")
    h.seed_order("b", delivery_window_start=f"{TODAY}T13:00:00+00:00")
    h.store.fail_on("search_documents", PRIORITIES)
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert [o["order_id"] for o in snap["trays"]["orders"]] == ["b", "a"]
    assert "delivery_priorities" in snap["degraded_sources"]
    assert all(o["priority_bucket"] is None for o in snap["trays"]["orders"])


async def test_tray_truncation_keeps_the_earliest_windows(h):
    base = datetime(2026, 10, 8, 13, tzinfo=timezone.utc)
    for i in range(995):
        start = (base + timedelta(seconds=i)).isoformat()
        h.store.seed(ORDERS, f"w{i:04d}", {**h.seed_order(f"w{i:04d}"), "delivery_window_start": start})
    for i in range(6):
        h.seed_order(f"nw{i}", delivery_window_start=None, delivery_window_end=None)
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    ids = [o["order_id"] for o in snap["trays"]["orders"]]
    assert snap["trays"]["orders_truncated"] is True and len(ids) == 1000
    assert sum(1 for i in ids if i.startswith("nw")) == 5  # the sixth no-window order dropped
    assert all(f"w{i:04d}" in ids for i in range(995))


async def test_filters_narrow_only_the_tray(h):
    h.seed_order("a", call_type="will_call", product_code="DIESEL_2")
    h.seed_order("b", call_type="keep_full", product_code="GASOLINE_REG")
    h.seed_order("c", call_type="keep_full", product_code="DIESEL_2")
    await h.lane_with("T1", "c")
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ, filters=SnapshotQuery(call_type="keep_full"))
    assert [o["order_id"] for o in snap["trays"]["orders"]] == ["b"]
    assert [l["truck_id"] for l in snap["lanes"]] == ["T1"]  # lanes stay complete
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ, filters=SnapshotQuery(product="DIESEL_2"))
    assert [o["order_id"] for o in snap["trays"]["orders"]] == ["a"]


async def test_snapshot_lanes_param_returns_only_those_lanes(h):
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    await h.run("add_lane", truck_id="T2", lanes=("T2",))
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ, lanes=["T2"])
    assert [l["truck_id"] for l in snap["lanes"]] == ["T2"] and snap["trays"]["orders"] == []


async def test_suggested_driver_cases():
    h = Harness(drivers=[
        driver("d1", assigned_truck_id="T1"),
        driver("d2", assigned_truck_id="T2"), driver("d3", assigned_truck_id="T2"),
        driver("d4", assigned_truck_id="T3"),
        driver("d5", assigned_truck_id="T4"), driver("d6", assigned_truck_id="T1", status="inactive"),
    ])
    for t in ("T1", "T2", "T3", "T4"):
        h.store.seed("truck_compartments", f"{t}_C1x", {"tenant_id": T, "truck_id": t, "compartment_id": "C1", "capacity_liters": 9000, "allowed_grades": ["AGO"]})
        await h.run("add_lane", truck_id=t, lanes=(t,))
    await h.run("pair_driver", truck_id="T4", driver_id="d4", lanes=("T4",))
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    suggested = {l["truck_id"]: l["suggested_driver"] for l in snap["lanes"]}
    assert suggested["T1"] == {"driver_id": "d1", "name": "Driver d1", "source": "assigned_truck_id"}
    assert suggested["T2"] is None  # two matches
    assert suggested["T3"] is None  # its only match is paired to T4
    assert suggested["T4"] is None  # already paired
    h2 = Harness(drivers=[])
    await h2.run("add_lane", truck_id="T1", lanes=("T1",))
    snap = await h2.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert snap["lanes"][0]["suggested_driver"] is None  # zero matches


async def test_lane_truck_type_present_missing_and_other_tenant(h):
    """R2.8 (plan task 36b): ``truck_type`` is the asset's ``asset_subtype``."""
    for t in ("T1", "T2", "T3"):
        await h.run("add_lane", truck_id=t, lanes=(t,))
    h.store.seed("trucks", "a1", {"tenant_id": T, "asset_id": "T1", "asset_subtype": "tank_wagon"})
    h.store.seed("trucks", "a2", {"tenant_id": T, "truck_id": "T2", "asset_subtype": "transport"})  # older key
    h.store.seed("trucks", "a3", {"tenant_id": "tenant-2", "asset_id": "T3", "asset_subtype": "bobtail"})
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    types = {l["truck_id"]: l["truck_type"] for l in snap["lanes"]}
    assert types == {"T1": "tank_wagon", "T2": "transport", "T3": None}
    h.store.fail_on("search_documents", "trucks")
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert {l["truck_id"]: l["truck_type"] for l in snap["lanes"]} == {"T1": None, "T2": None, "T3": None}
    assert snap["degraded_sources"] == []


async def test_driver_tray_tanker_endorsement_and_nearest_expiry(h):
    """R3.4 (plan task 36b): from the compliance DQ record, matched by id or ops id."""
    h.store.seed("drivers", "driver_aaa", {
        "tenant_id": T, "driver_id": "driver_aaa", "external_refs": {"ops_driver_id": "d1"},
        "cdl_expiry_date": "2027-05-01", "medical_card_expiry_date": "2026-11-02",
        "tanker_endorsement_expiry_date": "2027-01-01",
    })
    h.store.seed("drivers", "d2", {
        "tenant_id": T, "driver_id": "d2", "cdl_expiry_date": "2027-05-01",
        "medical_card_expiry_date": "2027-06-01", "tanker_endorsement_expiry_date": "2026-09-30",  # lapsed
    })
    h.store.seed("drivers", "other", {
        "tenant_id": "tenant-2", "driver_id": "x", "external_refs": {"ops_driver_id": "d3"},
        "cdl_expiry_date": "2026-10-10", "tanker_endorsement_expiry_date": "2027-01-01",
    })
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    tray = {d["driver_id"]: d for d in snap["trays"]["drivers"]}
    assert tray["d1"]["tanker_endorsement"] is True
    assert tray["d1"]["nearest_expiry"] == {"kind": "medical_card", "expires_on": "2026-11-02"}
    assert tray["d2"]["tanker_endorsement"] is False
    assert tray["d2"]["nearest_expiry"] == {"kind": "tanker", "expires_on": "2026-09-30"}
    # No record in this tenant: unknown, not "no".
    assert tray["d3"]["tanker_endorsement"] is None and tray["d3"]["nearest_expiry"] is None
    h.store.fail_on("search_documents", "drivers")
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    tray = {d["driver_id"]: d for d in snap["trays"]["drivers"]}
    assert set(tray) == {"d1", "d2", "d3"}
    assert all(d["tanker_endorsement"] is None and d["nearest_expiry"] is None for d in tray.values())


async def test_hos_figures_today_only(h):
    h.seed_order("o1")
    await h.lane_with("T1", "o1", driver_id="d1")
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert snap["lanes"][0]["driver"]["hos"]["remaining_drive_time"]["value"] == 10.0
    h.seed_order("o2", day=TOMORROW)
    await h.lane_with("T2", "o2", driver_id="d2", day=TOMORROW)
    snap = await h.service.snapshot(T, TOMORROW, mode="active_gated", tz=TZ)
    assert snap["lanes"][0]["driver"]["hos"] is None


async def test_stale_lanes_are_revalidated_and_written_back_without_a_version_bump(h):
    h.seed_order("o1")
    await h.lane_with("T1", "o1", driver_id="d1")
    doc = h.store.docs[DRAFTS][draft_doc_id(T, TODAY)]
    version = doc["lanes"]["T1"]["version"]
    doc["lanes"]["T1"]["checks_stale"] = True
    h.store.poke(ORDERS, "o1", status="cancelled")
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert "order_terminal" in {c["reason_code"] for c in snap["lanes"][0]["checks"]}
    stored = h.draft().lanes["T1"]
    assert stored.version == version and stored.checks_stale is False
    assert "order_terminal" in {c.reason_code for c in stored.checks}


async def test_stale_write_back_skipped_when_the_lane_moved(h):
    h.seed_order("o1")
    await h.lane_with("T1", "o1", driver_id="d1")
    doc_id = draft_doc_id(T, TODAY)
    h.store.docs[DRAFTS][doc_id]["lanes"]["T1"]["checks_stale"] = True

    async def bump(op, index, _id):
        if op == "atomic_update" and index == DRAFTS:
            h.store.hooks.clear()
            h.store.docs[DRAFTS][doc_id]["lanes"]["T1"]["version"] += 1

    h.store.hooks.append(bump)
    await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert h.draft().lanes["T1"].checks_stale is True


# ---- validate, history, names, time zone ----------------------------------


async def test_validate_batch_and_metric(h):
    h.seed_order("o1")
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    await h.run("add_lane", truck_id="T2", lanes=("T2",))
    body = ValidateBody.model_validate({"item": {"kind": "order", "ids": ["o1"]}, "candidates": ["T1", "T2"]})
    result = await h.service.validate(T, TODAY, body, tz=TZ)
    assert set(result["results"]) == {"T1", "T2"} and result["results"]["T1"]["outcome"] == "warn"
    assert any(m[0] == "board.validate.ms" and m[2]["kind"] == "batch" for m in h.telemetry.metrics)
    assert h.draft().order_ids == []


async def test_history_pagination_and_lane_filter(h):
    for t in ("T1", "T2"):
        await h.run("add_lane", truck_id=t, lanes=(t,))
    for d in ("d1", "d2", "d3"):
        h.now = h.now + timedelta(seconds=1)
        await h.run("pair_driver", truck_id="T1", driver_id=d, lanes=("T1",))
    page1 = await h.service.history(T, TODAY, HistoryQuery(size=2), tz=TZ)
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    page2 = await h.service.history(T, TODAY, HistoryQuery(size=2, cursor=page1["next_cursor"]), tz=TZ)
    seen = [i["command_id"] for i in page1["items"] + page2["items"]]
    assert len(seen) == len(set(seen))
    t2 = await h.service.history(T, TODAY, HistoryQuery(truck_id="T2"), tz=TZ)
    assert [i["type"] for i in t2["items"]] == ["add_lane"]
    assert all(i["actor_name"] == "ana" for i in t2["items"])
    other = await h.service.history("tenant-2", TODAY, HistoryQuery(), tz=TZ)
    assert other["items"] == []


async def test_resolve_actor_name(h):
    assert await h.service.resolve_actor_name(T, "user-1") == "ana"
    assert await h.service.resolve_actor_name(T, "user-1") == "ana"
    assert h.name_calls == [(T, "user-1")]  # cached
    assert await h.service.resolve_actor_name(T, "nobody") == "Another dispatcher"
    assert await h.service.resolve_actor_name("tenant-2", "user-1") == "Another dispatcher"
    h.names[(T, "long")] = "x" * 80 + "@example.com"
    assert await h.service.resolve_actor_name(T, "long") == "x" * 64

    async def broken(tenant_id, user_id):
        raise RuntimeError("db down")

    svc = DispatchBoardService(es_service=h.store, validation=h.validation, name_lookup=broken)
    assert await svc.resolve_actor_name(T, "user-1") == "Another dispatcher"


def test_tenant_time_zone_default_and_override():
    class Settings:
        timezone = "America/Denver"

    assert DispatchBoardService.timezone_for(T, None) == "America/Chicago"
    assert DispatchBoardService.timezone_for(T, Settings()) == "America/Denver"


async def test_draft_keeps_its_captured_time_zone(h):
    await h.run("add_lane", truck_id="T1", lanes=("T1",))
    assert h.draft().timezone == TZ
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz="America/Denver")
    assert snap["timezone"] == TZ  # E11


# ---- P6: version CAS ------------------------------------------------------


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(schedule=st.lists(st.integers(0, 3), min_size=4, max_size=12), second=st.sampled_from(["pair_driver", "assign_orders", "remove_lane"]))
def test_p6_two_commands_from_one_version_at_most_one_commits(schedule, second):
    async def scenario():
        h = Harness()
        h.seed_order("o1")
        await h.run("add_lane", truck_id="T1", lanes=("T1",))
        v = h.versions("T1")
        a = h.command("pair_driver", truck_id="T1", driver_id="d1", versions=v)
        if second == "pair_driver":
            b = h.command("pair_driver", truck_id="T1", driver_id="d2", versions=v)
        elif second == "assign_orders":
            b = h.command("assign_orders", order_ids=["o1"], truck_id="T1", versions=v)
        else:
            b = h.command("remove_lane", truck_id="T1", versions=v)
        h.store.set_yield_schedule(schedule, repeat=True)
        results = await asyncio.gather(h.send(a), h.send(b), return_exceptions=True)
        ok = [r for r in results if not isinstance(r, Exception)]
        errors = [r for r in results if isinstance(r, Exception)]
        assert len(ok) <= 1
        assert all(isinstance(e, AppException) and e.error_code == ErrorCode.BOARD_LANE_CONFLICT for e in errors)
        assert h.draft().draft_version == 1 + len(ok)

    asyncio.run(scenario())


# -- K15 copies (phase 7 review P7-1) ---------------------------------------


async def test_drafted_orders_do_not_use_up_the_tray_page(h, monkeypatch):
    from fuel.services import dispatch_board_service as svc

    monkeypatch.setattr(svc, "TRAY_LIMIT", 3)
    for o in ("d1", "d2", "d3", "u1", "u2"):
        h.seed_order(o)
    await h.lane_with("T1", "d1", "d2", "d3")
    snap = await h.service.snapshot(T, TODAY, mode="active_gated", tz=TZ)
    assert [o["order_id"] for o in snap["trays"]["orders"]] == ["u1", "u2"]
    assert snap["trays"]["orders_truncated"] is False


async def test_scoped_command_copy_leaves_the_read_draft_alone(h):
    for o in ("o1", "o2", "o3"):
        h.seed_order(o)
    await h.lane_with("T1", "o1", "o2")
    await h.lane_with("T2", "o3")
    draft = h.draft()
    before = draft.model_dump(mode="json")
    load_id = draft.lanes["T1"].loads[0].load_id
    cmd = h.command("move_stops", order_ids=["o3"], truck_id="T1", target={"load_id": load_id, "index": 0}, lanes=("T1", "T2"))
    ctx = await h.service._context_for(draft, ["T1", "T2"], extra_orders=["o3"])
    scoped = engine.apply(draft, cmd, ctx, scope=["T1", "T2"])
    assert draft.model_dump(mode="json") == before
    full = engine.apply(draft, cmd, ctx)
    assert scoped.draft.model_dump(mode="json") == full.draft.model_dump(mode="json")
    assert scoped.draft.lanes["T1"] is not draft.lanes["T1"]


async def test_scoped_apply_outside_its_scope_raises(h):
    for o in ("o1", "o2"):
        h.seed_order(o)
    await h.lane_with("T1", "o1")
    await h.lane_with("T2", "o2")
    draft = h.draft()
    cmd = h.command("move_stops", order_ids=["o2"], truck_id="T1", target={"load_id": "new"}, lanes=("T1", "T2"))
    ctx = await h.service._context_for(draft, ["T1", "T2"], extra_orders=["o2"])
    with pytest.raises(engine.ScopeExceeded):
        engine.apply(draft, cmd, ctx, scope=["T1"])


async def test_command_outside_its_lane_estimate_reruns_on_a_fresh_full_copy(h, monkeypatch):
    for o in ("o1", "o2"):
        h.seed_order(o)
    await h.lane_with("T1", "o1")
    await h.lane_with("T2", "o2")
    calls = []
    real = engine.apply

    def spy(draft, command, ctx, **kw):
        calls.append(kw.get("scope"))
        return real(draft, command, ctx, **kw)

    monkeypatch.setattr(engine, "apply", spy)
    # An estimate that misses the source lane (no current command does this).
    monkeypatch.setattr(h.service, "_touched_estimate", lambda d, c: [c.truck_id])
    result = await h.run("move_stops", order_ids=["o2"], truck_id="T1", target={"load_id": "new"}, lanes=("T1", "T2"))
    assert [set(s) if s is not None else None for s in calls] == [{"T1"}, None]
    assert {l["truck_id"] for l in result["lanes"]} == {"T1", "T2"}
    draft = h.draft()
    assert draft.order_index["o2"] == "T1"
    assert draft.lanes["T2"].loads == []
    assert len(committed_logs(h)) == 5  # 2 add_lane + 2 assign + this move, logged once


def test_clone_is_an_equal_unshared_copy():
    lane = engine.Lane(truck_id="T1", version=3, driver_id="d1")
    draft = BoardDraft(tenant_id=T, service_date=TODAY, timezone=TZ, lanes={"T1": lane})
    copy_ = engine.clone(draft)
    assert copy_ == draft and copy_.lanes["T1"] is not lane
