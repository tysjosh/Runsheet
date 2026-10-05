"""T-U16 (loading-plan-executor K11, R8, R3.10): applied orders leave the next
loading run.

* The document-store query excludes orders with a linked ``assigned_run_id``
  and keeps ``""``, null, absent and hand-scheduled orders (R8.1-R8.3),
  evaluated with ``persistence.document_matcher.matches``.
* The hybrid read passes ``unlinked_fields=["assigned_run_id"]``.
* Every loadable order committed: no request, no legacy fallback, degradation
  ``all_loadable_orders_committed`` with kind ``no_input``.
* Committed tank draw: volume on applied plans or in flight comes off the
  tank's ullage first; a read error fails closed for known tanks.
* ``_build_proposal`` carries ``order_snapshots``.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from Agents.overlay.base_overlay_agent import (
    CYCLE_METRIC_DEGRADATION_REASONS,
    DEGRADATION_KIND_NO_INPUT,
)
from Agents.overlay.compartment_loading_agent import (
    COMMITTED_ORDER_FIELD,
    _unlinked_clause,
)
from persistence.document_matcher import matches
from services.unit_conversion import GAL_TO_L
from tests.unit._loading_plan_fakes import (
    ORDERS,
    PLANS,
    InMemoryDocStore,
    fuel_order_doc,
    loading_agent,
    priority_list_for,
    seed_fleet,
)

TENANT = "tenant-1"


def _gal(liters: float) -> float:
    return liters / GAL_TO_L


def _reasons(agent) -> List[Dict[str, Any]]:
    return list(agent._cycle_metrics.get(CYCLE_METRIC_DEGRADATION_REASONS) or [])


class _MatchingES:
    """Records every query and answers it with ``document_matcher.matches``."""

    def __init__(self, docs: List[Dict[str, Any]]) -> None:
        self.docs = docs
        self.queries: List[Dict[str, Any]] = []

    async def search_documents(self, index, query, size=10):
        self.queries.append(query)
        body = (query or {}).get("query")
        return {"hits": {"hits": [
            {"_source": d} for d in self.docs if index == ORDERS and matches(d, body)
        ]}}


# ---------------------------------------------------------------------------
# Document-store query (R8.1, R8.2, R8.3)
# ---------------------------------------------------------------------------


class TestDocumentStoreQuery:
    async def test_committed_excluded_and_unlinked_included(self):
        absent = fuel_order_doc("ord-absent")
        absent.pop(COMMITTED_ORDER_FIELD)
        docs = [
            fuel_order_doc("ord-committed", status="scheduled", assigned_run_id="run-1",
                           assigned_asset_id="truck-1"),
            fuel_order_doc("ord-empty", assigned_run_id=""),
            fuel_order_doc("ord-null", status="placed", assigned_run_id=None),
            absent,
            # R8.3: hand-scheduled, no run id -> still loadable.
            fuel_order_doc("ord-hand-scheduled", status="scheduled"),
            fuel_order_doc("ord-dispatched", status="dispatched"),
            fuel_order_doc("ord-other-tenant", tenant_id="tenant-2"),
        ]
        es = _MatchingES(docs)
        agent = loading_agent(InMemoryDocStore())
        agent._es = es

        orders = await agent._query_fuel_orders(TENANT)

        assert sorted(o["order_id"] for o in orders) == [
            "ord-absent", "ord-empty", "ord-hand-scheduled", "ord-null",
        ]
        assert es.queries[0]["query"]["bool"]["filter"] == [
            _unlinked_clause("assigned_run_id")
        ]

    def test_unlinked_clause_semantics(self):
        clause = _unlinked_clause(COMMITTED_ORDER_FIELD)
        assert matches({}, clause)
        assert matches({"assigned_run_id": None}, clause)
        assert matches({"assigned_run_id": ""}, clause)
        assert not matches({"assigned_run_id": "run-1"}, clause)


# ---------------------------------------------------------------------------
# Hybrid read path
# ---------------------------------------------------------------------------


class _HybridRecorder:
    def __init__(self, items_by_call: List[List[Dict[str, Any]]]) -> None:
        self.items_by_call = list(items_by_call)
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, aggregate_type, tenant_id, **kwargs):
        self.calls.append({"aggregate_type": aggregate_type, "tenant_id": tenant_id, **kwargs})
        items = self.items_by_call.pop(0) if self.items_by_call else []
        return {"items": items, "total": len(items), "page": 1, "size": 500}


class TestHybridPath:
    async def test_query_passes_unlinked_fields(self, monkeypatch):
        import commerce.services.commerce_persistence_bridge as bridge

        recorder = _HybridRecorder([[fuel_order_doc("ord-1")]])
        monkeypatch.setattr(bridge, "read_hybrid_search", recorder)
        agent = loading_agent(InMemoryDocStore())

        orders = await agent._query_fuel_orders(TENANT)

        assert [o["order_id"] for o in orders] == ["ord-1"]
        call = recorder.calls[0]
        assert call["aggregate_type"] == "fuel_order"
        assert call["tenant_id"] == TENANT
        assert call["unlinked_fields"] == ["assigned_run_id"]
        assert sorted(call["in_filters"]["status"]) == ["confirmed", "placed", "scheduled"]

    async def test_committed_probe_uses_exists_and_drops_empty(self, monkeypatch):
        import commerce.services.commerce_persistence_bridge as bridge

        recorder = _HybridRecorder([[
            fuel_order_doc("ord-linked", status="scheduled", assigned_run_id="run-1"),
            # exists_fields keeps "" in SQL; the agent drops it.
            fuel_order_doc("ord-empty", assigned_run_id=""),
        ]])
        monkeypatch.setattr(bridge, "read_hybrid_search", recorder)
        agent = loading_agent(InMemoryDocStore())

        assert await agent._count_committed_loadable_orders(TENANT) == 1
        assert recorder.calls[0]["exists_fields"] == ["assigned_run_id"]
        assert recorder.calls[0]["size"] == 500


# ---------------------------------------------------------------------------
# All committed (finding 9)
# ---------------------------------------------------------------------------


def _committed(order_id: str, **overrides: Any) -> Dict[str, Any]:
    return fuel_order_doc(order_id, status="scheduled", assigned_run_id="run-0",
                          assigned_asset_id="truck-0", assigned_claim_id="att-0",
                          **overrides)


class TestAllCommitted:
    async def test_no_request_no_fallback_and_no_input_degradation(self):
        store = InMemoryDocStore()
        for oid in ("ord-1", "ord-2"):
            store.seed(ORDERS, oid, _committed(oid))
        seed_fleet(store)
        agent = loading_agent(store)
        agent._priority_buffer.append(priority_list_for(["ord-1", "ord-2"]))

        proposals = await agent.evaluate([])

        assert proposals == []
        # The legacy fallback would have built 5 000 L station demands and,
        # with a fleet seeded, persisted plans for them.
        assert store.writes(PLANS) == []
        reasons = _reasons(agent)
        assert [r["reason_code"] for r in reasons] == ["all_loadable_orders_committed"]
        assert reasons[0]["kind"] == DEGRADATION_KIND_NO_INPUT
        assert reasons[0]["committed_orders"] == 2
        assert agent._all_orders_committed is True

    async def test_build_returns_empty_without_legacy_fallback(self):
        store = InMemoryDocStore()
        store.seed(ORDERS, "ord-1", _committed("ord-1"))
        agent = loading_agent(store)

        requests = await agent._build_delivery_requests_from_orders(
            TENANT, priority_list_for(["ord-1"])
        )

        assert requests == []
        assert agent._committed_order_count == 1

    async def test_probe_error_fails_closed_without_flag(self):
        store = InMemoryDocStore()
        store.seed(ORDERS, "ord-1", _committed("ord-1"))
        seed_fleet(store)
        # 1st orders search = the loadable query (empty), 2nd = the probe.
        store.fail_on("search_documents", ORDERS, nth=2)
        agent = loading_agent(store)
        agent._priority_buffer.append(priority_list_for(["ord-1"]))

        proposals = await agent.evaluate([])

        assert proposals == []
        assert store.writes(PLANS) == []
        assert agent._all_orders_committed is False
        assert [r["reason_code"] for r in _reasons(agent)] == ["no_delivery_requests"]

    async def test_no_orders_at_all_keeps_legacy_fallback(self):
        agent = loading_agent(InMemoryDocStore())

        requests = await agent._build_delivery_requests_from_orders(
            TENANT, priority_list_for(["station-1"])
        )

        assert len(requests) == 1 and requests[0].order_id is None
        assert agent._all_orders_committed is False

    async def test_evaluate_resets_the_flag(self):
        store = InMemoryDocStore()
        store.seed(ORDERS, "ord-1", _committed("ord-1"))
        seed_fleet(store)
        agent = loading_agent(store)
        agent._priority_buffer.append(priority_list_for(["ord-1"]))
        await agent.evaluate([])
        assert agent._all_orders_committed is True

        await agent.evaluate([])  # empty buffer

        assert agent._all_orders_committed is False
        assert agent._committed_order_count == 0


# ---------------------------------------------------------------------------
# Committed tank draw (finding 8)
# ---------------------------------------------------------------------------

TANK = "tank-1"
TANK_1000_L = {TANK: (_gal(1000.0), 0.0)}


def _new_order(**overrides: Any) -> Dict[str, Any]:
    overrides.setdefault("customer_tank_id", TANK)
    overrides.setdefault("gallons_requested", _gal(800.0))
    return fuel_order_doc("ord-new", **overrides)


async def _requests(store, tanks=TANK_1000_L, order_ids=("ord-new",)):
    agent = loading_agent(store, tanks=tanks)
    return await agent._build_delivery_requests_from_orders(
        TENANT, priority_list_for(order_ids)
    )


class TestCommittedTankDraw:
    async def test_committed_scheduled_order_reduces_the_cap(self):
        store = InMemoryDocStore()
        store.seed(ORDERS, "ord-new", _new_order())
        store.seed(ORDERS, "ord-old", _committed(
            "ord-old", customer_tank_id=TANK, gallons_requested=_gal(600.0)))

        requests = await _requests(store)

        assert [r.order_id for r in requests] == ["ord-new"]
        assert requests[0].quantity_liters == pytest.approx(400.0, abs=0.02)
        assert requests[0].hard_cap_liters == pytest.approx(400.0, abs=0.02)

    async def test_committed_fill_to_full_skips_the_new_order(self):
        store = InMemoryDocStore()
        store.seed(ORDERS, "ord-new", _new_order())
        store.seed(ORDERS, "ord-old", _committed(
            "ord-old", customer_tank_id=TANK, fill_to_full=True))

        assert await _requests(store) == []

    @pytest.mark.parametrize("status", ["dispatched", "in_transit"])
    async def test_in_flight_order_counts_without_a_run_id(self, status):
        store = InMemoryDocStore()
        store.seed(ORDERS, "ord-new", _new_order())
        store.seed(ORDERS, "ord-truck", fuel_order_doc(
            "ord-truck", status=status, customer_tank_id=TANK,
            gallons_requested=_gal(600.0)))

        requests = await _requests(store)

        assert requests[0].quantity_liters == pytest.approx(400.0, abs=0.02)

    async def test_uncommitted_or_foreign_orders_do_not_draw(self):
        store = InMemoryDocStore()
        store.seed(ORDERS, "ord-new", _new_order())
        # Another tenant's committed order on a tank with the same id.
        store.seed(ORDERS, "ord-foreign", _committed(
            "ord-foreign", tenant_id="tenant-2", customer_tank_id=TANK,
            gallons_requested=_gal(600.0)))
        # A delivered order is history, not a pending draw.
        store.seed(ORDERS, "ord-delivered", fuel_order_doc(
            "ord-delivered", status="delivered", customer_tank_id=TANK,
            gallons_requested=_gal(600.0)))

        requests = await _requests(store)

        assert requests[0].quantity_liters == pytest.approx(800.0, abs=0.02)

    async def test_read_error_skips_known_tank_orders_only(self):
        store = InMemoryDocStore()
        store.seed(ORDERS, "ord-new", _new_order())
        store.seed(ORDERS, "ord-unknown-tank", fuel_order_doc(
            "ord-unknown-tank", customer_tank_id="tank-unknown",
            gallons_requested=_gal(700.0)))
        # 1st orders search = the loadable query, 2nd = the draw read.
        store.fail_on("search_documents", ORDERS, nth=2)

        requests = await _requests(store, order_ids=("ord-new", "ord-unknown-tank"))

        assert [r.order_id for r in requests] == ["ord-unknown-tank"]
        assert requests[0].quantity_liters == pytest.approx(700.0, abs=0.02)

    async def test_no_known_tank_means_no_draw_read(self):
        store = InMemoryDocStore()
        store.seed(ORDERS, "ord-new", _new_order(customer_tank_id="tank-unknown"))

        requests = await _requests(store, tanks={})

        assert requests[0].quantity_liters == pytest.approx(800.0, abs=0.02)
        assert len(store.calls("search_documents", ORDERS)) == 1


# ---------------------------------------------------------------------------
# Snapshots in the proposal (R3.10)
# ---------------------------------------------------------------------------


class TestOrderSnapshots:
    async def test_proposal_carries_snapshots_of_its_orders(self):
        store = InMemoryDocStore()
        store.seed(ORDERS, "ord-1", fuel_order_doc("ord-1", gallons_requested=400.0))
        store.seed(ORDERS, "ord-2", fuel_order_doc(
            "ord-2", product_code="DIESEL_2", gallons_requested=300.0, fill_to_full=False))
        seed_fleet(store)
        agent = loading_agent(store)
        agent._priority_buffer.append(priority_list_for(["ord-1", "ord-2"]))

        proposals = await agent.evaluate([])

        assert len(proposals) == 1
        params = proposals[0].actions[0]["parameters"]
        assert sorted(params["order_ids"]) == ["ord-1", "ord-2"]
        assert params["order_snapshots"] == {
            "ord-1": {"product_code": "DIESEL_2", "customer_tank_id": "tank-ord-1",
                      "gallons_requested": 400.0, "fill_to_full": False},
            "ord-2": {"product_code": "DIESEL_2", "customer_tank_id": "tank-ord-2",
                      "gallons_requested": 300.0, "fill_to_full": False},
        }

    async def test_snapshots_are_reset_per_evaluate(self):
        store = InMemoryDocStore()
        store.seed(ORDERS, "ord-1", fuel_order_doc("ord-1"))
        seed_fleet(store)
        agent = loading_agent(store)
        agent._priority_buffer.append(priority_list_for(["ord-1"]))
        await agent.evaluate([])
        assert set(agent._order_snapshots) == {"ord-1"}

        await agent.evaluate([])  # empty buffer

        assert agent._order_snapshots == {}
