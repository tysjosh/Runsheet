"""After a plan is applied, the next loading run does not propose its orders
again (design test plan "Next run"; K11, R8.1, R8.4).

The plan is approved through the real endpoint, then
``CompartmentLoadingAgent.evaluate`` runs again with the same priority list.
The LLM is mocked to raise.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from Agents.overlay.base_overlay_agent import (
    CYCLE_METRIC_DEGRADATION_REASONS,
    DEGRADATION_KIND_NO_INPUT,
)
from tests.integration._loading_plan_world import TENANT, World, no_llm
from tests.unit._loading_plan_fakes import ORDERS, PLANS, fuel_order_doc, seed_fleet

APPLIED = ("ord-1", "ord-2")
# Big tanks so the committed-draw read runs on every known tank.
TANKS = {f"tank-{oid}": (5000.0, 1000.0) for oid in ("ord-1", "ord-2", "ord-3")}


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    no_llm(monkeypatch)


async def _applied_world(monkeypatch) -> World:
    orders = [
        fuel_order_doc("ord-1", tenant_id=TENANT, gallons_requested=400.0),
        fuel_order_doc("ord-2", tenant_id=TENANT, gallons_requested=300.0),
    ]
    world = await World(monkeypatch, orders, tanks=TANKS).start()
    await world.propose(APPLIED)
    (entry,) = world.entries("pending")
    response = await world.approve(entry["action_id"])
    assert response.status_code == 200, response.text
    for oid in APPLIED:
        assert world.h.order(oid)["status"] == "scheduled"
        assert world.h.order(oid)["assigned_run_id"]
    return world


def _plan_order_ids(plan: Dict[str, Any]) -> List[str]:
    return [a.get("order_id") for a in plan.get("assignments") or []]


def _proposal_order_ids(proposals) -> List[str]:
    return [
        oid
        for p in proposals
        for action in p.actions
        for oid in (action.get("parameters") or {}).get("order_ids") or []
    ]


async def test_next_run_with_no_new_order_reports_all_committed(monkeypatch):
    world = await _applied_world(monkeypatch)
    plans_before = set(world.store.docs[PLANS])
    entries_before = len(world.entries())

    proposals = await world.propose(APPLIED)

    assert proposals == []
    assert set(world.store.docs[PLANS]) == plans_before
    assert len(world.entries()) == entries_before
    reasons = world.agent._cycle_metrics[CYCLE_METRIC_DEGRADATION_REASONS]
    assert [r["reason_code"] for r in reasons] == ["all_loadable_orders_committed"]
    assert reasons[0]["kind"] == DEGRADATION_KIND_NO_INPUT
    assert reasons[0]["committed_orders"] == len(APPLIED)


async def test_next_run_plans_only_the_new_uncommitted_order(monkeypatch):
    world = await _applied_world(monkeypatch)
    plans_before = set(world.store.docs[PLANS])
    # R8.4: a new order on a different tank arrives after the apply.
    world.store.seed(ORDERS, "ord-3", fuel_order_doc(
        "ord-3", tenant_id=TENANT, gallons_requested=250.0))
    # OI-18 (OQ11): truck-1 holds the applied, undispatched plan, so the new
    # order needs another truck.
    seed_fleet(world.store, tenant_id=TENANT, trucks=("truck-2",))

    proposals = await world.propose((*APPLIED, "ord-3"))

    assert _proposal_order_ids(proposals) == ["ord-3"]
    new_plans = [
        world.store.docs[PLANS][pid]
        for pid in world.store.docs[PLANS]
        if pid not in plans_before
    ]
    assert len(new_plans) == 1
    assert _plan_order_ids(new_plans[0]) == ["ord-3"]
    assert new_plans[0]["truck_id"] == "truck-2"
    for plan in new_plans:
        assert not set(_plan_order_ids(plan)) & set(APPLIED)
    (pending,) = world.entries("pending")
    assert pending["parameters"]["order_ids"] == ["ord-3"]
    assert set(pending["parameters"]["order_snapshots"]) == {"ord-3"}
    # The applied orders are untouched by the second run.
    for oid in APPLIED:
        assert world.h.order(oid)["status"] == "scheduled"


async def test_next_run_does_not_put_a_new_order_on_the_committed_truck(monkeypatch):
    """OI-18 (OQ11): the only truck holds an applied plan, so nothing is planned."""
    world = await _applied_world(monkeypatch)
    plans_before = set(world.store.docs[PLANS])
    world.store.seed(ORDERS, "ord-3", fuel_order_doc(
        "ord-3", tenant_id=TENANT, gallons_requested=250.0))
    proposals = await world.propose((*APPLIED, "ord-3"))
    assert proposals == []
    assert set(world.store.docs[PLANS]) == plans_before
