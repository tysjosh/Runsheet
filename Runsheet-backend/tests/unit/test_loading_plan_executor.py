"""Tests for ``fuel.services.loading_plan_executor`` (design test plan T-U1..T-U8).

The executor runs over the real ``FuelOrderRepository`` and the real
``OrderService`` (default clock, spy ``ws_manager``, spy subscribers on every
``order.*`` name, spy driver counters) on the shared in-memory document store.
No Redis, Postgres or network is touched: flag reads go to fakes.

Concurrency follows plan decision P4 (executor and MVP dispatch serialize on
the per-tenant plan-execution lock) and the design freeze test (a).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, create_autospec

import pytest

from errors.exceptions import AppException
from fuel.order_repository import FuelOrderRepository
from fuel.services.loading_plan_executor import (
    LOADING_FLAG_KEY,
    MESSAGE_TEMPLATES,
    REASON_INTERNAL_ERROR,
    LoadingPlanExecutionResult,
    LoadingPlanExecutor,
    _template,
)
from fuel.services.order_service import OrderService
from fuel.services.plan_dispatch_service import FuelPlanDispatchService
from fuel.websocket.orders_ws import OrdersWSManager
from ops.services.feature_flags import FeatureFlagService
from persistence.plan_execution_lock import PlanExecutionLock
from tests.unit._loading_plan_fakes import (
    EVENTS,
    ORDERS,
    PLANS,
    DictRedis,
    FakeFeatureFlagService,
    InMemoryDocStore,
    RaisingRedis,
    order_fixture,
    plan_doc,
)

T = "tenant-1"
ORDER_EVENTS = [
    f"order.{s}"
    for s in (
        "placed", "confirmed", "scheduled", "dispatched", "in_transit",
        "delivered", "failed", "cancelled", "on_hold",
    )
]
CHANGED_KEYS = {
    "status", "assigned_asset_id", "assigned_run_id", "assigned_claim_id",
    "updated_at", "last_event_timestamp",
}


def _snapshot(order):
    return {
        "product_code": order["product_code"],
        "customer_tank_id": order["customer_tank_id"],
        "gallons_requested": order["gallons_requested"],
        "fill_to_full": order["fill_to_full"],
    }


class FakeDriverRepository:
    def __init__(self):
        self.drivers = [
            {"driver_id": "driver-1", "tenant_id": T, "assigned_truck_id": "truck-1", "status": "active"},
            {"driver_id": "driver-2", "tenant_id": T, "assigned_truck_id": "truck-2", "status": "active"},
        ]

    async def search(self, tenant_id, **filters):
        truck = filters.get("assigned_truck_id")
        found = [d for d in self.drivers if truck is None or d.get("assigned_truck_id") == truck]
        return {"drivers": found, "total": len(found)}


class Harness:
    """Executor + real repository/OrderService over a fresh in-memory store."""

    def __init__(self, orders, *, plans=None, plan_orders=None, ff=None, lock=None, seed_plan=True):
        self.store = InMemoryDocStore()
        for order in orders:
            self.store.seed(ORDERS, order["order_id"], order)
        self.plans = plans or [plan_doc("plan-1", orders=plan_orders if plan_orders is not None else orders)]
        if seed_plan:
            for plan in self.plans:
                self.store.seed(PLANS, plan["plan_id"], plan)
        self.snapshots = {o["order_id"]: _snapshot(o) for o in (plan_orders if plan_orders is not None else orders)}
        self.repo = FuelOrderRepository(self.store)
        self.ws = create_autospec(OrdersWSManager, instance=True)
        self.counters = AsyncMock()
        self.order_service = OrderService(
            order_repo=self.repo, ws_manager=self.ws, driver_counter_service=self.counters
        )  # default clock
        self.notified = []
        for name in ORDER_EVENTS:
            self.order_service.subscribe(name, self._spy(name))
        self.ff = ff if ff is not None else FakeFeatureFlagService("active_gated")
        self.lock = lock or PlanExecutionLock(use_postgres=False, timeout_seconds=2)
        self.executor = LoadingPlanExecutor(
            es_service=self.store,
            order_repository=self.repo,
            order_service=self.order_service,
            feature_flag_service=self.ff,
            plan_lock=self.lock,
        )

    def _spy(self, name):
        async def handler(order):
            self.notified.append((name, order["order_id"]))

        return handler

    async def run(self, plan=None, **overrides):
        plan = plan or self.plans[0]
        kwargs = dict(
            tenant_id=T,
            plan_id=plan["plan_id"],
            expected_order_ids=sorted(a["order_id"] for a in plan["assignments"] if a.get("order_id")),
            expected_truck_id=plan.get("truck_id"),
            order_snapshots=self.snapshots,
            actor_user_id="user-1",
            action_id="act-1",
            approved_at="2026-07-29T12:30:00+00:00",
            mode="active_gated",
        )
        kwargs.update(overrides)
        return await self.executor.execute(**kwargs)

    def order(self, order_id):
        return self.store.doc(ORDERS, order_id)

    def plan(self, plan_id="plan-1"):
        return self.store.doc(PLANS, plan_id)

    def links(self, order_id):
        doc = self.order(order_id)
        return (doc.get("assigned_run_id"), doc.get("assigned_asset_id"))

    def events(self, order_id, event_type=None):
        return [
            e for e in sorted(self.store.events(order_id), key=lambda e: e["event_timestamp"])
            if event_type is None or e["event_type"] == event_type
        ]

    def order_writes(self, since=0):
        return [
            (op, idx, d) for op, idx, d, applied in self.store.ops[since:]
            if applied and idx in (ORDERS, EVENTS)
        ]

    def mark(self):
        return len(self.store.ops)

    def on(self, op, index, doc_id, action, *, nth=1):
        seen = []

        def hook(o, i, d):
            if (o, i, d) == (op, index, doc_id):
                seen.append(1)
                if len(seen) == nth:
                    return action()

        self.store.hooks.append(hook)

    async def cancel(self, order_id):
        """A dispatcher PATCH-style cancel from the current stored order."""
        await self.order_service.apply_status_transition(
            self.order(order_id), "cancelled", reason="customer_request", actor_user_id="dispatcher-1"
        )


def _three(*statuses, **overrides):
    statuses = statuses or ("confirmed", "confirmed", "confirmed")
    return [
        order_fixture(f"ord-{i + 1}", status=s, tenant_id=T, **overrides.get(f"ord-{i + 1}", {}))
        for i, s in enumerate(statuses)
    ]


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ---------------------------------------------------------------------------
# T-U1 success
# ---------------------------------------------------------------------------


async def test_t_u1_placed_confirmed_and_scheduled_orders_are_applied():
    h = Harness(_three("placed", "confirmed", "scheduled"))
    before = {oid: h.order(oid) for oid in ("ord-1", "ord-2", "ord-3")}

    result = await h.run()

    assert result.outcome == "applied" and result.success and not result.replay
    assert result.order_ids == ["ord-1", "ord-2", "ord-3"]
    assert result.applied_order_ids == ["ord-1", "ord-2", "ord-3"]
    assert result.pending_order_ids == [] and result.settled_order_ids == [] and result.failures == []
    assert result.reason is None and result.retryable is False and result.writes_made is True
    assert result.plan_id == "plan-1" and result.run_id == "run-1" and result.truck_id == "truck-1"
    assert result.attempt_id
    assert set(result.as_dict()) == {
        "outcome", "success", "replay", "plan_id", "run_id", "truck_id", "order_ids",
        "applied_order_ids", "settled_order_ids", "pending_order_ids", "would_apply",
        "failures", "reason", "retryable", "writes_made", "message", "attempt_id",
    }

    for oid in ("ord-1", "ord-2", "ord-3"):
        after = h.order(oid)
        assert after["status"] == "scheduled"
        assert h.links(oid) == ("run-1", "truck-1")
        assert after["assigned_claim_id"] == result.attempt_id
        assert after["assigned_driver_id"] is None
        diff = {k for k in set(before[oid]) | set(after) if before[oid].get(k) != after.get(k)}
        assert diff <= CHANGED_KEYS, (oid, diff)
        assigned = h.events(oid, "order_assigned")
        assert len(assigned) == 1
        payload = assigned[0]["event_payload"]
        assert payload["plan_id"] == "plan-1" and payload["run_id"] == "run-1"
        assert payload["asset_id"] == "truck-1" and payload["source"] == "loading_plan_executor"
        assert payload["actor_user_id"] == "user-1" and payload["attempt_id"] == result.attempt_id
        assert payload["allocations"] == [
            {"compartment_id": f"c-{oid[-1]}", "product_code": "DIESEL_2", "quantity_liters": 1500.0}
        ]

    transitions = {
        oid: [e["event_type"] for e in h.events(oid) if e["event_type"] != "order_assigned"]
        for oid in ("ord-1", "ord-2", "ord-3")
    }
    assert transitions == {
        "ord-1": ["order_confirmed", "order_scheduled"],
        "ord-2": ["order_scheduled"],
        "ord-3": [],
    }
    for oid in ("ord-1", "ord-2"):
        for event in h.events(oid):
            if event["event_type"] != "order_assigned":
                assert event["event_payload"]["reason"] == "loading_plan_approved"
                assert event["event_payload"]["actor_user_id"] == "user-1"
                assert event["event_payload"]["plan_id"] == "plan-1"

    plan = h.plan()
    assert plan["execution_status"] == "succeeded"
    assert plan["applied_by"] == "user-1" and plan["applied_at"]
    assert plan["execution_approved_at"] == "2026-07-29T12:30:00+00:00"
    assert plan["status"] == "scheduled"
    assert plan["execution_result"]["outcome"] == "applied"

    assert h.store.write_count(index="jobs_current") == 0
    h.ws.broadcast.assert_not_awaited()
    broadcasts = [c.args[0] for c in h.ws.broadcast_order_status_changed.await_args_list]
    per_order = {}
    for b in broadcasts:
        per_order.setdefault(b["order_id"], []).append(b["new_status"])
    assert per_order == {"ord-1": ["confirmed", "scheduled"], "ord-2": ["scheduled"]}


async def test_t_u1_blank_run_id_becomes_plan_id_on_plan_and_links():
    orders = _three()
    h = Harness(orders, plans=[plan_doc("plan-1", orders=orders, run_id="")])

    result = await h.run()

    assert result.outcome == "applied" and result.run_id == "plan-1"
    assert h.plan()["run_id"] == "plan-1"
    for oid in ("ord-1", "ord-2", "ord-3"):
        assert h.links(oid) == ("plan-1", "truck-1")


async def test_t_u1_dispatched_plan_keeps_its_status():
    orders = _three()
    h = Harness(orders, plans=[plan_doc("plan-1", orders=orders, status="dispatched")])

    result = await h.run()

    assert result.outcome == "applied"
    assert h.plan()["status"] == "dispatched"


async def test_empty_string_links_are_unlinked():
    h = Harness(_three(**{"ord-1": {"assigned_run_id": "", "assigned_asset_id": ""}}))

    result = await h.run()

    assert result.outcome == "applied"
    assert h.links("ord-1") == ("run-1", "truck-1")


# ---------------------------------------------------------------------------
# T-U2 idempotent
# ---------------------------------------------------------------------------


async def test_t_u2_second_run_is_a_replay_with_zero_writes():
    h = Harness(_three("placed", "confirmed", "scheduled"))
    first = await h.run()
    mark, broadcasts, events = h.mark(), h.ws.broadcast_order_status_changed.await_count, len(h.store.events())

    second = await h.run()

    assert second.outcome == "replayed" and second.replay and second.success
    assert second.applied_order_ids == first.applied_order_ids
    assert second.attempt_id == first.attempt_id
    assert [op for op in h.store.ops[mark:] if op[3]] == []
    assert h.ws.broadcast_order_status_changed.await_count == broadcasts
    assert len(h.store.events()) == events


async def test_t_u2_mvp_dispatched_orders_are_applied_with_no_order_writes():
    orders = _three(
        "dispatched", "dispatched", "dispatched",
        **{
            oid: {"assigned_run_id": "run-1", "assigned_asset_id": "truck-1", "assigned_driver_id": "driver-1"}
            for oid in ("ord-1", "ord-2", "ord-3")
        },
    )
    h = Harness(orders)

    result = await h.run()

    assert result.outcome == "applied"
    assert result.applied_order_ids == ["ord-1", "ord-2", "ord-3"]
    assert h.order_writes() == []
    assert h.ws.broadcast_order_status_changed.await_count == 0


# ---------------------------------------------------------------------------
# T-U3 stale-safe
# ---------------------------------------------------------------------------


def _case_missing_plan():
    return Harness(_three(), seed_plan=False), {}, "plan_not_found", []


def _case_cross_tenant_plan():
    orders = _three()
    return Harness(orders, plans=[plan_doc("plan-1", orders=orders, tenant_id="tenant-2")]), {}, "plan_not_found", []


def _case_rejected():
    orders = _three()
    return Harness(orders, plans=[plan_doc("plan-1", orders=orders, status="rejected")]), {}, "plan_rejected", []


def _case_identity():
    orders = _three()
    return Harness(orders, plans=[plan_doc("plan-1", orders=orders, truck_id="")]), {"expected_truck_id": None}, "plan_identity_incomplete", []


def _case_assignment_without_order():
    orders = _three()
    plan = plan_doc("plan-1", orders=orders)
    plan["assignments"].append({"compartment_id": "c-9", "station_id": "customer-1", "quantity_liters": 10.0})
    return Harness(orders, plans=[plan]), {}, "assignment_without_order", []


def _case_plan_changed_orders():
    return Harness(_three()), {"expected_order_ids": ["ord-1", "ord-2"]}, "plan_changed", []


def _case_plan_changed_truck():
    return Harness(_three()), {"expected_truck_id": "truck-9"}, "plan_changed", []


def _case_order_cross_tenant():
    planned = _three()
    stored = planned[:2] + [order_fixture("ord-3", tenant_id="tenant-2")]
    return Harness(stored, plan_orders=planned), {}, "order_not_found", ["ord-3"]


def _case_on_hold():
    planned = _three()
    stored = planned[:2] + [order_fixture("ord-3", status="on_hold", hold_reason="credit")]
    return Harness(stored, plan_orders=planned), {}, "order_not_loadable", ["ord-3"]


def _case_cancelled_unlinked_no_prior_writes():
    planned = _three()
    stored = planned[:2] + [order_fixture("ord-3", status="cancelled")]
    return Harness(stored, plan_orders=planned), {}, "order_not_loadable", ["ord-3"]


def _case_committed_elsewhere():
    planned = _three()
    stored = planned[:2] + [order_fixture("ord-3", assigned_run_id="run-x", assigned_asset_id="truck-9")]
    return Harness(stored, plan_orders=planned), {}, "order_committed_elsewhere", ["ord-3"]


def _changed(**fields):
    def build():
        planned = _three()
        stored = planned[:2] + [order_fixture("ord-3", **fields)]
        return Harness(stored, plan_orders=planned), {}, "order_changed_since_plan", ["ord-3"]

    return build


def _case_missing_window():
    orders = _three(
        **{"ord-3": {"call_type": "will_call", "delivery_window_start": None, "delivery_window_end": None}}
    )
    return Harness(orders), {}, "missing_delivery_window", ["ord-3"]


@pytest.mark.parametrize(
    "case",
    [
        _case_missing_plan,
        _case_cross_tenant_plan,
        _case_rejected,
        _case_identity,
        _case_assignment_without_order,
        _case_plan_changed_orders,
        _case_plan_changed_truck,
        _case_order_cross_tenant,
        _case_on_hold,
        _case_cancelled_unlinked_no_prior_writes,
        _case_committed_elsewhere,
        _changed(product_code="GASOLINE_REG"),
        _changed(customer_id="customer-2"),
        _changed(customer_tank_id="tank-other"),
        _changed(gallons_requested=750.0),
        _changed(fill_to_full=True),
        _case_missing_window,
    ],
    ids=[
        "missing_plan", "cross_tenant_plan", "rejected", "identity", "assignment_without_order",
        "plan_changed_orders", "plan_changed_truck", "order_cross_tenant", "on_hold",
        "cancelled_unlinked", "committed_elsewhere", "product", "customer", "tank", "gallons",
        "fill_to_full", "missing_window",
    ],
)
async def test_t_u3_stale_plan_is_refused_with_zero_order_writes(case):
    h, overrides, reason, failing = case()
    # A store fault that must never surface in a message.
    h.store.fail_on("index_document", "jobs_current", exc=RuntimeError("SECRET-db-text"))

    result = await h.run(**overrides)

    assert result.outcome == "failed"
    assert result.reason == reason
    assert result.writes_made is False and result.retryable is False and result.success is False
    assert [f["order_id"] for f in result.failures] == failing
    assert all(f["reason"] == reason for f in result.failures)
    assert result.message == _template(reason, "plan-1")
    assert "SECRET" not in result.message
    assert h.order_writes() == []
    assert h.notified == [] and h.ws.broadcast_order_status_changed.await_count == 0
    if reason != "plan_not_found":
        assert h.plan()["execution_status"] == "failed"


async def test_t_u3_snapshot_fields_are_named():
    planned = _three()
    stored = planned[:2] + [order_fixture("ord-3", gallons_requested=750.0)]
    h = Harness(stored, plan_orders=planned)

    result = await h.run()

    assert result.failures == [{"order_id": "ord-3", "reason": "order_changed_since_plan", "field": "gallons_requested"}]


async def test_t_u3_every_failing_order_is_listed():
    planned = _three()
    stored = [
        order_fixture("ord-1", status="on_hold", hold_reason="credit"),
        planned[1],
        order_fixture("ord-3", assigned_run_id="run-x", assigned_asset_id="truck-9"),
    ]
    h = Harness(stored, plan_orders=planned)

    result = await h.run()

    assert result.outcome == "failed"
    assert result.failures == [
        {"order_id": "ord-1", "reason": "order_not_loadable", "status": "on_hold"},
        {"order_id": "ord-3", "reason": "order_committed_elsewhere"},
    ]
    assert h.order_writes() == []


async def test_alias_product_code_on_plan_matches_canonical_order():
    orders = _three()
    plan = plan_doc("plan-1", orders=orders)
    for a in plan["assignments"]:
        a["product_code"] = "ago"  # alias of DIESEL_2
    h = Harness(orders, plans=[plan])

    assert (await h.run()).outcome == "applied"


# ---------------------------------------------------------------------------
# T-U4 resumable
# ---------------------------------------------------------------------------


async def test_t_u4_fault_on_second_guarded_upsert_then_retry_completes():
    h = Harness(_three())
    # ord-2: 1st atomic_update is the claim, 2nd the guarded transition.
    h.store.fail_on("atomic_update", ORDERS, "ord-2", nth=2, exc=RuntimeError("SECRET boom"))

    first = await h.run()

    assert first.outcome == "incomplete" and first.reason == "write_failed"
    assert first.writes_made is True and first.retryable is True
    assert first.applied_order_ids == ["ord-1"]
    assert first.pending_order_ids == ["ord-2", "ord-3"]
    assert first.failures == [{"order_id": "ord-2", "reason": "write_failed"}]
    assert "SECRET" not in first.message
    assert h.plan()["execution_status"] == "incomplete"
    assert h.plan()["status"] == "proposed"

    second = await h.run()

    assert second.outcome == "applied"
    for oid in ("ord-1", "ord-2", "ord-3"):
        assert h.order(oid)["status"] == "scheduled"
        assert len(h.events(oid, "order_assigned")) == 1
        assert len(h.events(oid, "order_scheduled")) == 1
    assert h.plan()["status"] == "scheduled"


async def test_t_u4_fault_between_claim_and_event_resumes_already_linked():
    h = Harness(_three())
    h.store.fail_on("index_document", EVENTS, nth=1)

    first = await h.run()
    assert first.outcome == "incomplete" and first.writes_made is True
    assert h.links("ord-1") == ("run-1", "truck-1")
    assert h.events("ord-1") == []

    claims = []
    original = h.repo.claim_assignment

    async def spy(tenant_id, order_id, **kw):
        claim = await original(tenant_id, order_id, **kw)
        claims.append((order_id, claim.outcome))
        return claim

    h.repo.claim_assignment = spy
    second = await h.run()

    assert second.outcome == "applied"
    assert ("ord-1", "already_linked") in claims
    assert len(h.events("ord-1", "order_assigned")) == 1
    assert len(h.events("ord-1", "order_scheduled")) == 1


@pytest.mark.parametrize("cancelled", ["ord-3", "ord-2"], ids=["never_claimed", "linked_by_claim"])
async def test_t_u4_cancelled_blocker_is_settled_on_retry(cancelled):
    h = Harness(_three())
    h.store.fail_on("atomic_update", ORDERS, "ord-2", nth=2)
    assert (await h.run()).outcome == "incomplete"
    expected_links = h.links(cancelled)
    await h.cancel(cancelled)
    mark = h.mark()

    result = await h.run()

    assert result.outcome == "applied"
    assert result.settled_order_ids == [cancelled]
    assert cancelled not in result.applied_order_ids
    assert h.order(cancelled)["status"] == "cancelled"
    assert h.links(cancelled) == expected_links
    assert [w for w in h.order_writes(mark) if w[2] == cancelled] == []


async def test_t_u4_edited_order_after_partial_apply_is_incomplete_and_named():
    h = Harness(_three())
    h.store.fail_on("atomic_update", ORDERS, "ord-2", nth=2)
    assert (await h.run()).outcome == "incomplete"
    h.store.poke(ORDERS, "ord-3", gallons_requested=999.0)
    mark = h.mark()

    result = await h.run()

    assert result.outcome == "incomplete"
    assert result.writes_made is True and result.retryable is True
    assert result.reason == "order_changed_since_plan"
    assert "Order ord-3" in result.message and "(1 of 3 orders)" in result.message
    assert h.order_writes(mark) == []


async def test_t_u4_crash_before_first_write_then_edit_then_cancel():
    orders = _three()
    plan = plan_doc(
        "plan-1", orders=orders, execution_status="in_progress",
        execution_attempt_id="dead", execution_claimed_at=_ago(200),
    )
    h = Harness(orders, plans=[plan])
    h.store.poke(ORDERS, "ord-2", gallons_requested=999.0)

    first = await h.run()

    assert first.outcome == "incomplete"
    assert first.writes_made is True and first.retryable is True
    assert h.order_writes() == []

    await h.cancel("ord-2")
    second = await h.run()

    assert second.outcome == "applied"
    assert second.settled_order_ids == ["ord-2"]
    assert second.applied_order_ids == ["ord-1", "ord-3"]
    assert h.links("ord-2") == (None, None)


async def test_nit2_crash_before_first_write_then_all_cancelled_is_applied_and_settled():
    orders = _three()
    plan = plan_doc(
        "plan-1", orders=orders, execution_status="in_progress",
        execution_attempt_id="dead", execution_claimed_at=_ago(200),
    )
    h = Harness(orders, plans=[plan])
    for oid in ("ord-1", "ord-2", "ord-3"):
        await h.cancel(oid)

    result = await h.run()

    assert result.outcome == "applied" and result.success
    assert result.settled_order_ids == ["ord-1", "ord-2", "ord-3"]
    assert result.applied_order_ids == []
    assert h.plan()["status"] == "proposed"
    assert h.plan()["execution_status"] == "succeeded"


async def test_nit3_unlinked_order_walked_to_delivered_is_settled_on_retry():
    h = Harness(_three())
    h.store.fail_on("atomic_update", ORDERS, "ord-2", nth=2)
    assert (await h.run()).outcome == "incomplete"
    order = h.order("ord-3")
    for status in ("scheduled", "dispatched", "in_transit", "delivered"):
        order = await h.order_service.apply_status_transition(order, status, actor_user_id="dispatcher-1")
    assert h.order("ord-3")["status"] == "delivered"

    result = await h.run()

    assert result.outcome == "applied"
    assert result.settled_order_ids == ["ord-3"]
    assert result.applied_order_ids == ["ord-1", "ord-2"]


async def test_first_claim_refused_with_no_prior_writes_is_failed():
    h = Harness(_three())
    # Between preflight and claim, ord-1 is moved elsewhere by another writer.
    h.on("atomic_update", ORDERS, "ord-1", lambda: h.store.poke(ORDERS, "ord-1", status="on_hold", hold_reason="x"))

    result = await h.run()

    assert result.outcome == "failed" and result.reason == "order_changed_since_plan"
    assert result.writes_made is False and result.retryable is False
    assert h.order_writes() == []


RESYNC_TS = "2026-07-29T12:10:00+00:00"


def _resync(h, order_id):
    """An ERP/CSV re-sync: same status, new quantity, newer timestamp."""
    return lambda: h.store.poke(
        ORDERS, order_id, gallons_requested=999.0, last_event_timestamp=RESYNC_TS
    )


async def test_same_status_edit_before_first_claim_is_a_clean_failure():
    # Review pass 1, finding 1: preflight read ord-1 as planned; a re-sync
    # lands before its claim. The claim CAS carries the preflight timestamp.
    h = Harness(_three())
    h.on("atomic_update", ORDERS, "ord-1", _resync(h, "ord-1"))

    result = await h.run()

    assert result.outcome == "failed" and result.reason == "order_changed_since_plan"
    assert result.writes_made is False and result.retryable is False
    assert result.failures == [{"order_id": "ord-1", "reason": "order_changed_since_plan"}]
    assert result.applied_order_ids == []
    assert h.order_writes() == []
    for oid in ("ord-1", "ord-2", "ord-3"):
        assert h.links(oid) == (None, None) and h.order(oid)["status"] == "confirmed"
    assert h.order("ord-1")["gallons_requested"] == 999.0
    assert h.notified == [] and h.ws.broadcast_order_status_changed.await_count == 0
    assert h.plan()["execution_status"] == "failed"


async def test_same_status_edit_before_later_claim_is_incomplete_and_retry_names_it():
    h = Harness(_three())
    h.on("atomic_update", ORDERS, "ord-2", _resync(h, "ord-2"))

    first = await h.run()

    assert first.outcome == "incomplete" and first.reason == "order_changed_since_plan"
    assert first.writes_made is True and first.retryable is True
    assert first.applied_order_ids == ["ord-1"]
    assert first.pending_order_ids == ["ord-2", "ord-3"]
    assert first.failures == [{"order_id": "ord-2", "reason": "order_changed_since_plan"}]
    # The edited order was never linked or moved; the old sizing is not applied.
    assert h.links("ord-2") == (None, None) and h.order("ord-2")["status"] == "confirmed"
    assert h.events("ord-2") == []
    assert h.links("ord-3") == (None, None)
    assert h.plan()["execution_status"] == "incomplete"

    mark = h.mark()
    retry = await h.run()

    assert retry.outcome == "incomplete" and retry.reason == "order_changed_since_plan"
    assert "Order ord-2" in retry.message and "(1 of 3 orders)" in retry.message
    assert retry.failures == [
        {"order_id": "ord-2", "reason": "order_changed_since_plan", "field": "gallons_requested"}
    ]
    assert h.order_writes(mark) == []


async def test_first_claim_raising_with_no_prior_writes_is_retryable_failed():
    h = Harness(_three())
    h.store.fail_on("atomic_update", ORDERS, "ord-1", nth=1)

    result = await h.run()

    assert result.outcome == "failed" and result.reason == "write_failed"
    assert result.writes_made is False and result.retryable is True


async def test_first_claim_committed_then_raised_is_released_and_clean_failed():
    # Review pass 2 finding 1: the claim row commits, then the call raises
    # (a lost COMMIT acknowledgement). The executor releases by ownership.
    h = Harness(_three())
    h.store.fail_on("atomic_update", ORDERS, "ord-1", nth=1, after_commit=True)

    result = await h.run()

    assert result.outcome == "failed" and result.reason == "write_failed"
    assert result.writes_made is False and result.retryable is True
    for oid in ("ord-1", "ord-2", "ord-3"):
        assert h.links(oid) == (None, None), oid
        assert h.order(oid)["status"] == "confirmed"
        assert h.events(oid) == []
    assert h.order("ord-1").get("assigned_claim_id") is None
    assert h.store.calls("atomic_update", ORDERS) == ["ord-1", "ord-1"]  # claim, release
    assert h.plan()["execution_status"] == "failed"


async def test_first_claim_committed_then_release_also_raising_holds_incomplete():
    # The link state is unknown, so nothing may treat the plan as unwritten.
    h = Harness(_three())
    h.store.fail_on("atomic_update", ORDERS, "ord-1", nth=1, after_commit=True)
    h.store.fail_on("atomic_update", ORDERS, "ord-1", nth=2)  # the release

    result = await h.run()

    assert result.outcome == "incomplete" and result.reason == "write_failed"
    assert result.writes_made is True and result.retryable is True
    assert h.links("ord-1") == ("run-1", "truck-1")
    assert h.order("ord-1")["assigned_claim_id"] == result.attempt_id
    assert h.plan()["execution_status"] == "incomplete"

    # A retry classifies the order (already linked here) and completes.
    retry = await h.run()
    assert retry.outcome == "applied"
    for oid in ("ord-1", "ord-2", "ord-3"):
        assert h.order(oid)["status"] == "scheduled"
        assert h.links(oid) == ("run-1", "truck-1")
        assert len(h.events(oid, "order_assigned")) == 1


async def test_later_claim_committed_then_raised_is_released_and_incomplete():
    h = Harness(_three())
    h.store.fail_on("atomic_update", ORDERS, "ord-2", nth=1, after_commit=True)

    result = await h.run()

    assert result.outcome == "incomplete" and result.writes_made is True
    assert result.applied_order_ids == ["ord-1"]
    assert h.links("ord-2") == (None, None)
    assert h.order("ord-2")["status"] == "confirmed" and h.events("ord-2") == []
    assert h.links("ord-3") == (None, None)

    retry = await h.run()
    assert retry.outcome == "applied"
    assert all(h.order(o)["status"] == "scheduled" for o in ("ord-1", "ord-2", "ord-3"))


# ---------------------------------------------------------------------------
# T-U5 concurrency (P4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schedule", [[0], [1], [0, 1, 2], [2, 0, 1, 0]], ids=str)
async def test_t_u5_concurrent_executes_serialize_one_applies_other_replays(schedule):
    h = Harness(_three("placed", "confirmed", "scheduled"))
    h.store.set_yield_schedule(schedule, repeat=True)

    results = await asyncio.gather(h.run(action_id="a"), h.run(action_id="b"))

    outcomes = sorted(r.outcome for r in results)
    assert outcomes == ["applied", "replayed"]
    plan_claims = [op for op in h.store.ops if op[:2] == ("atomic_update", PLANS) and op[3]]
    assert len(plan_claims) == 2  # one claim + one finalize
    for oid in ("ord-1", "ord-2", "ord-3"):
        assert len(h.events(oid, "order_assigned")) == 1
    assert len(h.events("ord-1", "order_scheduled")) == 1


async def test_t_u5_stale_lease_is_reclaimed():
    orders = _three()
    plan = plan_doc("plan-1", orders=orders, execution_status="in_progress",
                    execution_attempt_id="old", execution_claimed_at=_ago(121))
    h = Harness(orders, plans=[plan])

    result = await h.run()

    assert result.outcome == "applied"
    assert h.plan()["execution_attempt_id"] == result.attempt_id != "old"


async def test_t_u5_fresh_lease_is_in_progress_with_zero_writes():
    orders = _three()
    plan = plan_doc("plan-1", orders=orders, execution_status="in_progress",
                    execution_attempt_id="live", execution_claimed_at=_ago(60))
    h = Harness(orders, plans=[plan])

    result = await h.run()

    assert result.outcome == "in_progress" and result.reason == "execution_in_progress"
    assert result.retryable is True and result.writes_made is False
    assert h.store.writes() == []


async def test_t_u5_garbage_claimed_at_is_reclaimed_with_warning(caplog):
    orders = _three()
    plan = plan_doc("plan-1", orders=orders, execution_status="in_progress",
                    execution_attempt_id="old", execution_claimed_at="not-a-date")
    h = Harness(orders, plans=[plan])

    with caplog.at_level(logging.WARNING, logger="fuel.services.loading_plan_executor"):
        result = await h.run()

    assert result.outcome == "applied"
    assert "no parseable execution_claimed_at" in caplog.text


async def test_t_u5_lock_timeout_is_in_progress_with_zero_writes():
    lock = PlanExecutionLock(use_postgres=False, timeout_seconds=0.05)
    h = Harness(_three(), lock=lock)
    held, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with lock.hold(T):
            held.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await held.wait()
    try:
        result = await h.run()
    finally:
        release.set()
        await task

    assert result.outcome == "in_progress" and result.reason == "execution_in_progress"
    assert result.writes_made is False and result.retryable is True
    assert h.store.writes() == []


# ---------------------------------------------------------------------------
# T-U5b lost update, and FREEZE test (a)
# ---------------------------------------------------------------------------


def _route(*, order_ids, route_id, plan_id, run_id, truck_id):
    return {
        "route_id": route_id, "plan_id": plan_id, "run_id": run_id, "truck_id": truck_id,
        "tenant_id": T,
        "stops": [{"station_id": "customer-1", "order_ids": list(order_ids), "sequence": 0}],
    }


def _dispatch_service(h, *routes):
    for route in routes:
        h.store.seed("mvp_routes", route["route_id"], route)
    execution = AsyncMock()
    execution.create_execution = AsyncMock(
        side_effect=lambda **kw: {"execution_id": f"exec-{kw['route_id']}"}
    )
    driver_ws = AsyncMock()
    service = FuelPlanDispatchService(
        es_service=h.store,
        order_repository=h.repo,
        order_service=h.order_service,
        driver_repository=FakeDriverRepository(),
        execution_service=execution,
        driver_ws_manager=driver_ws,
        plan_lock=h.lock,
    )
    return service, driver_ws


def _last_index(events, event_type):
    idx = [i for i, e in enumerate(events) if e["event_type"] == event_type]
    return idx[-1] if idx else -1


SCHEDULES = [[0], [1], [0, 1], [1, 0, 2], [2, 1, 0, 0, 1]]


@pytest.mark.parametrize("executor_first", [True, False], ids=["executor_first", "dispatch_first"])
@pytest.mark.parametrize("schedule", SCHEDULES, ids=str)
async def test_t_u5b_executor_and_dispatch_of_the_same_plan(executor_first, schedule):
    h = Harness(_three())
    plan = h.plans[0]
    service, _ws = _dispatch_service(
        h, _route(order_ids=["ord-1", "ord-2", "ord-3"], route_id="route-1",
                  plan_id="plan-1", run_id="run-1", truck_id="truck-1"),
    )
    h.store.set_yield_schedule(schedule, repeat=True)

    async def dispatch():
        return await service.dispatch(tenant_id=T, plan_doc=dict(plan), actor_user_id="dispatcher-1")

    calls = [h.run(), dispatch()] if executor_first else [dispatch(), h.run()]
    results = await asyncio.gather(*calls, return_exceptions=True)
    exec_result = results[0] if executor_first else results[1]
    dispatch_result = results[1] if executor_first else results[0]

    assert isinstance(exec_result, LoadingPlanExecutionResult)
    assert exec_result.outcome == "applied"
    if isinstance(dispatch_result, Exception):
        assert isinstance(dispatch_result, AppException) and dispatch_result.status_code == 409
        h.store.set_yield_schedule(None)
        await dispatch()  # the dispatcher's retry
    for oid in ("ord-1", "ord-2", "ord-3"):
        doc = h.order(oid)
        events = h.events(oid)
        assert doc["status"] == "dispatched"
        assert h.links(oid) == ("run-1", "truck-1")
        assert doc["assigned_driver_id"] == "driver-1"
        assert _last_index(events, "order_dispatched") > _last_index(events, "order_scheduled")


def _assert_no_cancel_hidden_under_schedule(h):
    for oid in ("ord-1", "ord-2", "ord-3"):
        events = h.events(oid)
        if h.order(oid)["status"] == "scheduled":
            assert _last_index(events, "order_cancelled") < _last_index(events, "order_scheduled"), oid


@pytest.mark.parametrize("schedule", SCHEDULES, ids=str)
async def test_t_u5b_executor_gathered_with_stale_copy_cancel(schedule):
    h = Harness(_three())
    stale = h.order("ord-2")
    h.store.set_yield_schedule(schedule, repeat=True)

    results = await asyncio.gather(
        h.run(),
        h.order_service.apply_status_transition(stale, "cancelled", actor_user_id="dispatcher-1"),
        return_exceptions=True,
    )

    assert isinstance(results[0], LoadingPlanExecutionResult)
    assert not isinstance(results[1], Exception)
    _assert_no_cancel_hidden_under_schedule(h)
    assert h.order("ord-2")["status"] == "cancelled"


# Where the dispatcher's stale-copy cancel of ord-2 lands inside the executor:
# (store op, index, doc id, nth) -> (first outcome, first reason, retry outcome).
CANCEL_POINTS = {
    "before_preflight_read": (("get_document", ORDERS, "ord-2", 1), "failed", "order_not_loadable", "failed"),
    "before_claim": (("atomic_update", ORDERS, "ord-2", 1), "incomplete", "order_changed_since_plan", "applied"),
    "before_transition": (("atomic_update", ORDERS, "ord-2", 2), "incomplete", "order_changed_concurrently", "applied"),
    "after_applied": (("atomic_update", ORDERS, "ord-3", 1), "applied", None, "replayed"),
}


@pytest.mark.parametrize("point", sorted(CANCEL_POINTS))
async def test_t_u5b_stale_copy_cancel_at_each_point(point):
    (op, index, doc_id, nth), outcome, reason, retry_outcome = CANCEL_POINTS[point]
    h = Harness(_three())
    stale = h.order("ord-2")
    fired = []

    async def cancel():
        fired.append(1)
        await h.order_service.apply_status_transition(stale, "cancelled", actor_user_id="dispatcher-1")

    h.on(op, index, doc_id, cancel, nth=nth)

    result = await h.run()

    assert fired == [1]
    assert (result.outcome, result.reason) == (outcome, reason)
    _assert_no_cancel_hidden_under_schedule(h)
    retry = await h.run()
    assert retry.outcome == retry_outcome
    assert h.order("ord-2")["status"] == "cancelled"
    if retry_outcome in ("applied", "replayed"):
        for oid in ("ord-1", "ord-3"):
            assert h.order(oid)["status"] == "scheduled"
        if retry_outcome == "applied":
            assert retry.settled_order_ids == ["ord-2"]
    else:
        assert retry.writes_made is False
        assert [w for w in h.order_writes() if w[2] != "ord-2" and w[1] == ORDERS] == []


async def test_t_u5b_driver_assignment_between_claim_and_transition_is_refused():
    h = Harness(_three())
    h.on(
        "atomic_update", ORDERS, "ord-1",
        lambda: h.store.poke(ORDERS, "ord-1", assigned_driver_id="D2",
                             last_event_timestamp="2099-01-01T00:00:00+00:00"),
        nth=2,
    )

    result = await h.run()

    assert result.outcome == "incomplete" and result.reason == "order_changed_concurrently"
    assert h.order("ord-1")["assigned_driver_id"] == "D2"
    assert h.order("ord-1")["status"] == "confirmed"


@pytest.mark.parametrize("executor_first", [True, False], ids=["executor_first", "dispatch_first"])
@pytest.mark.parametrize("schedule", SCHEDULES, ids=str)
async def test_freeze_a_executor_and_dispatch_on_overlapping_plans_serialize(executor_first, schedule):
    orders = _three()
    plan_p = plan_doc("plan-p", orders=orders[:2], run_id="run-p", truck_id="truck-1")
    plan_q = plan_doc("plan-q", orders=orders[1:], run_id="run-q", truck_id="truck-2")
    h = Harness(orders, plans=[plan_p, plan_q])
    service, _ws = _dispatch_service(
        h, _route(order_ids=["ord-2", "ord-3"], route_id="route-q",
                  plan_id="plan-q", run_id="run-q", truck_id="truck-2"),
    )
    h.store.set_yield_schedule(schedule, repeat=True)

    async def dispatch():
        return await service.dispatch(tenant_id=T, plan_doc=dict(plan_q), actor_user_id="dispatcher-1")

    calls = [h.run(plan_p), dispatch()] if executor_first else [dispatch(), h.run(plan_p)]
    results = await asyncio.gather(*calls, return_exceptions=True)
    exec_result = results[0] if executor_first else results[1]
    dispatch_result = results[1] if executor_first else results[0]

    assert isinstance(exec_result, LoadingPlanExecutionResult)
    executor_won = exec_result.outcome == "applied"
    dispatch_won = not isinstance(dispatch_result, Exception)
    assert executor_won != dispatch_won, (exec_result, dispatch_result)
    if not dispatch_won:
        assert isinstance(dispatch_result, AppException) and dispatch_result.status_code == 409
    else:
        assert exec_result.outcome == "failed" and exec_result.writes_made is False
    winner_run, winner_truck = ("run-p", "truck-1") if executor_won else ("run-q", "truck-2")
    loser_claim = None if executor_won else exec_result.attempt_id
    for oid in ("ord-1", "ord-2", "ord-3"):
        doc = h.order(oid)
        run, truck = h.links(oid)
        assert (run, truck) in {(None, None), (winner_run, winner_truck)}, (oid, run, truck)
        if doc.get("assigned_claim_id") is not None:
            assert run == winner_run
            assert doc["assigned_claim_id"] != loser_claim
        if run is None:
            assert doc["status"] == "confirmed"
    assert h.links("ord-2") == (winner_run, winner_truck)


# ---------------------------------------------------------------------------
# T-U6 shadow and mode, T-U6b strict flag read
# ---------------------------------------------------------------------------


async def test_t_u6_shadow_lists_would_apply_and_writes_nothing():
    h = Harness(_three("placed", "confirmed", "scheduled"))

    result = await h.run(mode="shadow")

    assert result.outcome == "shadow" and result.reason == "shadow_mode"
    assert result.success is False and result.writes_made is False and result.retryable is False
    assert result.attempt_id is None
    assert result.would_apply == [
        {"order_id": "ord-1", "from_status": "placed", "target_status": "scheduled", "steps": ["confirmed", "scheduled"]},
        {"order_id": "ord-2", "from_status": "confirmed", "target_status": "scheduled", "steps": ["scheduled"]},
        {"order_id": "ord-3", "from_status": "scheduled", "target_status": "scheduled", "steps": ["link"]},
    ]
    assert h.store.writes() == []
    assert h.ws.broadcast_order_status_changed.await_count == 0 and h.notified == []


async def test_t_u6_shadow_reports_refusals_without_writing():
    planned = _three()
    stored = planned[:2] + [order_fixture("ord-3", status="on_hold", hold_reason="credit")]
    h = Harness(stored, plan_orders=planned)

    result = await h.run(mode="shadow")

    assert result.outcome == "shadow"
    assert result.failures == [{"order_id": "ord-3", "reason": "order_not_loadable", "status": "on_hold"}]
    assert h.store.writes() == []


async def test_t_u6_shadow_after_partial_apply_reports_prior_writes():
    h = Harness(_three())
    h.store.fail_on("atomic_update", ORDERS, "ord-2", nth=2)
    assert (await h.run()).outcome == "incomplete"
    mark = h.mark()

    result = await h.run(mode="shadow")

    assert result.outcome == "shadow"
    assert result.writes_made is True and result.retryable is True
    assert [op for op in h.store.ops[mark:] if op[3]] == []


async def test_t_u6_shadow_of_an_applied_plan_still_reports_shadow():
    h = Harness(_three())
    assert (await h.run()).outcome == "applied"

    result = await h.run(mode="shadow")

    assert result.outcome == "shadow" and result.applied_order_ids == ["ord-1", "ord-2", "ord-3"]


async def test_t_u6_mode_unavailable_when_flag_read_raises():
    h = Harness(_three(), ff=FakeFeatureFlagService(raises=ConnectionError("down")))

    result = await h.run(mode=None)

    assert result.outcome == "failed" and result.reason == "mode_unavailable"
    assert result.retryable is True and result.writes_made is False
    assert h.store.writes() == [] and h.store.ops == []


async def test_t_u6_disabled_mode_executes():
    h = Harness(_three(), ff=FakeFeatureFlagService("disabled"))

    result = await h.run(mode=None)

    assert result.outcome == "applied"


async def test_t_u6_tenant_flag_shadow_wins_over_any_agent_override():
    # The executor never consults the lenient read nor an agent's
    # _pipeline_mode_override (R6.4); the fake raises if the lenient read is used.
    ff = FakeFeatureFlagService("shadow")
    h = Harness(_three(), ff=ff)

    result = await h.run(mode=None)

    assert result.outcome == "shadow"
    assert ff.calls == [(LOADING_FLAG_KEY, T)]
    assert h.store.writes() == []


@pytest.mark.parametrize(
    "ff, expected",
    [
        (None, "disabled"),
        (FakeFeatureFlagService(None), "disabled"),
        (FakeFeatureFlagService("active_auto"), "active_auto"),
        (FakeFeatureFlagService("bogus"), None),
        (FakeFeatureFlagService(raises=RuntimeError("not connected")), None),
    ],
    ids=["no_service", "unset", "active_auto", "invalid", "raises"],
)
async def test_resolve_mode(ff, expected):
    executor = LoadingPlanExecutor(
        es_service=InMemoryDocStore(), order_repository=object(), order_service=object(),
        feature_flag_service=ff,
    )
    assert await executor.resolve_mode(T) == expected


async def test_t_u6b_real_flag_service_over_raising_redis_is_mode_unavailable():
    ff = FeatureFlagService(redis_url="redis://unused")
    ff.client = RaisingRedis()
    h = Harness(_three(), ff=ff)

    result = await h.run(mode=None)

    assert result.reason == "mode_unavailable"
    assert ff.client.get_calls == [f"overlay_ff:{LOADING_FLAG_KEY}:{T}"]
    assert h.store.ops == []


async def test_t_u6b_real_flag_service_decodes_bytes_shadow():
    ff = FeatureFlagService(redis_url="redis://unused")
    ff.client = DictRedis({f"overlay_ff:{LOADING_FLAG_KEY}:{T}": "shadow"})
    h = Harness(_three(), ff=ff)

    result = await h.run(mode=None)

    assert result.outcome == "shadow"
    assert h.store.writes() == []


# ---------------------------------------------------------------------------
# T-U7 tenant
# ---------------------------------------------------------------------------


async def test_t_u7_cross_tenant_succeeded_plan_leaks_nothing():
    orders = [order_fixture(f"secret-{i}", tenant_id="tenant-2") for i in range(2)]
    plan = plan_doc(
        "plan-1", orders=orders, tenant_id="tenant-2", execution_status="succeeded",
        execution_result={"outcome": "applied", "order_ids": ["secret-0", "secret-1"], "plan_id": "plan-1"},
    )
    h = Harness(orders, plans=[plan])

    result = await h.run(expected_order_ids=["secret-0", "secret-1"])

    assert result.outcome == "failed" and result.reason == "plan_not_found"
    assert "secret" not in repr(result.as_dict())
    assert h.store.writes() == []


async def test_t_u7_cross_tenant_plan_in_shadow_is_not_found():
    orders = _three()
    h = Harness(orders, plans=[plan_doc("plan-1", orders=orders, tenant_id="tenant-2")])

    result = await h.run(mode="shadow")

    assert result.reason == "plan_not_found"
    assert h.store.writes() == []


# ---------------------------------------------------------------------------
# T-U8 projection check
# ---------------------------------------------------------------------------


def _projection(monkeypatch, h, *, dual_write=True, repaired=False):
    """Cut reads over to a lagging projection; record mirror calls it triggers."""
    import commerce.services.commerce_persistence_bridge as bridge

    state = {"phase": False, "mirrored": []}

    async def read_hybrid_get(aggregate_type, tenant_id, order_id):
        state["phase"] = True
        doc = h.order(order_id)
        if repaired and order_id in state["mirrored"]:
            return doc
        doc.update(status="confirmed", assigned_run_id=None, assigned_asset_id=None)
        return doc

    async def mirror(aggregate_type, doc, **kw):
        if state["phase"]:
            state["mirrored"].append(doc["order_id"])

    monkeypatch.setattr(bridge, "read_from_postgres", lambda: True)
    monkeypatch.setattr(bridge, "dual_write_enabled", lambda: dual_write)
    monkeypatch.setattr(bridge, "read_hybrid_get", read_hybrid_get)
    monkeypatch.setattr(bridge, "mirror_current_state_upsert", mirror)
    return state


async def test_t_u8_projection_lag_after_one_repair_attempt(monkeypatch):
    h = Harness(_three())
    state = _projection(monkeypatch, h)

    result = await h.run()

    assert result.outcome == "incomplete" and result.reason == "projection_lag"
    assert result.retryable is True and result.writes_made is True
    assert state["mirrored"] == ["ord-1"]
    assert h.plan()["execution_status"] == "incomplete"


async def test_t_u8_projection_repaired_by_mirror_is_applied(monkeypatch):
    h = Harness(_three())
    state = _projection(monkeypatch, h, repaired=True)

    result = await h.run()

    assert result.outcome == "applied"
    assert state["mirrored"] == ["ord-1", "ord-2", "ord-3"]


async def test_t_u8_mirror_disabled_is_projection_mirror_disabled(monkeypatch, caplog):
    h = Harness(_three())
    state = _projection(monkeypatch, h, dual_write=False)

    with caplog.at_level(logging.ERROR, logger="fuel.services.loading_plan_executor"):
        result = await h.run()

    assert result.outcome == "incomplete" and result.reason == "projection_mirror_disabled"
    assert state["mirrored"] == []
    assert any(r.levelno == logging.ERROR and "projection_mirror_disabled" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Result contract and templates (K10, NIT 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", sorted(MESSAGE_TEMPLATES))
def test_every_message_template_formats_with_plan_id_only(reason):
    text = MESSAGE_TEMPLATES[reason].format(plan_id="P1")
    assert "P1" in text
    assert _template(reason, "P1") == text


def test_from_dict_and_failure_never_raise():
    internal = MESSAGE_TEMPLATES[REASON_INTERNAL_ERROR].format(plan_id="P1")

    empty = LoadingPlanExecutionResult.from_dict({})
    assert empty.outcome == "failed" and empty.writes_made is True
    assert empty.message == MESSAGE_TEMPLATES[REASON_INTERNAL_ERROR].format(plan_id="")

    assert LoadingPlanExecutionResult.from_dict({"reason": None}).writes_made is True
    unknown = LoadingPlanExecutionResult.from_dict({"reason": "no_such_reason", "plan_id": "P1"})
    assert unknown.message == internal
    assert LoadingPlanExecutionResult.from_dict(None).outcome == "failed"
    assert LoadingPlanExecutionResult.from_dict({"order_ids": "junk", "outcome": "weird", "extra": 1}).order_ids == []

    failure = LoadingPlanExecutionResult.failure("P1", "no_such_reason", retryable=True)
    assert failure.message == internal and failure.reason == "no_such_reason"
    assert failure.writes_made is False and failure.attempt_id is None


def test_round_trip_and_builders():
    result = LoadingPlanExecutionResult.failure("P1", "plan_rejected", retryable=False)
    assert LoadingPlanExecutionResult.from_dict(result.as_dict()) == result
    assert LoadingPlanExecutionResult.unavailable("P1").reason == "executor_unavailable"
    assert LoadingPlanExecutionResult.unavailable("P1").retryable is True
    assert LoadingPlanExecutionResult.mode_unavailable("P1").reason == "mode_unavailable"
    assert LoadingPlanExecutionResult.mode_unavailable(None).writes_made is False


def test_template_never_raises_on_a_template_needing_more_fields(monkeypatch):
    import fuel.services.loading_plan_executor as mod

    monkeypatch.setitem(mod.MESSAGE_TEMPLATES, "bad", "{plan_id} {missing} {0}")
    assert _template("bad", "P1") == MESSAGE_TEMPLATES[REASON_INTERNAL_ERROR].format(plan_id="P1")
    monkeypatch.setitem(mod.MESSAGE_TEMPLATES, "bad", "{plan_id:d}")
    assert _template("bad", "P1") == MESSAGE_TEMPLATES[REASON_INTERNAL_ERROR].format(plan_id="P1")


# ---------------------------------------------------------------------------
# Logging (K10)
# ---------------------------------------------------------------------------


async def test_start_and_finish_are_logged_at_info(caplog):
    h = Harness(_three())

    with caplog.at_level(logging.INFO, logger="fuel.services.loading_plan_executor"):
        result = await h.run()

    messages = [r.getMessage() for r in caplog.records if r.name == "fuel.services.loading_plan_executor"]
    assert any("start" in m and "plan-1" in m and result.attempt_id in m and "active_gated" in m for m in messages)
    assert any("finish" in m and "applied=3" in m and "settled=0" in m for m in messages)


async def test_unexpected_write_error_is_logged_with_ids_and_kept_out_of_the_message(caplog):
    h = Harness(_three())
    h.store.fail_on("index_document", EVENTS, nth=1, exc=RuntimeError("SECRET driver text"))

    with caplog.at_level(logging.ERROR, logger="fuel.services.loading_plan_executor"):
        result = await h.run()

    record = next(r for r in caplog.records if r.levelno == logging.ERROR)
    assert record.exc_info is not None
    text = record.getMessage()
    assert T in text and "plan-1" in text and "act-1" in text and result.attempt_id in text
    assert "SECRET" not in result.message and "SECRET" not in repr(result.failures)
