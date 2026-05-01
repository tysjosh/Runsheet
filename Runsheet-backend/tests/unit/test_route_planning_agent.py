"""
Unit tests for the RoutePlanningAgent overlay agent.

Tests cover:
- Constructor and agent_id configuration
- Signal subscription setup (InterventionProposal from compartment_loading)
- _on_signal() buffering of loading proposals
- evaluate() with empty proposal buffer
- evaluate() extracts loading plan from proposal (Req 4.1)
- evaluate() queries station locations (Req 4.3)
- evaluate() runs route optimization (Req 4.5)
- evaluate() computes objective value (Req 4.6)
- evaluate() persists route plans to mvp_routes (Req 4.7)
- evaluate() produces InterventionProposals with route plan actions
- _extract_loading_plan() from proposal actions
- _compute_objective_value() weighted scoring
- _build_location_list() with depot

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.route_planning_agent import (
    DEFAULT_DEPOT,
    DEFAULT_OBJECTIVE_WEIGHTS,
    RoutePlanningAgent,
)
from Agents.support.fuel_distribution_models import RoutePlan, RouteStop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_loading_proposal(
    truck_id="truck-1",
    plan_id="plan-1",
    tenant_id="tenant-1",
    station_ids=None,
):
    if station_ids is None:
        station_ids = ["station-1", "station-2"]

    assignments = [
        {
            "compartment_id": f"comp-{i}",
            "station_id": sid,
            "fuel_grade": "AGO",
            "quantity_liters": 5000.0,
            "compartment_capacity_liters": 10000.0,
        }
        for i, sid in enumerate(station_ids)
    ]

    return InterventionProposal(
        source_agent="compartment_loading",
        actions=[
            {
                "tool_name": "apply_loading_plan",
                "parameters": {
                    "plan_id": plan_id,
                    "truck_id": truck_id,
                    "assignments": assignments,
                    "total_utilization_pct": 75.0,
                    "unserved_demand_liters": 0.0,
                    "total_weight_kg": 8500.0,
                },
            }
        ],
        expected_kpi_delta={"truck_utilization_pct": 75.0},
        risk_class=RiskClass.LOW,
        confidence=0.85,
        priority=1,
        tenant_id=tenant_id,
    )


def _make_station_location_hit(station_id, lat, lon):
    return {
        "_source": {
            "station_id": station_id,
            "latitude": lat,
            "longitude": lon,
        }
    }


def _make_deps():
    """Create mocked dependencies for the RoutePlanningAgent."""
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": []}}
    )
    es_service.index_document = AsyncMock()

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    activity_log.log = AsyncMock()

    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock()

    autonomy_config = MagicMock()
    feature_flags = MagicMock()
    feature_flags.is_enabled = AsyncMock(return_value=True)

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": autonomy_config,
        "feature_flag_service": feature_flags,
    }


def _make_agent(**overrides):
    deps = _make_deps()
    deps.update(overrides)
    return RoutePlanningAgent(**deps), deps


# ---------------------------------------------------------------------------
# Tests: Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_agent_id(self):
        agent, _ = _make_agent()
        assert agent.agent_id == "route_planning"

    def test_subscription_to_intervention_proposals(self):
        agent, _ = _make_agent()
        assert len(agent._subscription_specs) == 1
        spec = agent._subscription_specs[0]
        assert spec["message_type"] is InterventionProposal
        assert spec["filters"]["source_agent"] == "compartment_loading"

    def test_default_poll_interval(self):
        agent, _ = _make_agent()
        assert agent.poll_interval == 60

    def test_proposal_buffer_initially_empty(self):
        agent, _ = _make_agent()
        assert agent._proposal_buffer == []


# ---------------------------------------------------------------------------
# Tests: _on_signal() — InterventionProposal buffering
# ---------------------------------------------------------------------------


class TestOnSignal:
    @pytest.mark.asyncio
    async def test_buffers_loading_proposal(self):
        agent, _ = _make_agent()
        proposal = _make_loading_proposal()
        await agent._on_signal(proposal)
        assert len(agent._proposal_buffer) == 1
        assert agent._proposal_buffer[0] is proposal

    @pytest.mark.asyncio
    async def test_ignores_non_loading_proposals(self):
        """Proposals from other agents should not be buffered."""
        agent, _ = _make_agent()
        proposal = InterventionProposal(
            source_agent="other_agent",
            actions=[],
            expected_kpi_delta={},
            risk_class=RiskClass.LOW,
            confidence=0.5,
            priority=1,
            tenant_id="tenant-1",
        )
        await agent._on_signal(proposal)
        assert len(agent._proposal_buffer) == 0

    @pytest.mark.asyncio
    async def test_non_proposal_goes_to_parent(self):
        """Non-InterventionProposal signals go to the parent signal buffer."""
        agent, _ = _make_agent()
        signal = RiskSignal(
            source_agent="test",
            entity_id="e1",
            entity_type="test",
            severity=Severity.LOW,
            confidence=0.5,
            ttl_seconds=300,
            tenant_id="tenant-1",
        )
        await agent._on_signal(signal)
        assert len(agent._proposal_buffer) == 0


# ---------------------------------------------------------------------------
# Tests: evaluate()
# ---------------------------------------------------------------------------


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_empty_buffer_returns_empty(self):
        agent, _ = _make_agent()
        result = await agent.evaluate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_produces_route_proposals(self):
        """Req 4.1: Produces route plan proposals from loading plans."""
        agent, deps = _make_agent()
        agent._proposal_buffer.append(
            _make_loading_proposal(station_ids=["station-1", "station-2"])
        )

        # Return station locations
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        _make_station_location_hit("station-1", 6.45, 3.40),
                        _make_station_location_hit("station-2", 6.50, 3.35),
                    ]
                }
            }
        )

        result = await agent.evaluate([])
        assert len(result) == 1
        assert isinstance(result[0], InterventionProposal)
        assert result[0].source_agent == "route_planning"

    @pytest.mark.asyncio
    async def test_persists_route_plan_to_es(self):
        """Req 4.7: Route plans persisted to mvp_routes."""
        agent, deps = _make_agent()
        agent._proposal_buffer.append(
            _make_loading_proposal(station_ids=["station-1"])
        )

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        _make_station_location_hit("station-1", 6.45, 3.40),
                    ]
                }
            }
        )

        await agent.evaluate([])

        assert deps["es_service"].index_document.call_count >= 1
        call_args = deps["es_service"].index_document.call_args
        assert call_args[0][0] == "mvp_routes"

    @pytest.mark.asyncio
    async def test_proposal_contains_route_plan_action(self):
        """Proposal actions should contain apply_route_plan tool."""
        agent, deps = _make_agent()
        agent._proposal_buffer.append(
            _make_loading_proposal(station_ids=["station-1"])
        )

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        _make_station_location_hit("station-1", 6.45, 3.40),
                    ]
                }
            }
        )

        result = await agent.evaluate([])
        assert len(result) == 1
        actions = result[0].actions
        assert len(actions) == 1
        assert actions[0]["tool_name"] == "apply_route_plan"
        assert "route_id" in actions[0]["parameters"]
        assert "distance_km" in actions[0]["parameters"]

    @pytest.mark.asyncio
    async def test_no_station_locations_returns_empty(self):
        """When no station locations are found, no route is produced."""
        agent, deps = _make_agent()
        agent._proposal_buffer.append(
            _make_loading_proposal(station_ids=["station-1"])
        )

        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        result = await agent.evaluate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_clears_buffer_after_evaluate(self):
        """Proposal buffer should be cleared after evaluation."""
        agent, deps = _make_agent()
        agent._proposal_buffer.append(_make_loading_proposal())

        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        await agent.evaluate([])
        assert len(agent._proposal_buffer) == 0


# ---------------------------------------------------------------------------
# Tests: _extract_loading_plan()
# ---------------------------------------------------------------------------


class TestExtractLoadingPlan:
    def test_extracts_from_valid_proposal(self):
        agent, _ = _make_agent()
        proposal = _make_loading_proposal(truck_id="truck-1", plan_id="plan-1")
        plan = agent._extract_loading_plan(proposal)
        assert plan is not None
        assert plan["truck_id"] == "truck-1"
        assert plan["plan_id"] == "plan-1"

    def test_returns_none_for_no_loading_action(self):
        agent, _ = _make_agent()
        proposal = InterventionProposal(
            source_agent="compartment_loading",
            actions=[{"tool_name": "other_tool", "parameters": {}}],
            expected_kpi_delta={},
            risk_class=RiskClass.LOW,
            confidence=0.5,
            priority=1,
            tenant_id="tenant-1",
        )
        plan = agent._extract_loading_plan(proposal)
        assert plan is None


# ---------------------------------------------------------------------------
# Tests: _compute_objective_value() (Req 4.6)
# ---------------------------------------------------------------------------


class TestComputeObjectiveValue:
    def test_objective_bounded_0_to_1(self):
        agent, _ = _make_agent()
        route_plan = RoutePlan(
            truck_id="truck-1",
            plan_id="plan-1",
            stops=[
                RouteStop(station_id="s1", eta="2024-01-01T10:00:00Z", drop={"AGO": 5000}, sequence=0),
            ],
            distance_km=100.0,
            eta_confidence=0.8,
            tenant_id="tenant-1",
        )
        value = agent._compute_objective_value(route_plan, utilization_pct=75.0)
        assert 0.0 <= value <= 1.0

    def test_short_route_higher_objective(self):
        """Shorter routes should have higher objective values."""
        agent, _ = _make_agent()
        stops = [
            RouteStop(station_id="s1", eta="2024-01-01T10:00:00Z", drop={"AGO": 5000}, sequence=0),
        ]
        short_route = RoutePlan(
            truck_id="t1", plan_id="p1", stops=stops,
            distance_km=50.0, eta_confidence=0.8, tenant_id="t1",
        )
        long_route = RoutePlan(
            truck_id="t1", plan_id="p1", stops=stops,
            distance_km=400.0, eta_confidence=0.8, tenant_id="t1",
        )
        short_value = agent._compute_objective_value(short_route, 75.0)
        long_value = agent._compute_objective_value(long_route, 75.0)
        assert short_value > long_value

    def test_higher_utilization_higher_objective(self):
        """Higher truck utilization should increase objective value."""
        agent, _ = _make_agent()
        route_plan = RoutePlan(
            truck_id="t1", plan_id="p1",
            stops=[RouteStop(station_id="s1", eta="2024-01-01T10:00:00Z", drop={}, sequence=0)],
            distance_km=100.0, eta_confidence=0.8, tenant_id="t1",
        )
        high_util = agent._compute_objective_value(route_plan, utilization_pct=90.0)
        low_util = agent._compute_objective_value(route_plan, utilization_pct=30.0)
        assert high_util > low_util


# ---------------------------------------------------------------------------
# Tests: _build_location_list()
# ---------------------------------------------------------------------------


class TestBuildLocationList:
    def test_depot_at_index_zero(self):
        agent, _ = _make_agent()
        locations, station_order = agent._build_location_list(
            station_ids=["s1"],
            station_locations={"s1": {"lat": 6.5, "lon": 3.4}},
        )
        assert locations[0] == DEFAULT_DEPOT
        assert len(locations) == 2
        assert station_order == ["s1"]

    def test_skips_stations_without_locations(self):
        agent, _ = _make_agent()
        locations, station_order = agent._build_location_list(
            station_ids=["s1", "s2"],
            station_locations={"s1": {"lat": 6.5, "lon": 3.4}},
        )
        assert len(locations) == 2  # depot + s1
        assert station_order == ["s1"]

    def test_empty_stations(self):
        agent, _ = _make_agent()
        locations, station_order = agent._build_location_list(
            station_ids=[],
            station_locations={},
        )
        assert len(locations) == 1  # depot only
        assert station_order == []
