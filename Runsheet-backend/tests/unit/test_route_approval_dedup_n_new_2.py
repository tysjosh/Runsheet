"""One ``apply_route_plan`` approval per (tenant, loading plan, truck) (N-new-2).

Staging queued two route approvals for every loading plan. The cause is double
delivery: ``PipelinePublishCapture`` still forwards each publish to the real
``SignalBus.publish``, so the subscribed ``RoutePlanningAgent._on_signal``
buffers the compartment_loading proposal, and then
``FuelDistributionPipeline._capture_and_inject`` appends the same proposal
again. Each copy was routed with a fresh ``route_id``, written to
``mvp_routes`` and queued for approval.

Two guards, each tested here:

* ``RoutePlanningAgent.evaluate`` de-duplicates its drained buffer on
  ``(tenant_id, plan_id)`` (one solver run, one ``mvp_routes`` write).
* ``ApprovalQueueService.create`` is idempotent for the route tools: the
  action id is a uuid5 of ``route|tenant|plan_id|truck_id``, written
  create-if-absent, and an existing entry in any status is never reset.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents import approval_queue_service as aqs
from Agents.approval_queue_service import ApprovalQueueService
from Agents.confirmation_protocol import MutationRequest
from Agents.overlay import route_planning_agent as rpa
from Agents.overlay.data_contracts import InterventionProposal, RiskClass
from Agents.overlay.route_planning_agent import (
    FUEL_ORDERS_CURRENT_INDEX,
    RoutePlanningAgent,
)
from Agents.overlay.signal_bus import SignalBus
from Agents.risk_registry import RiskLevel
from Agents.support.fuel_distribution_pipeline import (
    FuelDistributionPipeline,
    PipelinePublishCapture,
)
from tests.unit._loading_plan_fakes import (
    APPROVALS,
    FakeActivityLog,
    InMemoryDocumentStore,
    SpyAgentWS,
)

TENANT = "tenant-n-new-2"


# ---------------------------------------------------------------------------
# ApprovalQueueService.create idempotency
# ---------------------------------------------------------------------------


def _service():
    store = InMemoryDocumentStore()
    ws = SpyAgentWS()
    svc = ApprovalQueueService(
        es_service=store, ws_manager=ws, activity_log_service=FakeActivityLog()
    )
    return svc, store, ws


def _route_request(
    *, route_id: str = "r1", plan_id: str = "P1", truck_id: str = "T1",
    tool: str = "apply_route_plan", tenant: str = TENANT,
) -> MutationRequest:
    return MutationRequest(
        tool_name=tool,
        parameters={
            "route_id": route_id,
            "truck_id": truck_id,
            "plan_id": plan_id,
            "stops": [{"station_id": "s-1", "sequence": 1}],
        },
        tenant_id=tenant,
        agent_id="route_planning",
    )


class TestRouteApprovalIsIdempotent:
    async def test_same_plan_and_truck_queue_one_entry(self):
        svc, store, ws = _service()

        first = await svc.create(_route_request(route_id="r1"), RiskLevel.MEDIUM)
        second = await svc.create(_route_request(route_id="r2"), RiskLevel.MEDIUM)

        assert first == second
        assert list(store.docs[APPROVALS]) == [first]
        entry = store.doc(APPROVALS, first)
        assert entry["status"] == "pending"
        assert entry["parameters"]["route_id"] == "r1", "the first proposal wins"
        assert len(ws.of("approval_created")) == 1

    async def test_storm_tool_shares_the_key(self):
        svc, store, _ws = _service()

        first = await svc.create(_route_request(), RiskLevel.MEDIUM)
        storm = await svc.create(
            _route_request(route_id="r9", tool="apply_route_plan_storm_mode"),
            RiskLevel.HIGH,
        )

        assert storm == first
        assert len(store.docs[APPROVALS]) == 1

    async def test_a_different_plan_or_tenant_gets_its_own_entry(self):
        svc, store, ws = _service()

        first = await svc.create(_route_request(plan_id="P1"), RiskLevel.MEDIUM)
        other_plan = await svc.create(_route_request(plan_id="P2"), RiskLevel.MEDIUM)
        other_truck = await svc.create(_route_request(truck_id="T2"), RiskLevel.MEDIUM)
        other_tenant = await svc.create(_route_request(tenant="tenant-x"), RiskLevel.MEDIUM)

        assert len({first, other_plan, other_truck, other_tenant}) == 4
        assert len(store.docs[APPROVALS]) == 4
        assert len(ws.of("approval_created")) == 4

    async def test_recreate_never_resets_a_decided_entry(self):
        svc, store, ws = _service()
        action_id = await svc.create(_route_request(), RiskLevel.MEDIUM)
        await svc.reject(action_id, "user-1", "not today", tenant_id=TENANT)

        again = await svc.create(_route_request(route_id="r3"), RiskLevel.MEDIUM)

        assert again == action_id
        entry = store.doc(APPROVALS, action_id)
        assert entry["status"] == "rejected"
        assert entry["parameters"]["route_id"] == "r1"
        assert len(ws.of("approval_created")) == 1

    async def test_other_tools_keep_one_entry_per_call(self):
        svc, store, ws = _service()
        request = MutationRequest(
            tool_name="request_fuel_refill",
            parameters={"station_id": "s-1", "plan_id": "P1", "truck_id": "T1"},
            tenant_id=TENANT,
            agent_id="fuel_management",
        )

        first = await svc.create(request, RiskLevel.MEDIUM)
        second = await svc.create(request, RiskLevel.MEDIUM)

        assert first != second
        assert len(store.docs[APPROVALS]) == 2
        assert len(ws.of("approval_created")) == 2

    async def test_route_without_plan_or_truck_is_not_deduplicated(self):
        svc, store, _ws = _service()

        first = await svc.create(_route_request(plan_id=""), RiskLevel.MEDIUM)
        second = await svc.create(_route_request(plan_id=""), RiskLevel.MEDIUM)

        assert first != second
        assert len(store.docs[APPROVALS]) == 2

    def test_route_tool_names_match_the_agent(self):
        assert aqs.ROUTE_APPROVAL_TOOLS == frozenset(
            {rpa.APPLY_ROUTE_PLAN_TOOL, rpa.APPLY_ROUTE_PLAN_STORM_MODE_TOOL}
        )


# ---------------------------------------------------------------------------
# Root cause: the pipeline delivers each loading proposal twice
# ---------------------------------------------------------------------------


def _route_agent(bus: SignalBus):
    """A real RoutePlanningAgent over faked collaborators (wiring-test deps)."""
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
                                "tenant_id": TENANT,
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
        return {"hits": {"hits": []}}

    es_service.search_documents = AsyncMock(side_effect=_search)

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    activity_log.log = AsyncMock()
    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()
    ws_manager.broadcast_event = AsyncMock(return_value=0)
    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock()
    feature_flags = MagicMock()
    feature_flags.is_enabled = AsyncMock(return_value=True)

    agent = RoutePlanningAgent(
        signal_bus=bus,
        es_service=es_service,
        activity_log_service=activity_log,
        ws_manager=ws_manager,
        confirmation_protocol=confirmation_protocol,
        autonomy_config_service=MagicMock(),
        feature_flag_service=feature_flags,
    )
    agent._find_available_asset = AsyncMock(return_value=True)
    agent._check_hos_eligibility = AsyncMock(return_value=True)
    agent._persist_route_plan = AsyncMock(return_value=True)
    # Read the order through the faked ES search even when a local env
    # enables the Postgres read cutover.
    async def _orders(tenant_id):
        resp = await _search(FUEL_ORDERS_CURRENT_INDEX, {}, 1000)
        return [hit["_source"] for hit in resp["hits"]["hits"]]

    agent._fetch_routable_orders = _orders
    return agent


def _loading_proposal(plan_id: str = "plan-1", truck_id: str = "truck-1") -> InterventionProposal:
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
        tenant_id=TENANT,
    )


async def _subscribe(agent: RoutePlanningAgent, bus: SignalBus) -> None:
    """The same subscribe calls ``OverlayAgentBase.start`` makes (no loop task)."""
    for spec in agent._subscription_specs:
        await bus.subscribe(
            subscriber_id=agent.agent_id,
            message_type=spec["message_type"],
            callback=agent._on_signal,
            filters=spec.get("filters"),
        )


async def _deliver_through_pipeline(agent, bus, proposal) -> None:
    async with PipelinePublishCapture(bus) as capture:
        await bus.publish(proposal)
    pipeline = FuelDistributionPipeline(
        agents={"route_planning": agent}, ws_manager=MagicMock(), signal_bus=bus
    )
    await pipeline._capture_and_inject(
        "compartment_loading", capture.captured, agent, TENANT
    )


class TestDoubleDeliveryRoutesOnce:
    async def test_pipeline_delivers_the_loading_proposal_twice(self):
        bus = SignalBus(es_service=AsyncMock())
        agent = _route_agent(bus)
        await _subscribe(agent, bus)
        proposal = _loading_proposal()

        await _deliver_through_pipeline(agent, bus, proposal)

        # The repro: subscription + injection buffer the same object.
        assert [p is proposal for p in agent._proposal_buffer] == [True, True]
        await bus.unsubscribe(agent.agent_id)

    async def test_a_duplicated_proposal_produces_one_route(self):
        bus = SignalBus(es_service=AsyncMock())
        agent = _route_agent(bus)
        await _subscribe(agent, bus)
        await _deliver_through_pipeline(agent, bus, _loading_proposal())

        routes = await agent.evaluate([])

        assert len(routes) == 1, routes
        assert agent._persist_route_plan.await_count == 1
        assert agent.cycle_metrics["loading_plans_considered"] == 1
        await bus.unsubscribe(agent.agent_id)

    async def test_distinct_plans_are_all_routed(self):
        bus = SignalBus(es_service=AsyncMock())
        agent = _route_agent(bus)
        agent._proposal_buffer.extend(
            [_loading_proposal("plan-1", "truck-1"), _loading_proposal("plan-2", "truck-2")]
        )

        routes = await agent.evaluate([])

        assert len(routes) == 2
        assert agent._persist_route_plan.await_count == 2

    def test_dedupe_keeps_first_and_passes_planless_proposals(self):
        first = _loading_proposal("plan-1")
        dup = _loading_proposal("plan-1")
        other_tenant = _loading_proposal("plan-1").model_copy(update={"tenant_id": "t-2"})
        planless = InterventionProposal(
            source_agent="compartment_loading",
            actions=[],
            expected_kpi_delta={},
            risk_class=RiskClass.LOW,
            confidence=0.5,
            priority=1,
            tenant_id=TENANT,
        )

        kept: List[Any] = RoutePlanningAgent._dedupe_loading_proposals(
            [first, dup, other_tenant, planless, planless]
        )

        assert kept[0] is first
        assert kept[1] is other_tenant
        assert kept[2:] == [planless, planless]
        assert len(kept) == 4
