"""Loading-plan approvals never commit one order twice (staging F10 d, review R4).

``ApprovalQueueService.approve`` refuses an ``apply_loading_plan`` approval
that overlaps an approved or executed plan, and otherwise expires the
overlapping pending plans before its own pending -> approved CAS. The example
tests in ``tests/unit/test_loading_plan_approval_supersede.py`` pin single
scenarios. This state machine generates plans with overlapping order sets and
interleaves approves, rejects and concurrent approve/approve and
approve/reject pairs, with the fake store yielding to the event loop a
generated number of times inside every call so the two coroutines' reads and
CAS writes interleave in different orders.

Invariants checked after every step:

* no order id is in more than one approved/executed plan;
* every expired plan names the plan that superseded it
  (``execution_result.superseded_by``), and the two share an order;
* a sequential approve succeeds exactly when no committed plan overlaps it,
  and a refused one leaves its own entry pending;
* in a concurrent pair, a call that raised did not commit its own entry.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)

from Agents.approval_queue_service import ApprovalQueueService

TENANT = "t1"
ORDERS = [f"ord_{c}" for c in "ABCD"]
COMMITTED = {"approved", "executed"}
REFUSALS = (ValueError, RuntimeError)


class _YieldingStore:
    """In-memory approval index with the facade's ``atomic_update`` contract.

    Every call first yields to the event loop ``schedule.pop(0)`` times (0
    once the schedule runs out). The read, the transform and the write of
    ``atomic_update`` happen with no await between them, which is the
    atomicity the real store gives a single CAS.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.schedule: List[int] = []

    async def _yield(self) -> None:
        for _ in range(self.schedule.pop(0) if self.schedule else 0):
            await asyncio.sleep(0)

    async def get_document(self, index, doc_id):
        await self._yield()
        doc = self.docs.get(doc_id)
        return dict(doc) if doc else None

    async def search_documents(self, index, query, size=None):
        await self._yield()
        terms: Dict[str, Any] = {}
        for f in query["query"]["bool"]["filter"]:
            for kind in ("term", "terms"):
                if kind in f:
                    terms.update(f[kind])

        def _ok(doc):
            for field, want in terms.items():
                value = doc.get(field)
                if (value not in want) if isinstance(want, list) else (value != want):
                    return False
            return True

        hits = [{"_source": dict(d)} for d in self.docs.values() if _ok(d)]
        return {"hits": {"hits": hits, "total": {"value": len(hits)}}}

    async def atomic_update(self, index, doc_id, transform, **kwargs):
        await self._yield()
        current = dict(self.docs[doc_id])
        updated = transform(current)
        if updated is None:
            return current, False
        self.docs[doc_id] = dict(updated)
        return dict(updated), True

    async def index_document(self, index, doc_id, doc):
        await self._yield()
        self.docs[doc_id] = dict(doc)


def _plan_doc(action_id: str, order_ids: List[str]) -> Dict[str, Any]:
    return {
        "action_id": action_id,
        "action_type": "mutation",
        "tool_name": "apply_loading_plan",
        "parameters": {
            "plan_id": f"plan-{action_id}",
            "truck_id": f"truck-{action_id}",
            "order_ids": list(order_ids),
            "assignments": [{"order_id": o, "quantity_liters": 500.0} for o in order_ids],
        },
        "risk_level": "high",
        "proposed_by": "compartment_loading",
        "status": "pending",
        "tenant_id": TENANT,
    }


def _orders(doc: Dict[str, Any]) -> set:
    return set(doc["parameters"]["order_ids"])


order_sets = st.lists(st.sampled_from(ORDERS), min_size=1, max_size=2, unique=True)
schedules = st.lists(st.integers(min_value=0, max_value=2), min_size=2, max_size=8)


class SupersedeMachine(RuleBasedStateMachine):
    plans = Bundle("plans")

    @initialize(with_executor=st.booleans())
    def setup(self, with_executor: bool) -> None:
        self.store = _YieldingStore()
        es = MagicMock()
        es.get_document = AsyncMock(side_effect=self.store.get_document)
        es.search_documents = AsyncMock(side_effect=self.store.search_documents)
        es.atomic_update = AsyncMock(side_effect=self.store.atomic_update)
        es.index_document = AsyncMock(side_effect=self.store.index_document)
        ws = MagicMock()
        ws.broadcast_approval_event = AsyncMock()
        activity = MagicMock()
        activity.log = AsyncMock(return_value="log-1")
        confirmation = None
        if with_executor:
            # What staging does today: no executor, so approve records executed.
            confirmation = MagicMock()
            confirmation._execute_mutation = AsyncMock(
                return_value="Unknown tool apply_loading_plan — no mutation executed"
            )
        self.svc = ApprovalQueueService(
            es_service=es, ws_manager=ws, activity_log_service=activity,
            confirmation_protocol=confirmation,
        )
        self.next_id = 0
        self.next_reviewer = 0

    # -- helpers -----------------------------------------------------------

    def _status(self, action_id: str) -> str:
        return self.store.docs[action_id]["status"]

    def _committed_overlap(self, action_id: str) -> bool:
        mine = _orders(self.store.docs[action_id])
        return any(
            other_id != action_id
            and doc["status"] in COMMITTED
            and _orders(doc) & mine
            for other_id, doc in self.store.docs.items()
        )

    @staticmethod
    async def _outcome(coro) -> Optional[BaseException]:
        try:
            await coro
        except REFUSALS as exc:
            return exc
        return None

    # -- rules -------------------------------------------------------------

    @rule(target=plans, order_ids=order_sets)
    def propose(self, order_ids: List[str]) -> str:
        action_id = f"P{self.next_id}"
        self.next_id += 1
        self.store.docs[action_id] = _plan_doc(action_id, order_ids)
        return action_id

    @rule(action_id=plans)
    def approve(self, action_id: str) -> None:
        was_pending = self._status(action_id) == "pending"
        blocked = self._committed_overlap(action_id)

        error = asyncio.run(self._outcome(self.svc.approve(action_id, self._reviewer())))

        if not was_pending:
            assert isinstance(error, ValueError)
            return
        if blocked:
            assert isinstance(error, ValueError)
            assert self._status(action_id) == "pending"
        else:
            # Disjoint from every committed plan: the approve goes through.
            assert error is None, error
            assert self._status(action_id) in COMMITTED

    @rule(action_id=plans)
    def reject(self, action_id: str) -> None:
        was_pending = self._status(action_id) == "pending"
        error = asyncio.run(self._outcome(self.svc.reject(action_id, self._reviewer(), "no")))
        if was_pending:
            assert error is None
            assert self._status(action_id) == "rejected"
        else:
            assert isinstance(error, ValueError)

    def _reviewer(self) -> str:
        """A reviewer id unique to one call, so its writes are attributable."""
        self.next_reviewer += 1
        return f"reviewer-{self.next_reviewer}"

    def _assert_approve_outcome(
        self, action_id: str, reviewer: str, error: Optional[BaseException]
    ) -> None:
        doc = self.store.docs[action_id]
        committed_by_call = doc["status"] in COMMITTED and doc.get("reviewed_by") == reviewer
        if error is None:
            assert committed_by_call
        else:
            # A refused approve never committed its own entry.
            assert not committed_by_call

    @rule(first=plans, second=plans, schedule=schedules)
    def concurrent_approves(self, first: str, second: str, schedule: List[int]) -> None:
        reviewers = (self._reviewer(), self._reviewer())
        self.store.schedule = list(schedule)

        async def _both():
            return await asyncio.gather(
                self._outcome(self.svc.approve(first, reviewers[0])),
                self._outcome(self.svc.approve(second, reviewers[1])),
            )

        errors = asyncio.run(_both())
        self.store.schedule = []
        for action_id, reviewer, error in zip((first, second), reviewers, errors):
            self._assert_approve_outcome(action_id, reviewer, error)

    @rule(to_approve=plans, to_reject=plans, schedule=schedules)
    def concurrent_approve_and_reject(
        self, to_approve: str, to_reject: str, schedule: List[int]
    ) -> None:
        approver, rejecter = self._reviewer(), self._reviewer()
        self.store.schedule = list(schedule)

        async def _both():
            return await asyncio.gather(
                self._outcome(self.svc.approve(to_approve, approver)),
                self._outcome(self.svc.reject(to_reject, rejecter, "no")),
            )

        approve_error, reject_error = asyncio.run(_both())
        self.store.schedule = []
        self._assert_approve_outcome(to_approve, approver, approve_error)
        rejected = self.store.docs[to_reject]
        if reject_error is None:
            assert rejected["status"] == "rejected"
            assert rejected["reviewed_by"] == rejecter
        else:
            assert rejected.get("reviewed_by") != rejecter

    # -- invariants --------------------------------------------------------

    @invariant()
    def no_order_in_two_committed_plans(self) -> None:
        if not hasattr(self, "store"):
            return
        holder: Dict[str, str] = {}
        for action_id, doc in self.store.docs.items():
            if doc["status"] not in COMMITTED:
                continue
            for order_id in _orders(doc):
                assert order_id not in holder, (
                    f"{order_id} committed in {holder.get(order_id)} and {action_id}"
                )
                holder[order_id] = action_id

    @invariant()
    def expired_plans_name_an_overlapping_superseder(self) -> None:
        if not hasattr(self, "store"):
            return
        for action_id, doc in self.store.docs.items():
            if doc["status"] != "expired":
                continue
            result = doc.get("execution_result") or {}
            superseder = result.get("superseded_by")
            assert superseder in self.store.docs, (action_id, result)
            assert result.get("reason") == "conflicting_loading_plan"
            assert _orders(self.store.docs[superseder]) & _orders(doc)


SupersedeMachine.TestCase.settings = settings(
    max_examples=150, stateful_step_count=25, deadline=None
)
TestSupersedeStateMachine = SupersedeMachine.TestCase


class TestRacingOverlappingApprovals:
    """The race the state machine reaches only sometimes, every example.

    Two pending plans that share an order are approved concurrently under a
    generated interleaving. At most one may commit; a loser stays uncommitted;
    disjoint third plans are unaffected.
    """

    @settings(max_examples=300, deadline=None)
    @given(
        shared=st.sampled_from(ORDERS),
        extra_a=order_sets,
        extra_b=order_sets,
        schedule=schedules,
        with_executor=st.booleans(),
        reject_instead=st.booleans(),
    )
    def test_two_racing_overlapping_approvals_never_both_commit(
        self, shared, extra_a, extra_b, schedule, with_executor, reject_instead
    ):
        machine = SupersedeMachine()
        machine.setup(with_executor=with_executor)
        a = machine.propose(sorted({shared, *extra_a}))
        b = machine.propose(sorted({shared, *extra_b}))
        bystander = machine.propose(["ord_Z"])

        if reject_instead:
            machine.concurrent_approve_and_reject(a, b, schedule)
        else:
            machine.concurrent_approves(a, b, schedule)

        machine.no_order_in_two_committed_plans()
        machine.expired_plans_name_an_overlapping_superseder()
        committed = [x for x in (a, b) if machine._status(x) in COMMITTED]
        assert len(committed) <= 1
        assert machine._status(bystander) == "pending"
