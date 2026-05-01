"""
Unit tests for the ExceptionReplanningAgent overlay agent.

Tests cover:
- Constructor and agent_id configuration
- Signal subscription setup (RiskSignals from disruption sources)
- evaluate() with empty signals
- evaluate() detects disruption types (Req 5.1)
- evaluate() loads plan snapshot from ES (Req 5.2)
- _handle_truck_breakdown() — truck swap (Req 5.3)
- _handle_station_outage() — station removal (Req 5.4)
- _handle_demand_spike() — volume reallocation (Req 5.5)
- _handle_delay() — stop reorder (Req 5.2)
- Escalation when no feasible replan (Req 5.6)
- Persistence to mvp_replan_events (Req 5.7)
- Risk classification: MEDIUM default, HIGH for truck swaps (Req 5.8)

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8
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
from Agents.overlay.exception_replanning_agent import (
    DISRUPTION_SOURCE_AGENTS,
    ExceptionReplanningAgent,
)
from Agents.support.fuel_distribution_models import ReplanDiff, ReplanEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(
    source_agent="delay_response_agent",
    entity_id="truck-1",
    entity_type="truck",
    severity=Severity.HIGH,
    confidence=0.8,
    tenant_id="tenant-1",
    context=None,
):
    return RiskSignal(
        source_agent=source_agent,
        entity_id=entity_id,
        entity_type=entity_type,
        severity=severity,
        confidence=confidence,
        ttl_seconds=300,
        tenant_id=tenant_id,
        context=context or {},
    )


def _make_plan_snapshot(
    plan_id="plan-1",
    truck_id="truck-1",
    station_ids=None,
):
    if station_ids is None:
        station_ids = ["station-1", "station-2", "station-3"]

    loading_plan = {
        "plan_id": plan_id,
        "truck_id": truck_id,
        "assignments": [
            {
                "compartment_id": f"comp-{i}",
                "station_id": sid,
                "fuel_grade": "AGO",
                "quantity_liters": 5000.0,
            }
            for i, sid in enumerate(station_ids)
        ],
        "status": "proposed",
        "tenant_id": "tenant-1",
    }

    route_plan = {
        "route_id": "route-1",
        "truck_id": truck_id,
        "plan_id": plan_id,
        "stops": [
            {"station_id": sid, "eta": "2024-01-01T10:00:00Z", "drop": {"AGO": 5000}, "sequence": i}
            for i, sid in enumerate(station_ids)
        ],
        "distance_km": 150.0,
        "status": "proposed",
        "tenant_id": "tenant-1",
    }

    return {"loading_plan": loading_plan, "route_plan": route_plan}


def _make_deps():
    """Create mocked dependencies for the ExceptionReplanningAgent."""
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
    return ExceptionReplanningAgent(**deps), deps


def _setup_plan_snapshot_in_es(deps, plan_snapshot):
    """Configure ES mock to return a plan snapshot."""
    loading_plan = plan_snapshot["loading_plan"]
    route_plan = plan_snapshot["route_plan"]

    deps["es_service"].search_documents = AsyncMock(
        side_effect=[
            # First call: loading plan query
            {"hits": {"hits": [{"_source": loading_plan}]}},
            # Second call: route plan query
            {"hits": {"hits": [{"_source": route_plan}]}},
        ]
    )


# ---------------------------------------------------------------------------
# Tests: Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_agent_id(self):
        agent, _ = _make_agent()
        assert agent.agent_id == "exception_replanning"

    def test_subscription_to_risk_signals(self):
        agent, _ = _make_agent()
        assert len(agent._subscription_specs) == 1
        spec = agent._subscription_specs[0]
        assert spec["message_type"] is RiskSignal
        # Filters should include disruption source agents
        source_filter = spec["filters"]["source_agent"]
        assert set(source_filter) == DISRUPTION_SOURCE_AGENTS

    def test_default_poll_interval(self):
        """Req 11.6: 30-second continuous decision cycle."""
        agent, _ = _make_agent()
        assert agent.poll_interval == 30

    def test_default_cooldown(self):
        agent, _ = _make_agent()
        assert agent.cooldown_minutes == 5


# ---------------------------------------------------------------------------
# Tests: evaluate()
# ---------------------------------------------------------------------------


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_empty_signals_returns_empty(self):
        agent, _ = _make_agent()
        result = await agent.evaluate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_no_active_plan_returns_empty(self):
        """When no active plan exists, evaluate returns empty."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        signal = _make_signal()
        result = await agent.evaluate([signal])
        assert result == []

    @pytest.mark.asyncio
    async def test_produces_replan_proposal_for_delay(self):
        """Req 5.2: Produces replan proposal for delay disruption."""
        agent, deps = _make_agent()
        plan_snapshot = _make_plan_snapshot()
        _setup_plan_snapshot_in_es(deps, plan_snapshot)

        signal = _make_signal(
            source_agent="delay_response_agent",
            entity_id="station-1",
            context={"disruption_type": "delay"},
        )
        result = await agent.evaluate([signal])
        assert len(result) == 1
        assert isinstance(result[0], InterventionProposal)
        assert result[0].source_agent == "exception_replanning"

    @pytest.mark.asyncio
    async def test_persists_replan_event_to_es(self):
        """Req 5.7: Replan events persisted to mvp_replan_events."""
        agent, deps = _make_agent()
        plan_snapshot = _make_plan_snapshot()
        _setup_plan_snapshot_in_es(deps, plan_snapshot)

        signal = _make_signal(
            source_agent="delay_response_agent",
            entity_id="station-1",
            context={"disruption_type": "delay"},
        )
        await agent.evaluate([signal])

        # index_document should be called for the replan event
        assert deps["es_service"].index_document.call_count >= 1
        call_args = deps["es_service"].index_document.call_args
        assert call_args[0][0] == "mvp_replan_events"

    @pytest.mark.asyncio
    async def test_proposal_contains_replan_action(self):
        """Proposal actions should contain apply_replan tool."""
        agent, deps = _make_agent()
        plan_snapshot = _make_plan_snapshot()
        _setup_plan_snapshot_in_es(deps, plan_snapshot)

        signal = _make_signal(
            source_agent="delay_response_agent",
            entity_id="station-1",
            context={"disruption_type": "delay"},
        )
        result = await agent.evaluate([signal])
        assert len(result) == 1
        actions = result[0].actions
        assert len(actions) == 1
        assert actions[0]["tool_name"] == "apply_replan"
        assert "event_id" in actions[0]["parameters"]
        assert "diff" in actions[0]["parameters"]


# ---------------------------------------------------------------------------
# Tests: _detect_disruption_type() (Req 5.1)
# ---------------------------------------------------------------------------


class TestDetectDisruptionType:
    def test_explicit_disruption_type_in_context(self):
        agent, _ = _make_agent()
        signal = _make_signal(context={"disruption_type": "truck_breakdown"})
        assert agent._detect_disruption_type(signal) == "truck_breakdown"

    def test_station_outage_from_context(self):
        agent, _ = _make_agent()
        signal = _make_signal(context={"disruption_type": "station_outage"})
        assert agent._detect_disruption_type(signal) == "station_outage"

    def test_demand_spike_from_context(self):
        agent, _ = _make_agent()
        signal = _make_signal(context={"disruption_type": "demand_spike"})
        assert agent._detect_disruption_type(signal) == "demand_spike"

    def test_delay_from_source_agent(self):
        agent, _ = _make_agent()
        signal = _make_signal(source_agent="delay_response_agent")
        assert agent._detect_disruption_type(signal) == "delay"

    def test_sla_breach_defaults_to_delay(self):
        agent, _ = _make_agent()
        signal = _make_signal(source_agent="sla_guardian_agent")
        assert agent._detect_disruption_type(signal) == "delay"

    def test_keyword_match_in_context(self):
        agent, _ = _make_agent()
        signal = _make_signal(
            source_agent="exception_commander",
            context={"reason": "vehicle_failure detected"},
        )
        assert agent._detect_disruption_type(signal) == "truck_breakdown"

    def test_unknown_defaults_to_delay(self):
        agent, _ = _make_agent()
        signal = _make_signal(
            source_agent="exception_commander",
            entity_type="unknown",
            context={},
        )
        assert agent._detect_disruption_type(signal) == "delay"


# ---------------------------------------------------------------------------
# Tests: Disruption handlers (Req 5.3–5.5)
# ---------------------------------------------------------------------------


class TestHandleTruckBreakdown:
    def test_returns_diff_with_truck_swap(self):
        """Req 5.3: Truck breakdown produces truck swap diff."""
        agent, _ = _make_agent()
        signal = _make_signal(entity_id="truck-1")
        plan_snapshot = _make_plan_snapshot(truck_id="truck-1")

        result = agent._handle_truck_breakdown(signal, plan_snapshot)
        assert result is not None
        diff, patched_plan_id, risk_class = result
        assert diff.truck_swapped == "truck-1"
        assert risk_class == RiskClass.HIGH  # Req 5.8

    def test_returns_none_when_truck_not_in_plan(self):
        """No replan needed if broken truck is not in current plan."""
        agent, _ = _make_agent()
        signal = _make_signal(entity_id="truck-99")
        plan_snapshot = _make_plan_snapshot(truck_id="truck-1")

        result = agent._handle_truck_breakdown(signal, plan_snapshot)
        assert result is None


class TestHandleStationOutage:
    def test_returns_diff_with_deferred_station(self):
        """Req 5.4: Station outage defers station and reorders stops."""
        agent, _ = _make_agent()
        signal = _make_signal(entity_id="station-2")
        plan_snapshot = _make_plan_snapshot(
            station_ids=["station-1", "station-2", "station-3"]
        )

        result = agent._handle_station_outage(signal, plan_snapshot)
        assert result is not None
        diff, _, risk_class = result
        assert "station-2" in diff.stations_deferred
        assert "station-2" not in diff.stops_reordered
        assert risk_class == RiskClass.MEDIUM

    def test_returns_none_when_station_not_in_route(self):
        agent, _ = _make_agent()
        signal = _make_signal(entity_id="station-99")
        plan_snapshot = _make_plan_snapshot()

        result = agent._handle_station_outage(signal, plan_snapshot)
        assert result is None

    def test_returns_none_when_no_route_plan(self):
        agent, _ = _make_agent()
        signal = _make_signal(entity_id="station-1")
        plan_snapshot = {"loading_plan": {"plan_id": "p1"}, "route_plan": None}

        result = agent._handle_station_outage(signal, plan_snapshot)
        assert result is None


class TestHandleDemandSpike:
    def test_returns_diff_with_volume_reallocation(self):
        """Req 5.5: Demand spike reallocates volume."""
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="station-1",
            context={"additional_liters": 2000.0},
        )
        plan_snapshot = _make_plan_snapshot()

        result = agent._handle_demand_spike(signal, plan_snapshot)
        assert result is not None
        diff, _, risk_class = result
        assert "station-1" in diff.volumes_reallocated
        assert diff.volumes_reallocated["station-1"] == 2000.0
        assert risk_class == RiskClass.MEDIUM

    def test_returns_none_when_station_not_in_plan(self):
        agent, _ = _make_agent()
        signal = _make_signal(entity_id="station-99")
        plan_snapshot = _make_plan_snapshot()

        result = agent._handle_demand_spike(signal, plan_snapshot)
        assert result is None


class TestHandleDelay:
    def test_returns_diff_with_reordered_stops(self):
        """Req 5.2: Delay reorders stops."""
        agent, _ = _make_agent()
        signal = _make_signal(entity_id="station-1")
        plan_snapshot = _make_plan_snapshot(
            station_ids=["station-1", "station-2", "station-3"]
        )

        result = agent._handle_delay(signal, plan_snapshot)
        assert result is not None
        diff, _, risk_class = result
        # Delayed station should be moved to end
        assert diff.stops_reordered[-1] == "station-1"
        assert risk_class == RiskClass.MEDIUM

    def test_returns_none_when_no_route(self):
        agent, _ = _make_agent()
        signal = _make_signal(entity_id="station-1")
        plan_snapshot = {"loading_plan": {"plan_id": "p1"}, "route_plan": None}

        result = agent._handle_delay(signal, plan_snapshot)
        assert result is None

    def test_returns_none_when_single_stop(self):
        """Cannot reorder with only one stop."""
        agent, _ = _make_agent()
        signal = _make_signal(entity_id="station-1")
        plan_snapshot = _make_plan_snapshot(station_ids=["station-1"])

        result = agent._handle_delay(signal, plan_snapshot)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: Escalation (Req 5.6)
# ---------------------------------------------------------------------------


class TestEscalation:
    @pytest.mark.asyncio
    async def test_escalates_when_no_feasible_replan(self):
        """Req 5.6: Publishes HIGH-severity RiskSignal when no replan possible."""
        agent, deps = _make_agent()

        # Setup: plan exists but truck breakdown for a truck not in plan
        plan_snapshot = _make_plan_snapshot(truck_id="truck-1")
        _setup_plan_snapshot_in_es(deps, plan_snapshot)

        signal = _make_signal(
            entity_id="truck-99",  # Not in plan
            context={"disruption_type": "truck_breakdown"},
        )
        result = await agent.evaluate([signal])
        assert result == []

        # Should have published an escalation RiskSignal
        publish_calls = deps["signal_bus"].publish.call_args_list
        assert len(publish_calls) >= 1
        escalation = publish_calls[0][0][0]
        assert isinstance(escalation, RiskSignal)
        assert escalation.severity == Severity.HIGH
        assert escalation.source_agent == "exception_replanning"
        assert escalation.context.get("escalation_required") is True

    @pytest.mark.asyncio
    async def test_persists_escalated_replan_event(self):
        """Req 5.7: Escalated replan events are persisted with status 'escalated'."""
        agent, deps = _make_agent()
        plan_snapshot = _make_plan_snapshot(truck_id="truck-1")
        _setup_plan_snapshot_in_es(deps, plan_snapshot)

        signal = _make_signal(
            entity_id="truck-99",
            context={"disruption_type": "truck_breakdown"},
        )
        await agent.evaluate([signal])

        # Check that the persisted event has status "escalated"
        index_calls = deps["es_service"].index_document.call_args_list
        assert len(index_calls) >= 1
        persisted_doc = index_calls[0][0][2]
        assert persisted_doc["status"] == "escalated"


# ---------------------------------------------------------------------------
# Tests: Risk classification (Req 5.8)
# ---------------------------------------------------------------------------


class TestRiskClassification:
    @pytest.mark.asyncio
    async def test_truck_swap_is_high_risk(self):
        """Req 5.8: Truck swaps classified as HIGH risk."""
        agent, deps = _make_agent()
        plan_snapshot = _make_plan_snapshot(truck_id="truck-1")
        _setup_plan_snapshot_in_es(deps, plan_snapshot)

        signal = _make_signal(
            entity_id="truck-1",
            context={"disruption_type": "truck_breakdown"},
        )
        result = await agent.evaluate([signal])
        assert len(result) == 1
        assert result[0].risk_class == RiskClass.HIGH

    @pytest.mark.asyncio
    async def test_delay_is_medium_risk(self):
        """Req 5.8: Delays classified as MEDIUM risk."""
        agent, deps = _make_agent()
        plan_snapshot = _make_plan_snapshot()
        _setup_plan_snapshot_in_es(deps, plan_snapshot)

        signal = _make_signal(
            entity_id="station-1",
            context={"disruption_type": "delay"},
        )
        result = await agent.evaluate([signal])
        assert len(result) == 1
        assert result[0].risk_class == RiskClass.MEDIUM

    @pytest.mark.asyncio
    async def test_station_outage_is_medium_risk(self):
        """Req 5.8: Station outages classified as MEDIUM risk."""
        agent, deps = _make_agent()
        plan_snapshot = _make_plan_snapshot()
        _setup_plan_snapshot_in_es(deps, plan_snapshot)

        signal = _make_signal(
            entity_id="station-1",
            context={"disruption_type": "station_outage"},
        )
        result = await agent.evaluate([signal])
        assert len(result) == 1
        assert result[0].risk_class == RiskClass.MEDIUM
