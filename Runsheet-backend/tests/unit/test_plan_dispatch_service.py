"""Tests for the dispatcher-plan to driver-work bridge.

Runs the real ``FuelOrderRepository`` and ``OrderService`` (default clock)
over the shared in-memory document store, so the claim CAS, the guarded
transitions and release-by-claim-id (loading-plan-executor design K5/K5a,
FREEZE rules 1-2, test plan T-U15) are exercised end to end.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from driver.services.work_service import DriverWorkService
from errors.exceptions import AppException
from fuel.order_repository import FuelOrderRepository
from fuel.services.order_service import OrderService
from fuel.services.plan_dispatch_service import FuelPlanDispatchService
from persistence.plan_execution_lock import PLAN_EXECUTION_LOCK, PlanExecutionLock
from tests.unit._loading_plan_fakes import ORDERS, InMemoryDocumentStore, fuel_order_doc

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
T = "tenant-1"
THREE = ["ord-1", "ord-2", "ord-3"]


def _order(order_id: str = "ord-1", *, status: str = "scheduled", **overrides) -> dict:
    overrides.setdefault("customer_tank_id", "tank-1")
    return fuel_order_doc(order_id, tenant_id=T, status=status, **overrides)


def _plan(*, order_ids=("ord-1",), plan_id="plan-1", run_id="run-1", truck_id="truck-1") -> dict:
    assignments = []
    for order_id in order_ids or [None]:
        assignment = {
            "station_id": "tank-1",
            "compartment_id": "c-1",
            "fuel_grade": "DIESEL_2",
            "quantity_liters": 1000,
        }
        if order_id:
            assignment["order_id"] = order_id
        assignments.append(assignment)
    return {
        "plan_id": plan_id,
        "run_id": run_id,
        "truck_id": truck_id,
        "tenant_id": T,
        "status": "proposed",
        "assignments": assignments,
    }


def _route(*, order_ids=("ord-1",), route_id="route-1", plan_id="plan-1", run_id="run-1", truck_id="truck-1") -> dict:
    return {
        "route_id": route_id,
        "plan_id": plan_id,
        "run_id": run_id,
        "truck_id": truck_id,
        "tenant_id": T,
        "stops": [
            {
                "station_id": "tank-1",
                "order_ids": list(order_ids),
                "eta": "2026-07-30T09:00:00+00:00",
                "drop": {"DIESEL_2": 1000},
                "sequence": 0,
            }
        ],
    }


class FakeDriverRepository:
    def __init__(self, drivers=None):
        self.drivers = (
            [
                {"driver_id": "driver-1", "tenant_id": T, "assigned_truck_id": "truck-1", "status": "active"},
                {"driver_id": "driver-2", "tenant_id": T, "assigned_truck_id": "truck-2", "status": "active"},
            ]
            if drivers is None
            else drivers
        )

    async def search(self, tenant_id, **filters):
        truck = filters.get("assigned_truck_id")
        found = [d for d in self.drivers if truck is None or d.get("assigned_truck_id") == truck]
        return {"drivers": found, "total": len(found)}


class Harness:
    """One dispatch service wired over a fresh in-memory store."""

    def __init__(
        self,
        orders,
        *,
        plans=None,
        routes=None,
        drivers=None,
        existing_execution=None,
        plan_lock=None,
    ):
        self.store = InMemoryDocumentStore()
        for order in orders:
            self.store.seed(ORDERS, order["order_id"], order)
        order_ids = [o["order_id"] for o in orders]
        self.plans = plans or [_plan(order_ids=order_ids)]
        for plan in self.plans:
            self.store.seed("mvp_load_plans", plan["plan_id"], plan)
        for route in routes or [_route(order_ids=order_ids)]:
            self.store.seed("mvp_routes", route["route_id"] or "route-blank", route)
        if existing_execution:
            self.store.seed("mvp_plan_executions", existing_execution["execution_id"], existing_execution)

        self.repo = FuelOrderRepository(self.store)
        self.claims, self.releases = [], []
        claim, release = self.repo.claim_assignment, self.repo.release_assignment

        async def claim_spy(tenant_id, order_id, **kwargs):
            self.claims.append(order_id)
            return await claim(tenant_id, order_id, **kwargs)

        async def release_spy(tenant_id, order_id, **kwargs):
            self.releases.append(order_id)
            return await release(tenant_id, order_id, **kwargs)

        self.repo.claim_assignment = claim_spy
        self.repo.release_assignment = release_spy

        self.order_service = OrderService(order_repo=self.repo, ws_manager=AsyncMock())  # default clock
        self.push = AsyncMock()
        self.order_service.subscribe("order.dispatched", self.push)
        self.execution = AsyncMock()
        self.execution.create_execution = AsyncMock(return_value={"execution_id": "execution-1"})
        self.driver_ws = AsyncMock()
        self.lock = plan_lock or PlanExecutionLock(use_postgres=False, timeout_seconds=2)
        self.service = FuelPlanDispatchService(
            es_service=self.store,
            order_repository=self.repo,
            order_service=self.order_service,
            driver_repository=FakeDriverRepository(drivers),
            execution_service=self.execution,
            driver_ws_manager=self.driver_ws,
            clock=lambda: NOW,
            plan_lock=self.lock,
        )

    async def dispatch(self, plan=None):
        return await self.service.dispatch(
            tenant_id=T, plan_doc=plan or self.plans[0], actor_user_id="dispatcher-1"
        )

    async def dispatch_conflict(self, plan=None) -> AppException:
        with pytest.raises(AppException) as raised:
            await self.dispatch(plan)
        assert raised.value.status_code == 409
        return raised.value

    def order(self, order_id):
        return self.store.doc(ORDERS, order_id)

    def links(self, order_id):
        doc = self.order(order_id)
        return (doc.get("assigned_run_id"), doc.get("assigned_asset_id"), doc.get("assigned_claim_id"))

    def on(self, op, index, doc_id, action, *, nth=1):
        """Run ``action`` just before the ``nth`` matching store operation."""
        seen = []

        def hook(o, i, d):
            if (o, i, d) == (op, index, doc_id):
                seen.append(1)
                if len(seen) == nth:
                    return action()

        self.store.hooks.append(hook)

    def assert_nothing_dispatched(self):
        assert self.store.writes("mvp_routes") == []
        assert self.store.writes("mvp_load_plans") == []
        self.execution.create_execution.assert_not_awaited()
        self.push.assert_not_awaited()
        self.driver_ws.send_assignment.assert_not_awaited()
        assert self.store.events() == []

    def assert_unlinked(self, *order_ids):
        for order_id in order_ids:
            assert self.links(order_id) == (None, None, None), order_id


def _three(status="confirmed", **per_order):
    return [_order(oid, status=status, **per_order.get(oid, {})) for oid in THREE]


def _stale_projection(monkeypatch, harness, **overrides_by_order):
    """Serve list_for_tenant from a relational projection that lags es_documents."""
    import commerce.services.commerce_persistence_bridge as bridge

    async def fake_read_hybrid_search(aggregate_type, tenant_id, **kwargs):
        assert aggregate_type == "fuel_order"
        items = []
        for order_id in sorted(harness.store.docs[ORDERS]):
            doc = harness.order(order_id)
            doc.update(overrides_by_order.get(order_id, {}))
            items.append(doc)
        return {"items": items, "total": len(items), "page": 1, "size": len(items)}

    monkeypatch.setattr(bridge, "read_hybrid_search", fake_read_hybrid_search)


# ---------------------------------------------------------------------------
# Happy paths and existing behaviour
# ---------------------------------------------------------------------------


async def test_dispatch_links_transitions_and_notifies():
    h = Harness([_order()])

    result = await h.dispatch()

    stored = h.order("ord-1")
    assert stored["status"] == "dispatched"
    assert stored["assigned_driver_id"] == "driver-1"
    assert stored["assigned_asset_id"] == "truck-1"
    assert stored["assigned_run_id"] == "run-1"
    assert stored["assigned_claim_id"]
    assert [e["event_type"] for e in h.store.events()] == ["order_assigned", "order_dispatched"]
    h.push.assert_awaited_once()
    h.driver_ws.send_assignment.assert_awaited_once_with(
        "driver-1",
        {
            "plan_id": "plan-1",
            "run_id": "run-1",
            "truck_id": "truck-1",
            "route_ids": ["route-1"],
            "order_ids": ["ord-1"],
        },
    )
    h.execution.create_execution.assert_awaited_once()
    assert result.newly_dispatched == 1
    assert result.order_ids == ["ord-1"]
    assert h.store.writes()[-1] == ("update_document", "mvp_load_plans", "plan-1")
    assert h.releases == []


async def test_approved_plan_is_visible_in_driver_work_read_model():
    h = Harness([_order()])

    await h.dispatch()
    work = await DriverWorkService(es_service=h.store, order_repository=h.repo).list_work(
        T, "driver-1", statuses=("dispatched", "in_transit")
    )

    assert work["pagination"]["total"] == 1
    assert work["data"][0]["order_id"] == "ord-1"
    assert work["data"][0]["status"] == "dispatched"


async def test_confirmed_order_is_scheduled_then_dispatched_in_one_call():
    # Regression guard for pass-2 finding 1: a chained guarded transition.
    h = Harness([_order(status="confirmed")])

    await h.dispatch()

    assert [e["event_type"] for e in h.store.events()] == [
        "order_assigned",
        "order_scheduled",
        "order_dispatched",
    ]
    assert h.order("ord-1")["status"] == "dispatched"


async def test_executor_prelinked_scheduled_orders_dispatch_as_already_linked():
    # R9.5 / K12: an executor-applied plan; links and the attempt id are kept.
    linked = {"assigned_run_id": "run-1", "assigned_asset_id": "truck-1", "assigned_claim_id": "attempt-A"}
    h = Harness([_order(oid, status="scheduled", **linked) for oid in THREE])

    result = await h.dispatch()

    assert result.newly_dispatched == 3
    for oid in THREE:
        assert h.order(oid)["status"] == "dispatched"
        assert h.links(oid) == ("run-1", "truck-1", "attempt-A")
        assert h.order(oid)["assigned_driver_id"] == "driver-1"
    assert h.releases == []


async def test_retry_is_idempotent_for_active_order_and_execution():
    active = _order(
        status="dispatched",
        assigned_driver_id="driver-1",
        assigned_asset_id="truck-1",
        assigned_run_id="run-1",
    )
    h = Harness([active], existing_execution={"execution_id": "execution-existing", "tenant_id": T, "plan_id": "plan-1", "route_id": "route-1"})

    result = await h.dispatch()

    assert result.newly_dispatched == 0
    assert result.already_dispatched == 1
    assert h.store.events() == []
    assert h.claims == []
    h.push.assert_not_awaited()
    h.execution.create_execution.assert_not_awaited()
    h.driver_ws.send_assignment.assert_awaited_once()


async def test_legacy_ambiguous_stop_requires_plan_regeneration():
    h = Harness(
        [_order("ord-1"), _order("ord-2")],
        plans=[_plan(order_ids=None)],
        routes=[_route(order_ids=[])],
    )

    exc = await h.dispatch_conflict()

    assert exc.details["reason"] == "plan_order_ambiguous"


async def test_dispatch_requires_exactly_one_active_driver():
    h = Harness([_order()], drivers=[])

    with pytest.raises(AppException) as raised:
        await h.dispatch()

    assert raised.value.error_code.value == "DRIVER_UNAVAILABLE"
    assert raised.value.details["reason"] == "no_active_driver_for_truck"


def test_default_lock_is_the_shared_plan_execution_lock():
    service = FuelPlanDispatchService(
        es_service=object(),
        order_repository=object(),
        order_service=object(),
        driver_repository=object(),
        execution_service=object(),
    )
    assert service._plan_lock is PLAN_EXECUTION_LOCK


# ---------------------------------------------------------------------------
# Preflight refusals before any write
# ---------------------------------------------------------------------------


async def test_order_committed_elsewhere_is_refused_before_any_write():
    h = Harness(_three(**{"ord-2": {"assigned_run_id": "run-other", "assigned_asset_id": "truck-9"}}))

    exc = await h.dispatch_conflict()

    assert exc.details == {"reason": "order_committed_elsewhere", "order_id": "ord-2"}
    assert "committed to another run" in exc.message
    assert h.claims == []
    assert h.store.writes() == []


async def test_blank_route_id_refuses_before_any_claim():
    h = Harness(_three(), routes=[_route(order_ids=THREE, route_id="")])

    exc = await h.dispatch_conflict()

    assert exc.details["reason"] == "route_identity_incomplete"
    assert h.claims == []
    assert h.store.writes() == []


async def test_lock_timeout_is_plan_execution_busy_with_zero_writes():
    lock = PlanExecutionLock(use_postgres=False, timeout_seconds=0.05)
    h = Harness(_three(), plan_lock=lock)
    inside, release = asyncio.Event(), asyncio.Event()

    async def other_holder():
        async with lock.hold(T):
            inside.set()
            await release.wait()

    task = asyncio.create_task(other_holder())
    await inside.wait()
    try:
        exc = await h.dispatch_conflict()
    finally:
        release.set()
        await task

    assert exc.details["reason"] == "plan_execution_busy"
    assert "retry" in exc.message
    assert h.claims == []
    assert h.store.writes() == []


# ---------------------------------------------------------------------------
# Claim refusals: earlier fresh claims are released, nothing else is written
# ---------------------------------------------------------------------------


async def test_third_order_linked_elsewhere_after_preflight_releases_first_two():
    h = Harness(_three())
    h.on("atomic_update", ORDERS, "ord-3", lambda: h.store.poke(ORDERS, "ord-3", assigned_run_id="run-other", assigned_asset_id="truck-9"))

    exc = await h.dispatch_conflict()

    assert exc.details == {"reason": "order_committed_elsewhere", "order_id": "ord-3"}
    h.assert_unlinked("ord-1", "ord-2")
    assert h.order("ord-3")["assigned_run_id"] == "run-other"
    assert sorted(h.releases) == ["ord-1", "ord-2"]
    h.assert_nothing_dispatched()


async def test_hybrid_stale_preflight_is_caught_by_the_claim(monkeypatch):
    h = Harness(_three(**{"ord-3": {"assigned_run_id": "run-other", "assigned_asset_id": "truck-9"}}))
    _stale_projection(monkeypatch, h, **{"ord-3": {"assigned_run_id": None, "assigned_asset_id": None}})

    exc = await h.dispatch_conflict()

    assert exc.details["reason"] == "order_committed_elsewhere"
    h.assert_unlinked("ord-1", "ord-2")
    assert h.order("ord-3")["assigned_run_id"] == "run-other"
    h.assert_nothing_dispatched()


async def test_claim_store_error_propagates_and_releases_claims():
    h = Harness(_three())

    def boom():
        raise RuntimeError("store unavailable")

    h.on("atomic_update", ORDERS, "ord-3", boom)

    with pytest.raises(RuntimeError, match="store unavailable"):
        await h.dispatch()

    h.assert_unlinked("ord-1", "ord-2", "ord-3")
    h.assert_nothing_dispatched()


@pytest.mark.parametrize("raising", ["ord-1", "ord-2", "ord-3"])
async def test_claim_committed_then_raised_is_released(raising):
    # Review pass 2 finding 1: the claim row commits, then the call raises
    # (a lost COMMIT acknowledgement). The order is released by ownership.
    h = Harness(_three())
    h.store.fail_on("atomic_update", ORDERS, raising, nth=1, after_commit=True)

    with pytest.raises(RuntimeError, match="injected store fault"):
        await h.dispatch()

    assert raising in h.releases
    h.assert_unlinked(*THREE)
    h.assert_nothing_dispatched()


async def test_already_linked_order_is_not_released_on_refusal():
    h = Harness(
        [
            _order("ord-1", status="scheduled", assigned_run_id="run-1", assigned_asset_id="truck-1", assigned_claim_id="attempt-A"),
            _order("ord-2", status="confirmed"),
            _order("ord-3", status="confirmed"),
        ]
    )
    h.on("atomic_update", ORDERS, "ord-3", lambda: h.store.poke(ORDERS, "ord-3", assigned_run_id="run-other", assigned_asset_id="truck-9"))

    await h.dispatch_conflict()

    assert h.links("ord-1") == ("run-1", "truck-1", "attempt-A")
    assert "ord-1" not in h.releases
    h.assert_unlinked("ord-2")
    h.assert_nothing_dispatched()


async def test_pre_assigned_driver_order_is_released_with_driver_kept():
    # Pass-5 finding 1: the plan's driver was already on order 1.
    h = Harness(_three(**{"ord-1": {"assigned_driver_id": "driver-1"}}))
    h.on("atomic_update", ORDERS, "ord-3", lambda: h.store.poke(ORDERS, "ord-3", assigned_run_id="run-other", assigned_asset_id="truck-9"))

    await h.dispatch_conflict()

    h.assert_unlinked("ord-1", "ord-2")
    assert h.order("ord-1")["assigned_driver_id"] == "driver-1"
    h.assert_nothing_dispatched()


async def test_driver_only_in_es_documents_is_caught_after_the_claim(monkeypatch):
    # Pass-5 finding 2: the projection lacks D2; the claimed document has it.
    h = Harness(_three(**{"ord-3": {"assigned_driver_id": "D2"}}))
    _stale_projection(monkeypatch, h, **{"ord-3": {"assigned_driver_id": None}})

    exc = await h.dispatch_conflict()

    assert exc.details["reason"] == "order_driver_conflict"
    assert exc.details["order_id"] == "ord-3"
    h.assert_unlinked("ord-1", "ord-2", "ord-3")
    assert h.order("ord-3")["assigned_driver_id"] == "D2"
    h.assert_nothing_dispatched()


@pytest.mark.parametrize(
    ("interference", "reason", "text"),
    [
        ({"assigned_run_id": "run-other", "assigned_asset_id": "truck-9"}, "order_committed_elsewhere", "committed to another run"),
        ({"status": "scheduled"}, "order_changed_since_plan", "changed since it was loaded; reload and retry"),
        (None, "order_not_found", "The plan references orders that no longer exist"),
    ],
)
async def test_each_claim_refusal_reason_has_its_own_message(interference, reason, text):
    h = Harness(_three())
    if interference is None:
        h.on("atomic_update", ORDERS, "ord-3", lambda: h.store.remove(ORDERS, "ord-3"))
    else:
        h.on("atomic_update", ORDERS, "ord-3", lambda: h.store.poke(ORDERS, "ord-3", **interference))

    exc = await h.dispatch_conflict()

    assert exc.details == {"reason": reason, "order_id": "ord-3"}
    assert text in exc.message
    h.assert_unlinked("ord-1", "ord-2")
    h.assert_nothing_dispatched()


async def test_freeze_b_driver_write_between_claim_and_refusal_still_releases():
    # FREEZE test (b): assign-driver (driver D2 + new timestamp) lands on
    # order 1 after its claim; order 3's claim is then refused.
    h = Harness(_three())

    def interleave():
        h.store.poke(ORDERS, "ord-1", assigned_driver_id="D2", last_event_timestamp="2026-07-29T13:00:00Z")
        h.store.poke(ORDERS, "ord-3", assigned_run_id="run-other", assigned_asset_id="truck-9")

    h.on("atomic_update", ORDERS, "ord-3", interleave)

    await h.dispatch_conflict()

    h.assert_unlinked("ord-1", "ord-2")
    assert h.order("ord-1")["assigned_driver_id"] == "D2"
    assert h.order("ord-1")["last_event_timestamp"] == "2026-07-29T13:00:00Z"
    h.assert_nothing_dispatched()


# ---------------------------------------------------------------------------
# Failures after the claims
# ---------------------------------------------------------------------------


async def test_route_update_fault_releases_all_claims():
    h = Harness(_three())

    def boom():
        raise RuntimeError("route write failed")

    h.on("update_document", "mvp_routes", "route-1", boom)

    with pytest.raises(RuntimeError, match="route write failed"):
        await h.dispatch()

    h.assert_unlinked(*THREE)
    h.assert_nothing_dispatched()


async def test_ensure_execution_fault_releases_all_claims():
    h = Harness(_three())
    h.execution.create_execution.side_effect = RuntimeError("execution store down")

    with pytest.raises(RuntimeError, match="execution store down"):
        await h.dispatch()

    h.assert_unlinked(*THREE)
    assert all(h.order(oid)["status"] == "confirmed" for oid in THREE)
    assert h.store.events() == []
    h.push.assert_not_awaited()
    h.driver_ws.send_assignment.assert_not_awaited()
    assert h.store.writes("mvp_load_plans") == []


@pytest.mark.parametrize(
    "interference",
    [
        {"assigned_driver_id": "D2", "last_event_timestamp": "2099-01-01T00:00:00Z"},
        {"status": "cancelled", "last_event_timestamp": "2099-01-01T00:00:00Z"},
    ],
    ids=["driver_assigned", "cancelled"],
)
async def test_concurrent_write_before_order_2_transition_is_order_changed_concurrently(interference):
    h = Harness(_three())
    # ord-2's first atomic_update is its claim, the second its guarded transition.
    h.on("atomic_update", ORDERS, "ord-2", lambda: h.store.poke(ORDERS, "ord-2", **interference), nth=2)

    exc = await h.dispatch_conflict()

    assert exc.details == {"reason": "order_changed_concurrently", "order_id": "ord-2"}
    assert "changed while it was being dispatched" in exc.message
    # Documented residual: order 1 stays dispatched and linked.
    assert h.order("ord-1")["status"] == "dispatched"
    assert h.links("ord-1")[:2] == ("run-1", "truck-1")
    h.assert_unlinked("ord-2", "ord-3")
    for key, value in interference.items():
        assert h.order("ord-2")[key] == value
    assert h.order("ord-3")["status"] == "confirmed"
    assert h.push.await_count == 1
    h.driver_ws.send_assignment.assert_not_awaited()
    assert h.store.writes("mvp_load_plans") == []


async def test_concurrent_dispatches_sharing_an_order_are_serialized():
    # FREEZE rule 1: two plans, two trucks, one shared order; the lock
    # serializes them, so the order ends on exactly one truck.
    lock = PlanExecutionLock(use_postgres=False, timeout_seconds=2)
    plan_a = _plan(order_ids=["ord-1", "ord-2"], plan_id="plan-a", run_id="run-a", truck_id="truck-1")
    plan_b = _plan(order_ids=["ord-2", "ord-3"], plan_id="plan-b", run_id="run-b", truck_id="truck-2")
    h = Harness(
        _three(),
        plans=[plan_a, plan_b],
        routes=[
            _route(order_ids=["ord-1", "ord-2"], route_id="route-a", plan_id="plan-a", run_id="run-a", truck_id="truck-1"),
            _route(order_ids=["ord-2", "ord-3"], route_id="route-b", plan_id="plan-b", run_id="run-b", truck_id="truck-2"),
        ],
        plan_lock=lock,
    )

    results = await asyncio.gather(h.dispatch(plan_a), h.dispatch(plan_b), return_exceptions=True)

    wins = [r for r in results if not isinstance(r, Exception)]
    losses = [r for r in results if isinstance(r, AppException)]
    assert len(wins) == 1 and len(losses) == 1
    winner = wins[0]
    assert losses[0].status_code == 409
    loser_run = "run-b" if winner.run_id == "run-a" else "run-a"
    for oid in THREE:
        doc = h.order(oid)
        assert doc["assigned_run_id"] in (None, winner.run_id)
        assert doc["assigned_run_id"] != loser_run
        if doc["assigned_run_id"] is None:
            assert doc["status"] == "confirmed"
    assert h.order("ord-2")["assigned_run_id"] == winner.run_id
    assert h.order("ord-2")["status"] == "dispatched"
