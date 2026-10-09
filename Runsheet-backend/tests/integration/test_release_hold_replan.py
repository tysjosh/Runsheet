"""Hold → release → re-plan, end to end (OI-18: OQ10 + OQ11).

Real ``ApprovalQueueService`` + ``LoadingPlanExecutor`` + ``OrderService`` +
``CompartmentLoadingAgent`` truck selection over one in-memory store:

1. Plan A is approved and applied: the order is linked to truck-1, and
   truck-1 is no longer offered to the loader (committed, OQ11).
2. The order is put on hold and released: the links are cleared (OQ10) and
   truck-1 is offered again.
3. Plan B puts the order on truck-2: the approval is not blocked by A's
   executed hold, and the executor links the order to B's run and truck.
"""

from __future__ import annotations

from tests.unit._loading_plan_fakes import (
    ApprovalHarness,
    loading_agent,
    order_fixture,
    seed_fleet,
)

T = "tenant-1"


async def _offered(agent) -> set:
    return set(await agent._query_trucks_with_equipment_check(T))


async def test_release_frees_the_order_and_the_truck_for_a_new_plan():
    h = ApprovalHarness([order_fixture("o1", status="confirmed", tenant_id=T)])
    h.svc.set_order_repository(h.repo)  # as bootstrap wires it
    seed_fleet(h.store, tenant_id=T, trucks=("truck-1", "truck-2"))
    agent = loading_agent(h.store)

    h.add_plan("A", ["o1"], truck_id="truck-1", run_id="run-A")
    assert (await h.approve("A"))["status"] == "executed"
    assert h.links("o1") == ("run-A", "truck-1")
    assert h.order("o1")["status"] == "scheduled"
    assert await _offered(agent) == {"truck-2"}

    order = await h.order_service.place_on_hold(h.order("o1"), "site closed", "user-1")
    await h.order_service.release_hold(order, "user-1")
    assert h.links("o1") == (None, None)
    assert h.order("o1")["status"] == "placed"
    assert await _offered(agent) == {"truck-1", "truck-2"}

    h.add_plan("B", ["o1"], truck_id="truck-2", run_id="run-B")
    b = await h.approve("B")
    assert b["status"] == "executed", b.get("execution_result")
    assert h.links("o1") == ("run-B", "truck-2")
    assert h.order("o1")["status"] == "scheduled"
    assert await _offered(agent) == {"truck-1"}
