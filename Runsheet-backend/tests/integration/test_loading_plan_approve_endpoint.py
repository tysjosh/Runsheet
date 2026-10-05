"""Approving a loading plan through the real endpoint applies it (design test
plan "Integration", L1065-1073; R1, R3, R7.3-R7.5, R9, R11, K9, K10).

The approval is created by ``CompartmentLoadingAgent.evaluate`` and
``_route_proposal`` under ``suggest-only``; ``POST /api/agent/approvals/{id}/
approve`` then runs the real approval service, protocol and executor over the
in-memory store. The LLM is mocked to raise.
"""

from __future__ import annotations

import pytest

from fuel.services.loading_plan_executor import MESSAGE_TEMPLATES, REASON_ORDER_CHANGED_SINCE_PLAN
from tests.integration._loading_plan_world import (
    DISPATCHER,
    OTHER_TENANT,
    RUN_ID,
    TENANT,
    TRUCK_ID,
    World,
    no_llm,
)
from tests.unit._loading_plan_fakes import (
    APPROVALS,
    EVENTS,
    ORDERS,
    PLANS,
    fuel_order_doc,
)

ORDER_IDS = ("ord-1", "ord-2")


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    no_llm(monkeypatch)


async def _world(monkeypatch) -> World:
    orders = [
        fuel_order_doc("ord-1", tenant_id=TENANT, gallons_requested=400.0,
                       assigned_driver_id="drv-7"),
        fuel_order_doc("ord-2", tenant_id=TENANT, status="placed", gallons_requested=300.0),
    ]
    world = await World(monkeypatch, orders).start()
    proposals = await world.propose(ORDER_IDS)
    assert len(proposals) == 1
    assert len(world.entries("pending")) == 1
    return world


def _only_entry(world: World) -> dict:
    (entry,) = world.entries()
    return entry


def _assert_t2_saw_nothing(world: World) -> None:
    assert world.sock_t2.non_handshake() == []


async def test_approve_applies_the_plan(monkeypatch):
    world = await _world(monkeypatch)
    entry = _only_entry(world)
    action_id = entry["action_id"]
    plan_id = entry["parameters"]["plan_id"]
    assert entry["parameters"]["truck_id"] == TRUCK_ID
    assert set(entry["parameters"]["order_snapshots"]) == set(ORDER_IDS)
    world.sock_t1.messages.clear()

    # The reviewer comes from the session, not the query (pattern 8841929).
    response = await world.approve(action_id, reviewer_id="someone-else")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "executed"
    stored = world.h.entry(action_id)
    assert stored["status"] == "executed"
    assert stored["reviewed_by"] == DISPATCHER
    assert stored["execution_result"]["actor_user_id"] == DISPATCHER
    assert sorted(stored["execution_result"]["applied_order_ids"]) == list(ORDER_IDS)

    for oid in ORDER_IDS:
        order = world.h.order(oid)
        assert order["status"] == "scheduled"
        assert order["assigned_run_id"] == RUN_ID
        assert order["assigned_asset_id"] == TRUCK_ID
    # Driver assignment is the dispatcher's, never the executor's.
    assert world.h.order("ord-1")["assigned_driver_id"] == "drv-7"
    assert world.h.order("ord-2")["assigned_driver_id"] is None
    plan = world.h.plan(plan_id)
    assert plan["execution_status"] == "succeeded"
    assert plan["applied_by"] == DISPATCHER
    assert plan["status"] == "scheduled"

    (activity,) = world.activity_entries("loading_plan_execution")
    assert activity["outcome"] == "executed"
    assert activity["tenant_id"] == TENANT
    assert activity["user_id"] == DISPATCHER
    assert activity["details"]["plan_id"] == plan_id

    t1_types = world.sock_t1.types()
    assert "approval_approved" in t1_types
    assert "approval_execution_updated" in t1_types
    _assert_t2_saw_nothing(world)
    assert len(world.h.execute_calls) == 1


async def test_second_approve_returns_the_stored_entry_without_writes(monkeypatch):
    world = await _world(monkeypatch)
    action_id = _only_entry(world)["action_id"]
    first = await world.approve(action_id)
    assert first.status_code == 200, first.text
    stored = world.h.entry(action_id)
    mark = world.h.mark()

    second = await world.approve(action_id)

    assert second.status_code == 200, second.text
    assert second.json()["status"] == "executed"
    assert second.json()["execution_result"] == stored["execution_result"]
    assert world.h.writes_since(mark, ORDERS, EVENTS, PLANS, APPROVALS) == []
    assert world.h.entry(action_id) == stored
    _assert_t2_saw_nothing(world)


async def test_stale_order_gives_409_with_the_reason(monkeypatch):
    world = await _world(monkeypatch)
    entry = _only_entry(world)
    action_id = entry["action_id"]
    plan_id = entry["parameters"]["plan_id"]
    # Someone edits the order's volume after the plan was proposed.
    world.store.poke(ORDERS, "ord-2", gallons_requested=950.0)

    response = await world.approve(action_id)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error_code"] == "LOADING_PLAN_EXECUTION_FAILED"
    details = body["details"]
    assert details["reason"] == REASON_ORDER_CHANGED_SINCE_PLAN == "order_changed_since_plan"
    assert details["writes_made"] is False
    assert details["plan_id"] == plan_id
    # A fixed template, no exception text.
    assert body["message"] == MESSAGE_TEMPLATES[REASON_ORDER_CHANGED_SINCE_PLAN].format(plan_id=plan_id)
    assert "Traceback" not in response.text and "Error(" not in response.text

    stored = world.h.entry(action_id)
    assert stored["execution_result"]["reason"] == "order_changed_since_plan"
    assert stored["execution_result"]["writes_made"] is False
    (activity,) = world.activity_entries("loading_plan_execution")
    assert activity["details"]["reason"] == "order_changed_since_plan"
    for oid in ORDER_IDS:
        assert world.h.links(oid) == (None, None)
    _assert_t2_saw_nothing(world)


async def test_driver_role_is_refused_before_the_service(monkeypatch):
    world = await _world(monkeypatch)
    action_id = _only_entry(world)["action_id"]
    world.roles = ["driver"]
    mark = world.h.mark()

    response = await world.approve(action_id)

    assert response.status_code == 403, response.text
    assert world.h.status(action_id) == "pending"
    assert world.h.entry(action_id)["reviewed_by"] is None
    assert world.h.execute_calls == []
    assert world.h.writes_since(mark) == []


async def test_include_unresolved_lists_an_incomplete_entry(monkeypatch):
    world = await _world(monkeypatch)
    action_id = _only_entry(world)["action_id"]
    # ord-1 is committed first; every write to ord-2 then fails.
    world.store.fail_on("atomic_update", ORDERS, doc_id="ord-2", times=50)

    response = await world.approve(action_id)

    assert response.status_code == 409, response.text
    assert response.json()["details"]["writes_made"] is True
    assert world.h.status(action_id) == "incomplete"

    unresolved = await world.list_approvals(include_unresolved="true")
    assert unresolved.status_code == 200, unresolved.text
    listed = {i["action_id"]: i["status"] for i in unresolved.json()["items"]}
    assert listed == {action_id: "incomplete"}

    pending_only = await world.list_approvals()
    assert pending_only.status_code == 200, pending_only.text
    assert [i["action_id"] for i in pending_only.json()["items"]] == []
    _assert_t2_saw_nothing(world)


async def test_other_tenant_socket_stays_silent_through_proposal(monkeypatch):
    world = await _world(monkeypatch)

    assert "approval_created" in world.sock_t1.types()
    _assert_t2_saw_nothing(world)
    assert OTHER_TENANT != TENANT
