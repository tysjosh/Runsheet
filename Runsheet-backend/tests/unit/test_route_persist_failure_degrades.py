"""A route that could not be stored is not a route the run produced.

``_persist_route_plan`` was fire-and-forget: it caught every exception, logged
``failed to persist route plan``, and returned. The per-truck loop carried on to
build an ``apply_route_plan`` proposal, the agent logged "produced N route
plans", and the pipeline reported the run ``complete``.

That went live. ``mvp_routes`` is ``dynamic: strict`` and did not map
``RoutePlan.window_misses``, so Elasticsearch rejected every route document with
a 400. All four routes in a run were discarded, ``GET /plan/{run_id}`` returned
``route_plan: null``, and four dispatcher approvals were queued against routes
that had never been written — approving one would have applied nothing.

An unstored route cannot be retrieved, dispatched or executed, so the agent now
records a ``route_persist_failed`` skip, emits no proposal for it, and lets the
existing degradation channel carry it to the orchestrator.

Requirements: 4.1 (skip reasons on the run result), 4.7 (route persistence).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.data_contracts import InterventionProposal, RiskClass
from Agents.overlay.route_planning_agent import (
    FUEL_ORDERS_CURRENT_INDEX,
    FUEL_STATIONS_INDEX,
    RoutePlanningAgent,
)
from Agents.support.mvp_es_mappings import MVP_ROUTES_INDEX

TENANT_ID = "tenant-persist-1"


def _make_agent():
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.index_document = AsyncMock()

    async def _search(index_name, query, size):
        if index_name == FUEL_ORDERS_CURRENT_INDEX:
            return {
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "order_id": "ord-1",
                                "tenant_id": TENANT_ID,
                                "status": "confirmed",
                                "ship_to_lat": 32.7767,
                                "ship_to_lon": -96.7970,
                                "ship_to_address": "123 Main St, Dallas, TX",
                                "product_code": "DIESEL_2",
                                "gallons_requested": 500.0,
                            }
                        }
                    ]
                }
            }
        if index_name == FUEL_STATIONS_INDEX:
            return {"hits": {"hits": []}}
        return {"hits": {"hits": []}}

    es_service.search_documents = AsyncMock(side_effect=_search)

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    activity_log.log = AsyncMock()

    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock()

    feature_flags = MagicMock()
    feature_flags.is_enabled = AsyncMock(return_value=True)

    agent = RoutePlanningAgent(
        signal_bus=signal_bus,
        es_service=es_service,
        activity_log_service=activity_log,
        ws_manager=ws_manager,
        confirmation_protocol=confirmation_protocol,
        autonomy_config_service=MagicMock(),
        feature_flag_service=feature_flags,
    )
    return agent, es_service


def _loading_proposal(truck_id: str = "truck-1", plan_id: str = "plan-1"):
    return InterventionProposal(
        source_agent="compartment_loading",
        actions=[
            {
                "tool_name": "apply_loading_plan",
                "parameters": {
                    "plan_id": plan_id,
                    "truck_id": truck_id,
                    "assignments": [
                        {
                            "compartment_id": "comp-0",
                            "station_id": "cust-1",
                            "order_id": "ord-1",
                            "fuel_grade": "DIESEL_2",
                            "quantity_liters": 5000.0,
                            "compartment_capacity_liters": 10000.0,
                        }
                    ],
                    "total_utilization_pct": 50.0,
                    "unserved_demand_liters": 0.0,
                    "total_weight_kg": 4200.0,
                },
            }
        ],
        expected_kpi_delta={"truck_utilization_pct": 50.0},
        risk_class=RiskClass.LOW,
        confidence=0.85,
        priority=1,
        tenant_id=TENANT_ID,
    )


def _reject_route_writes(es_service):
    """Fail exactly the mvp_routes write, the way a strict mapping does."""

    async def _index(index_name, doc_id, doc, *args, **kwargs):
        if index_name == MVP_ROUTES_INDEX:
            raise RuntimeError(
                "strict_dynamic_mapping_exception: dynamic introduction of "
                "[window_misses] within [_doc] is not allowed"
            )
        return {"result": "created"}

    es_service.index_document = AsyncMock(side_effect=_index)


class TestPersistFailureIsReported:
    @pytest.mark.asyncio
    async def test_a_rejected_write_produces_no_proposal(self):
        agent, es_service = _make_agent()
        _reject_route_writes(es_service)
        agent._proposal_buffer.append(_loading_proposal())

        result = await agent.evaluate([])

        assert result == [], (
            "a route nobody can read must not reach the dispatcher as an "
            f"approvable proposal: {result}"
        )

    @pytest.mark.asyncio
    async def test_a_rejected_write_is_recorded_as_a_skip(self):
        agent, es_service = _make_agent()
        _reject_route_writes(es_service)
        agent._proposal_buffer.append(_loading_proposal(truck_id="truck-9"))

        await agent.evaluate([])

        assert [s.reason_code for s in agent.last_route_skips] == [
            "route_persist_failed"
        ]
        skip = agent.last_route_skips[0]
        assert skip.truck_id == "truck-9"
        assert skip.plan_id == "plan-1", (
            "the skip must name the loading plan that was lost, so the "
            "dispatcher can tell which plan has no route"
        )
        assert MVP_ROUTES_INDEX in (skip.detail or "")

    @pytest.mark.asyncio
    async def test_a_rejected_write_degrades_the_run(self):
        agent, es_service = _make_agent()
        _reject_route_writes(es_service)
        agent._proposal_buffer.append(_loading_proposal())

        await agent.evaluate([])

        metrics = agent.cycle_metrics
        assert metrics["degraded"] is True
        assert metrics["trucks_routed"] == 0
        assert metrics["trucks_skipped"] == 1
        assert [s["reason_code"] for s in metrics["route_skips"]] == [
            "route_persist_failed"
        ]


class TestASuccessfulWriteStillProposes:
    """Counterweight: the guard must not swallow the healthy path.

    Without this, returning ``False`` unconditionally from
    ``_persist_route_plan`` would satisfy every assertion above.
    """

    @pytest.mark.asyncio
    async def test_a_written_route_proposes_and_does_not_degrade(self):
        agent, es_service = _make_agent()
        agent._proposal_buffer.append(_loading_proposal())

        result = await agent.evaluate([])

        assert len(result) == 1, f"expected one route proposal, got {result}"
        assert agent.last_route_skips == []
        assert agent.cycle_metrics["degraded"] is False
        assert agent.cycle_metrics["trucks_routed"] == 1

        written = [
            call.args[0]
            for call in es_service.index_document.await_args_list
        ]
        assert MVP_ROUTES_INDEX in written, (
            f"the route was proposed but never written: {written}"
        )
