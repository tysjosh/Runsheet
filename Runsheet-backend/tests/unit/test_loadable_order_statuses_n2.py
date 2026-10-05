"""
N2 regression: the route planner reads the same order statuses the loader loads.

``CompartmentLoadingAgent`` and ``DeliveryPrioritizationAgent`` read orders in
``{placed, confirmed, scheduled}``; ``RoutePlanningAgent._fetch_routable_orders``
read only ``{confirmed, scheduled}``. A loading plan built from a ``placed``
order then had no coordinates in the route planner and was skipped as
``unresolvable_stop_locations`` (staging run ``run_20261004_183801_7a36e3b8``).

These tests drive ``RoutePlanningAgent.evaluate`` with fakes that honour the
status filter (the existing ``_es_dispatcher`` ignores filters, which is how the
mismatch stayed hidden), on both the document-store and the Postgres
(``read_hybrid_search``) read paths, and pin that all three agents pass one
shared status list.
"""
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

import commerce.services.commerce_persistence_bridge as bridge
from Agents.overlay.base_overlay_agent import CYCLE_METRIC_DEGRADATION_REASONS
from Agents.overlay.compartment_loading_agent import CompartmentLoadingAgent
from Agents.overlay.delivery_prioritization_agent import (
    DeliveryPrioritizationAgent,
)
from Agents.overlay.route_planning_agent import (
    FUEL_ORDERS_CURRENT_INDEX,
    RoutePlanningAgent,
)
from persistence.document_matcher import matches
from tests.unit.test_route_planning_agent_order_stops import (
    TENANT_ID,
    _make_agent,
    _make_deps,
    _make_loading_proposal,
    _order,
)

#: The loader's set. Spelled out (not imported) so these tests collect and fail
#: meaningfully on code that predates the shared constant.
EXPECTED_STATUSES = ["placed", "confirmed", "scheduled"]


def _query_honouring_search(orders: List[Dict[str, Any]]):
    """A ``search_documents`` fake that applies the query to the order docs."""

    async def _search(index_name, query, size=None):
        if index_name != FUEL_ORDERS_CURRENT_INDEX:
            return {"hits": {"hits": []}}
        hits = [
            {"_source": o} for o in orders if matches(o, query.get("query"))
        ]
        return {"hits": {"hits": hits}}

    return _search


def _placed_order() -> Dict[str, Any]:
    return _order("ord-1", status="placed", lat=29.78, lon=-95.35)


class TestRoutePlannerRoutesPlacedOrders:
    @pytest.mark.asyncio
    async def test_document_store_path_routes_placed_order(self):
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_query_honouring_search([_placed_order()])
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                stops=[{"station_id": "cust-1", "order_id": "ord-1"}]
            )
        )

        result = await agent.evaluate([])

        assert len(result) == 1, agent.last_route_skips
        stops = result[0].actions[0]["parameters"]["stops"]
        assert [s["station_id"] for s in stops] == ["cust-1"]
        assert agent.last_route_skips == []

    @pytest.mark.asyncio
    async def test_postgres_path_routes_placed_order(self, monkeypatch):
        orders = [_placed_order()]
        seen_in_filters: List[Dict[str, Any]] = []

        async def _fake_read_hybrid_search(
            aggregate_type, tenant_id, *, in_filters=None, **kwargs
        ):
            seen_in_filters.append(dict(in_filters or {}))
            wanted = set((in_filters or {}).get("status") or [])
            return {
                "items": [
                    o for o in orders
                    if o["tenant_id"] == tenant_id and o["status"] in wanted
                ]
            }

        monkeypatch.setattr(
            bridge, "read_hybrid_search", _fake_read_hybrid_search
        )
        agent, deps = _make_agent()
        # The document store has nothing: only the Postgres path can resolve it.
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                stops=[{"station_id": "cust-1", "order_id": "ord-1"}]
            )
        )

        result = await agent.evaluate([])

        assert len(result) == 1, agent.last_route_skips
        assert agent.last_route_skips == []
        assert seen_in_filters
        assert all(f["status"] == EXPECTED_STATUSES for f in seen_in_filters)


class TestOneStatusSetAcrossAgents:
    """Drift guard: loading, prioritization and routing read one status list."""

    @pytest.mark.asyncio
    async def test_all_three_agents_pass_the_same_status_list(
        self, monkeypatch
    ):
        pg_statuses: List[List[str]] = []
        es_statuses: List[List[str]] = []

        async def _cut_over_search(aggregate_type, *args, in_filters=None, **kw):
            pg_statuses.append(list((in_filters or {}).get("status") or []))
            return bridge._NOT_CUT_OVER

        monkeypatch.setattr(bridge, "read_hybrid_search", _cut_over_search)
        monkeypatch.setattr(
            bridge, "read_hybrid_search_all_tenants", _cut_over_search
        )

        def _collect_terms(node: Any) -> None:
            if isinstance(node, dict):
                terms = node.get("terms")
                if isinstance(terms, dict) and "status" in terms:
                    es_statuses.append(list(terms["status"]))
                for value in node.values():
                    _collect_terms(value)
            elif isinstance(node, list):
                for value in node:
                    _collect_terms(value)

        async def _es_search(index_name, query, size=None):
            _collect_terms(query)
            return {"hits": {"hits": []}}

        agents = []
        for cls in (
            RoutePlanningAgent,
            CompartmentLoadingAgent,
            DeliveryPrioritizationAgent,
        ):
            deps = _make_deps()
            deps["es_service"].search_documents = AsyncMock(
                side_effect=_es_search
            )
            agents.append(cls(**deps))
        route, loading, prioritization = agents

        await route._fetch_routable_orders(TENANT_ID)
        await loading._query_fuel_orders(TENANT_ID)
        await prioritization._fetch_pending_orders(TENANT_ID)
        await prioritization._discover_tenants_with_pending_orders()

        from fuel.order_models import LOADABLE_ORDER_STATUSES

        expected = list(LOADABLE_ORDER_STATUSES)
        assert expected == EXPECTED_STATUSES
        assert len(pg_statuses) == 4
        assert len(es_statuses) == 4
        assert all(s == expected for s in pg_statuses), pg_statuses
        assert all(s == expected for s in es_statuses), es_statuses


class TestDegradationReasonsAreNotDoubled:
    """Plan item 5 runtime check: one unresolvable plan -> one reason entry."""

    @pytest.mark.asyncio
    async def test_one_unresolvable_plan_records_one_reason(self):
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_query_honouring_search([])
        )
        agent._proposal_buffer.append(
            _make_loading_proposal(
                truck_id="truck-1",
                plan_id="plan-1",
                stops=[
                    {"station_id": "cust-1", "order_id": "ord-1"},
                    {"station_id": "cust-2", "order_id": "ord-2"},
                ],
            )
        )

        assert await agent.evaluate([]) == []

        reasons = agent.cycle_metrics[CYCLE_METRIC_DEGRADATION_REASONS]
        assert [
            (r["reason_code"], r["truck_id"], r["plan_id"]) for r in reasons
        ] == [("unresolvable_stop_locations", "truck-1", "plan-1")]

    @pytest.mark.asyncio
    async def test_two_unresolvable_plans_record_one_reason_each(self):
        """The staging duplicate is explained by two loading plans."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=_query_honouring_search([])
        )
        for truck_id, plan_id in (("truck-1", "plan-1"), ("truck-2", "plan-2")):
            agent._proposal_buffer.append(
                _make_loading_proposal(
                    truck_id=truck_id,
                    plan_id=plan_id,
                    stops=[{"station_id": "cust-1", "order_id": "ord-1"}],
                )
            )

        assert await agent.evaluate([]) == []

        reasons = agent.cycle_metrics[CYCLE_METRIC_DEGRADATION_REASONS]
        assert sorted(
            (r["reason_code"], r["truck_id"], r["plan_id"]) for r in reasons
        ) == [
            ("unresolvable_stop_locations", "truck-1", "plan-1"),
            ("unresolvable_stop_locations", "truck-2", "plan-2"),
        ]
