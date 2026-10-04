"""Approving a loading plan cannot double-dispatch an order (staging F10 d).

Each truck's plan is its own ``apply_loading_plan`` approval. Plans from one
loading run are order-disjoint, so approving all of them is correct. Across
runs they are not: a dispatcher who re-runs the plan while older approvals are
still pending gets the same orders proposed again, and before this fix both
copies could be approved.

Approving one plan now expires every overlapping pending plan
(``execution_result.superseded_by``) and refuses when an overlapping plan is
already approved.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.approval_queue_service import ApprovalQueueService

TENANT = "t1"


class _Store:
    """In-memory approval index with the facade's ``atomic_update`` contract."""

    def __init__(self, docs: List[Dict[str, Any]]):
        self.docs = {d["action_id"]: dict(d) for d in docs}
        self.before_update = None  # hook to simulate a concurrent writer

    async def get_document(self, index, doc_id):
        doc = self.docs.get(doc_id)
        return dict(doc) if doc else None

    async def search_documents(self, index, query, size=None):
        filters = query["query"]["bool"]["filter"]
        terms: Dict[str, Any] = {}
        for f in filters:
            for kind in ("term", "terms"):
                if kind in f:
                    terms.update(f[kind])

        def _ok(doc):
            for field, want in terms.items():
                if isinstance(want, list):
                    if doc.get(field) not in want:
                        return False
                elif doc.get(field) != want:
                    return False
            return True

        hits = [{"_source": dict(d)} for d in self.docs.values() if _ok(d)]
        return {"hits": {"hits": hits, "total": {"value": len(hits)}}}

    async def atomic_update(self, index, doc_id, transform, **kwargs):
        if self.before_update is not None:
            self.before_update(doc_id)
        current = dict(self.docs[doc_id])
        updated = transform(current)
        if updated is None:
            return current, False
        self.docs[doc_id] = dict(updated)
        return dict(updated), True

    async def index_document(self, index, doc_id, doc):
        self.docs[doc_id] = dict(doc)


def _plan(action_id: str, order_ids: List[str], *, status: str = "pending",
          tenant_id: str = TENANT, run_id: str = "run-1",
          legacy_params: bool = False) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "plan_id": f"plan-{action_id}",
        "truck_id": f"truck-{action_id}",
        "assignments": [
            {"order_id": o, "quantity_liters": 1000.0} for o in order_ids
        ],
        "total_utilization_pct": 50.0,
    }
    if not legacy_params:
        params["order_ids"] = list(order_ids)
        params["run_id"] = run_id
    return {
        "action_id": action_id,
        "action_type": "mutation",
        "tool_name": "apply_loading_plan",
        "parameters": params,
        "risk_level": "high",
        "proposed_by": "compartment_loading",
        "status": status,
        "tenant_id": tenant_id,
    }


def _service(*docs: Dict[str, Any], feedback=None):
    store = _Store(list(docs))
    es = MagicMock()
    es.get_document = AsyncMock(side_effect=store.get_document)
    es.search_documents = AsyncMock(side_effect=store.search_documents)
    es.atomic_update = AsyncMock(side_effect=store.atomic_update)
    es.index_document = AsyncMock(side_effect=store.index_document)
    ws = MagicMock()
    ws.broadcast_approval_event = AsyncMock()
    activity = MagicMock()
    activity.log = AsyncMock(return_value="log-1")
    svc = ApprovalQueueService(
        es_service=es, ws_manager=ws, activity_log_service=activity,
        feedback_service=feedback,
    )
    return svc, store, ws, activity


def _actions(activity, action_type: str) -> List[Dict[str, Any]]:
    return [
        c.args[0] for c in activity.log.await_args_list
        if c.args[0].get("action_type") == action_type
    ]


class TestSupersede:
    @pytest.mark.asyncio
    async def test_approving_a_plan_expires_overlapping_pending_plans(self):
        feedback = MagicMock()
        feedback.record_rejection = AsyncMock()
        svc, store, ws, activity = _service(
            _plan("A", ["ord_A", "ord_B"], run_id="run-2"),
            _plan("B", ["ord_B"], run_id="run-1"),
            feedback=feedback,
        )

        result = await svc.approve("A", reviewer_id="dispatcher-1")

        assert result["status"] == "approved"
        b = store.docs["B"]
        assert b["status"] == "expired"
        assert b["execution_result"] == {
            "superseded_by": "A", "reason": "conflicting_loading_plan",
        }
        assert b["reviewed_at"]
        events = [c.args[0] for c in ws.broadcast_approval_event.await_args_list]
        assert "approval_expired" in events
        (logged,) = _actions(activity, "approval_superseded")
        assert logged["details"] == {
            "action_id": "B", "superseded_by": "A", "order_ids": ["ord_B"],
        }
        # A system supersede is not a reviewer rejection.
        feedback.record_rejection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_superseded_plan_can_no_longer_be_approved(self):
        svc, store, _, _ = _service(_plan("A", ["ord_A"]), _plan("B", ["ord_A"]))
        await svc.approve("A", reviewer_id="dispatcher-1")

        with pytest.raises(ValueError, match="expired"):
            await svc.approve("B", reviewer_id="dispatcher-2")
        assert store.docs["B"]["status"] == "expired"

    @pytest.mark.asyncio
    async def test_conflict_with_an_approved_plan_refuses_and_stays_pending(self):
        svc, store, _, _ = _service(
            _plan("A", ["ord_A"], status="approved"),
            _plan("C", ["ord_A", "ord_C"]),
            _plan("D", ["ord_C"]),
        )

        with pytest.raises(ValueError, match="conflicts with approved loading plan A"):
            await svc.approve("C", reviewer_id="dispatcher-1")

        assert store.docs["C"]["status"] == "pending"
        # Refused before anything was superseded.
        assert store.docs["D"]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_conflict_with_an_executed_plan_refuses(self):
        svc, store, _, _ = _service(
            _plan("A", ["ord_A"], status="executed"), _plan("C", ["ord_A"]),
        )
        with pytest.raises(ValueError, match="conflicts with approved loading plan A"):
            await svc.approve("C", reviewer_id="dispatcher-1")
        assert store.docs["C"]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_disjoint_plans_from_one_run_all_approve(self):
        svc, store, _, _ = _service(
            _plan("T1", ["ord_A"]), _plan("T2", ["ord_B"]),
        )
        await svc.approve("T1", reviewer_id="dispatcher-1")
        await svc.approve("T2", reviewer_id="dispatcher-1")
        assert store.docs["T1"]["status"] == "approved"
        assert store.docs["T2"]["status"] == "approved"

    @pytest.mark.asyncio
    async def test_concurrent_change_is_a_conflict_and_leaves_own_entry_pending(self):
        svc, store, _, _ = _service(_plan("A", ["ord_A"]), _plan("B", ["ord_A"]))

        def _other_reviewer_wins(doc_id):
            # Another reviewer approves B between our read and our expire.
            if doc_id == "B" and store.docs["B"]["status"] == "pending":
                store.docs["B"]["status"] = "approved"

        store.before_update = _other_reviewer_wins

        with pytest.raises(ValueError, match="changed while approving"):
            await svc.approve("A", reviewer_id="dispatcher-1")
        assert store.docs["A"]["status"] == "pending"
        assert store.docs["B"]["status"] == "approved"

    @pytest.mark.asyncio
    async def test_order_ids_fall_back_to_assignments(self):
        """Approvals queued before order_ids was added still overlap-check."""
        svc, store, _, _ = _service(
            _plan("A", ["ord_A"]), _plan("OLD", ["ord_A"], legacy_params=True),
        )
        await svc.approve("A", reviewer_id="dispatcher-1")
        assert store.docs["OLD"]["status"] == "expired"

    @pytest.mark.asyncio
    async def test_other_tenants_and_tools_are_untouched(self):
        other_tool = _plan("J", ["ord_A"])
        other_tool["tool_name"] = "cancel_job"
        svc, store, _, _ = _service(
            _plan("A", ["ord_A"]),
            _plan("X", ["ord_A"], tenant_id="t2"),
            other_tool,
        )
        await svc.approve("A", reviewer_id="dispatcher-1")
        assert store.docs["X"]["status"] == "pending"
        assert store.docs["J"]["status"] == "pending"
