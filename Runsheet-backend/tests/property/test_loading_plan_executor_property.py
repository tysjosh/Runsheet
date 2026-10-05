"""No order is ever committed to two trucks (loading-plan-executor property test).

A hypothesis ``RuleBasedStateMachine`` in the pattern of
``tests/property/test_loading_plan_supersede_property.py`` (b01b211), over the
real ``ApprovalQueueService``, ``ConfirmationProtocol``,
``LoadingPlanExecutor``, ``FuelOrderRepository`` and ``OrderService`` on a
yielding ``InMemoryDocStore`` (``tests.unit._loading_plan_fakes.ApprovalHarness``)
with a ``TickingClock`` for the plan lease: 4 orders, 3 trucks.

Rules (design test plan, property test): ``propose``; ``approve``, ``reject``,
``retry``; ``concurrent_approves``; ``auto_execute`` (the full-auto path through
``process_mutation``) racing an approve; ``expire_sweep`` racing an approve;
``stall`` (an attempt cancelled after its pending -> approved CAS) and
``clock_jump`` so a later ``retry`` reclaims it; ``approve_racing_foreign_claim``
(a ``claim_assignment(..., claim_id="foreign")`` for an unrelated run, the MVP
dispatch stand-in); ``approve_racing_cancel``; ``inject_fault``; ``set_mode``
(``shadow`` | ``active_gated`` | ``unavailable``); ``unwire_executor`` /
``rewire_executor``; ``concurrent_retry`` (an unresolvable-mode retry racing a
resolvable one on the same entry).

Invariant 1 is the user-required property: under any interleaving of approvals
over overlapping plans, every order is linked to at most one run, and that run's
truck. Invariants 2-9 are the design's (L1050-1059).
"""
from __future__ import annotations

import asyncio
import copy
from typing import Any, Dict, List, Optional, Set

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)

from Agents.approval_queue_service import _holds_orders
from Agents.confirmation_protocol import MutationRequest
from fuel.order_state_machine import TERMINAL_STATUSES
from tests.unit._loading_plan_fakes import (
    APPROVALS,
    EVENTS,
    ORDERS,
    PLANS,
    ApprovalHarness,
    TickingClock,
    order_fixture,
)

T = "tenant-1"
ORDER_IDS = ["o1", "o2", "o3", "o4"]
TRUCKS = ["truck-1", "truck-2", "truck-3"]
FOREIGN_RUN, FOREIGN_TRUCK = "foreign-run", "foreign-truck"
EXPECTED = (Exception,)  # every refusal surfaces as an exception the rule tolerates

order_sets = st.lists(st.sampled_from(ORDER_IDS), min_size=1, max_size=3, unique=True)
schedules = st.lists(st.integers(min_value=0, max_value=2), min_size=1, max_size=6)


class ModeFF:
    """Strict flag read: ``queue`` steps first (one per call), then ``mode``.

    A step or mode of ``"unavailable"`` raises like a Redis outage.
    """

    def __init__(self) -> None:
        self.mode = "active_gated"
        self.queue: List[str] = []

    async def get_overlay_state_strict(self, flag_key: str, tenant_id: str) -> Optional[str]:
        step = self.queue.pop(0) if self.queue else self.mode
        if step == "unavailable":
            raise ConnectionError("redis unavailable")
        return step


async def _quiet(coro) -> Any:
    try:
        return await coro
    except EXPECTED as exc:
        return exc


class ExecutorMachine(RuleBasedStateMachine):
    entries = Bundle("entries")

    @initialize()
    def setup(self) -> None:
        self.ff = ModeFF()
        self.clock = TickingClock()
        self.h = ApprovalHarness(
            [order_fixture(o, tenant_id=T) for o in ORDER_IDS],
            ff=self.ff, clock=self.clock, lock_timeout=0.5,
        )
        self.next_id = 0
        self.plans: Dict[str, Dict[str, Any]] = {}  # action_id -> {run, truck, orders}
        self.cancelled: Set[str] = set()
        self.sticky: Dict[str, tuple] = {}  # action_id -> (attempt_id, writes_made)

    # -- helpers -----------------------------------------------------------

    def _new_plan(self, order_ids: List[str], truck: str) -> str:
        action_id = f"A{self.next_id}"
        self.next_id += 1
        self.h.add_plan(action_id, sorted(order_ids), truck_id=truck, run_id=f"run-{action_id}")
        self.plans[action_id] = {"run": f"run-{action_id}", "truck": truck, "orders": set(order_ids)}
        return action_id

    def _run(self, *coros, schedule=None) -> List[Any]:
        async def _all():
            return await asyncio.gather(*(_quiet(c) for c in coros))

        self.h.store.set_yield_schedule(schedule, repeat=True) if schedule else None
        try:
            return asyncio.run(_all())
        finally:
            self.h.store.set_yield_schedule(None)

    def _approve(self, action_id: str, user: str = "dispatcher-1"):
        return self.h.approve(action_id, user=user)

    # -- rules -------------------------------------------------------------

    @rule(target=entries, order_ids=order_sets, truck=st.sampled_from(TRUCKS))
    def propose(self, order_ids, truck) -> str:
        return self._new_plan(order_ids, truck)

    @rule(a=entries)
    def approve(self, a) -> None:
        self._run(self._approve(a))

    @rule(a=entries)
    def retry(self, a) -> None:
        self._run(self._approve(a, user="dispatcher-2"))

    @rule(a=entries)
    def reject(self, a) -> None:
        self._run(self.h.reject(a))

    @rule(a=entries, b=entries, schedule=schedules)
    def concurrent_approves(self, a, b, schedule) -> None:
        self._run(self._approve(a), self._approve(b, user="dispatcher-2"), schedule=schedule)

    @rule(target=entries, order_ids=order_sets, truck=st.sampled_from(TRUCKS), a=entries, schedule=schedules)
    def auto_execute(self, order_ids, truck, a, schedule) -> str:
        seed = self._new_plan(order_ids, truck)
        params = self.h.entry(seed)["parameters"]
        self.h.store.remove(APPROVALS, seed)  # process_mutation creates the entry
        request = MutationRequest(
            tool_name="apply_loading_plan", parameters=params, tenant_id=T, agent_id="compartment_loading",
        )
        self.h.autonomy.get_level.return_value = "full-auto"
        before = set(self.h.store.docs[APPROVALS])
        try:
            self._run(self.h.protocol.process_mutation(request), self._approve(a), schedule=schedule)
        finally:
            self.h.autonomy.get_level.return_value = "suggest-only"
        created = sorted(set(self.h.store.docs[APPROVALS]) - before)
        assert len(created) == 1, created
        self.plans[created[0]] = self.plans.pop(seed)
        return created[0]

    @rule(a=entries, schedule=schedules)
    def expire_sweep(self, a, schedule) -> None:
        store = self.h.store
        real_search = store.search_documents

        async def search(index, query, size=10):
            if "range" in str(query):  # the sweeper sees every pending entry as expired
                await real_search(index, query, size)
                hits = [{"_source": copy.deepcopy(d)} for d in store.docs[APPROVALS].values() if d["status"] == "pending"]
                return {"hits": {"hits": hits, "total": {"value": len(hits)}}}
            return await real_search(index, query, size)

        store.search_documents = search
        try:
            self._run(self.h.svc.expire_stale(), self._approve(a), schedule=schedule)
        finally:
            store.search_documents = real_search

    @rule(a=entries, nth=st.integers(min_value=1, max_value=3))
    def stall(self, a, nth) -> None:
        """An approve cancelled mid-attempt (pod kill), after its pending -> approved CAS."""
        h = self.h

        async def _stall():
            reached = asyncio.Event()
            seen = []

            async def hook(op, index, doc_id):
                if (op, index) == ("atomic_update", ORDERS):
                    seen.append(doc_id)
                    if len(seen) == nth:
                        reached.set()
                        await asyncio.Event().wait()  # never returns; cancelled below

            h.store.hooks.append(hook)
            try:
                task = asyncio.create_task(_quiet(self._approve(a)))
                waiter = asyncio.create_task(reached.wait())
                await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                waiter.cancel()
            finally:
                h.store.hooks.remove(hook)

        asyncio.run(_stall())

    @rule()
    def clock_jump(self) -> None:
        """Every lease runs out: approval claims and plan claims look stale."""
        self.clock.advance(300)
        for action_id, doc in list(self.h.store.docs[APPROVALS].items()):
            r = doc.get("execution_result") or {}
            if doc.get("status") == "approved" and r.get("claimed_at"):
                self.h.store.poke(APPROVALS, action_id, execution_result={**r, "claimed_at": "2000-01-01T00:00:00+00:00"})

    @rule(a=entries, order=st.sampled_from(ORDER_IDS), schedule=schedules)
    def approve_racing_foreign_claim(self, a, order, schedule) -> None:
        status = self.h.order(order)["status"]
        self._run(
            self._approve(a),
            self.h.repo.claim_assignment(
                T, order, run_id=FOREIGN_RUN, asset_id=FOREIGN_TRUCK, expected_status=status, claim_id="foreign",
            ),
            schedule=schedule,
        )

    @rule(a=entries, order=st.sampled_from(ORDER_IDS), schedule=schedules)
    def approve_racing_cancel(self, a, order, schedule) -> None:
        copy_ = self.h.order(order)
        if copy_["status"] in TERMINAL_STATUSES:
            return
        self._run(
            self._approve(a),
            self.h.order_service.apply_status_transition(
                copy_, "cancelled", reason="customer_request", actor_user_id="dispatcher-9",
            ),
            schedule=schedule,
        )

    @rule(target_=st.sampled_from([
        ("atomic_update", ORDERS), ("index_document", EVENTS), ("atomic_update", PLANS),
    ]))
    def inject_fault(self, target_) -> None:
        method, index = target_
        self.h.store.fail_on(method, index, nth=1)

    @rule(mode=st.sampled_from(["shadow", "active_gated", "unavailable"]))
    def set_mode(self, mode) -> None:
        self.ff.mode = mode

    @rule()
    def unwire_executor(self) -> None:
        self.h.protocol.set_loading_plan_executor(None)

    @rule()
    def rewire_executor(self) -> None:
        self.h.protocol.set_loading_plan_executor(self.h.executor)

    @rule(a=entries, schedule=schedules)
    def concurrent_retry(self, a, schedule) -> None:
        self.ff.queue = ["unavailable", "active_gated"]
        try:
            self._run(self._approve(a), self._approve(a, user="dispatcher-2"), schedule=schedule)
        finally:
            self.ff.queue = []

    # -- invariants --------------------------------------------------------

    def _linked(self, action_id: str, *, include_terminal: bool = True) -> List[str]:
        run = self.plans[action_id]["run"]
        return [
            o for o in ORDER_IDS
            if self.h.order(o).get("assigned_run_id") == run
            and (include_terminal or self.h.order(o)["status"] not in TERMINAL_STATUSES)
        ]

    def _entries(self) -> Dict[str, Dict[str, Any]]:
        return {a: d for a, d in self.h.store.docs[APPROVALS].items() if a in self.plans}

    @invariant()
    def i1_each_order_on_at_most_one_run_and_its_truck(self) -> None:
        if not hasattr(self, "h"):
            return
        trucks = {p["run"]: p["truck"] for p in self.plans.values()}
        trucks[FOREIGN_RUN] = FOREIGN_TRUCK
        for o in ORDER_IDS:
            run, asset = self.h.links(o)
            if run is None:
                assert asset is None, (o, run, asset)
                continue
            assert run in trucks, (o, run)
            assert asset == trucks[run], (o, run, asset)

    @invariant()
    def i2_no_order_held_by_two_holding_approvals(self) -> None:
        if not hasattr(self, "h"):
            return
        holder: Dict[str, str] = {}
        for a, doc in self._entries().items():
            if not _holds_orders(doc):
                continue
            for o in self.plans[a]["orders"]:
                assert o not in holder, f"{o} held by {holder.get(o)} and {a}"
                holder[o] = a

    @invariant()
    def i3_executed_plans_have_their_orders_scheduled_and_linked(self) -> None:
        if not hasattr(self, "h"):
            return
        for a, doc in self._entries().items():
            if doc["status"] != "executed":
                continue
            for o in self.plans[a]["orders"]:
                order = self.h.order(o)
                if order["status"] in TERMINAL_STATUSES:
                    continue
                assert order["status"] == "scheduled", (a, o, order["status"])
                assert self.h.links(o) == (self.plans[a]["run"], self.plans[a]["truck"]), (a, o)

    @invariant()
    def i4_failed_or_shadowed_plans_linked_nothing(self) -> None:
        if not hasattr(self, "h"):
            return
        for a, doc in self._entries().items():
            if doc["status"] in ("failed", "shadowed"):
                assert self._linked(a) == [], (a, doc["status"], self._linked(a))

    @invariant()
    def i5_nothing_dispatched_and_cancelled_stays_cancelled(self) -> None:
        if not hasattr(self, "h"):
            return
        for o in ORDER_IDS:
            status = self.h.order(o)["status"]
            assert status != "dispatched"
            if o in self.cancelled:
                assert status == "cancelled", (o, status)
            if status == "cancelled":
                self.cancelled.add(o)

    @invariant()
    def i6_linked_non_terminal_orders_belong_to_holding_approvals(self) -> None:
        if not hasattr(self, "h"):
            return
        entries = self._entries()
        for a in self.plans:
            linked = self._linked(a, include_terminal=False)
            if linked:
                assert a in entries and _holds_orders(entries[a]), (a, linked, entries.get(a, {}).get("status"))

    @invariant()
    def i7_no_two_overlapping_plans_both_partly_linked(self) -> None:
        if not hasattr(self, "h"):
            return
        with_links = [a for a in self.plans if self._linked(a)]
        for i, a in enumerate(with_links):
            for b in with_links[i + 1:]:
                assert not (self.plans[a]["orders"] & self.plans[b]["orders"]), (a, b)

    @invariant()
    def i8_sticky_writes(self) -> None:
        if not hasattr(self, "h"):
            return
        for a, doc in self._entries().items():
            r = doc.get("execution_result") or {}
            if doc["status"] == "incomplete" and self._linked(a):
                assert r.get("writes_made") is True, (a, r)
            if doc["status"] == "rejected":
                assert self._linked(a, include_terminal=False) == [], a
            if "writes_made" in r:
                prev = self.sticky.get(a)
                if prev is not None and prev[1] is True and r["writes_made"] is False:
                    # Only a new attempt's authoritative result may clear it.
                    assert r.get("attempt_id") != prev[0], (a, prev, r)
                self.sticky[a] = (r.get("attempt_id"), r.get("writes_made"))

    @invariant()
    def i9_pending_entries_hold_nothing(self) -> None:
        if not hasattr(self, "h"):
            return
        for a, doc in self._entries().items():
            if doc["status"] != "pending":
                continue
            assert not _holds_orders(doc)
            assert not doc.get("reviewed_by"), a
            assert not (doc.get("execution_result") or {}).get("attempt_id"), a
            assert self._linked(a) == [], a


ExecutorMachine.TestCase.settings = settings(max_examples=150, stateful_step_count=25, deadline=None)
TestLoadingPlanExecutorStateMachine = ExecutorMachine.TestCase


class TestRacingOverlappingApprovals:
    """Two overlapping approvals race under a generated interleaving, every example."""

    @settings(max_examples=150, deadline=None)
    @given(
        shared=st.sampled_from(ORDER_IDS),
        extra_a=order_sets,
        extra_b=order_sets,
        trucks=st.tuples(st.sampled_from(TRUCKS), st.sampled_from(TRUCKS)),
        schedule=schedules,
        mode=st.sampled_from(["active_gated", "shadow"]),
    )
    def test_two_racing_overlapping_approvals_never_double_commit(
        self, shared, extra_a, extra_b, trucks, schedule, mode
    ):
        m = ExecutorMachine()
        m.setup()
        m.ff.mode = mode
        a = m.propose(sorted({shared, *extra_a}), trucks[0])
        b = m.propose(sorted({shared, *extra_b}), trucks[1])
        m.concurrent_approves(a, b, schedule)

        m.i1_each_order_on_at_most_one_run_and_its_truck()
        m.i2_no_order_held_by_two_holding_approvals()
        m.i3_executed_plans_have_their_orders_scheduled_and_linked()
        m.i4_failed_or_shadowed_plans_linked_nothing()
        m.i6_linked_non_terminal_orders_belong_to_holding_approvals()
        m.i7_no_two_overlapping_plans_both_partly_linked()
        m.i9_pending_entries_hold_nothing()
        linked = [x for x in (a, b) if m._linked(x)]
        assert len(linked) <= (1 if mode == "active_gated" else 0)
        # Two guards may expire each other (both lose), so at most one executes.
        assert [m.h.status(x) for x in (a, b)].count("executed") <= 1
