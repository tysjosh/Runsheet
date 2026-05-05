"""
Unit tests for TruckFuelMonitor autonomous agent.

Tests constructor defaults, severity derivation, MutationRequest creation,
WebSocket broadcasting, RiskSignal publishing, cooldown handling, tenant
feature flag filtering, and ES query failure resilience.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.autonomous.truck_fuel_monitor import TruckFuelMonitor
from Agents.confirmation_protocol import MutationRequest, MutationResult
from Agents.overlay.data_contracts import RiskSignal, Severity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(feature_flags=False):
    """Create mocked dependencies for the agent."""
    es_service = MagicMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id-1")

    ws_manager = MagicMock()
    ws_manager.broadcast_event = AsyncMock()
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock(
        return_value=MutationResult(
            executed=True,
            risk_level="medium",
            result="Successfully executed truck_fuel_alert",
            confirmation_method="immediate",
        )
    )

    feature_flag_service = None
    if feature_flags:
        feature_flag_service = MagicMock()
        feature_flag_service.is_enabled = AsyncMock(return_value=True)

    return es_service, activity_log, ws_manager, confirmation_protocol, feature_flag_service


def _make_agent(signal_bus=None, feature_flags=False, **kwargs):
    """Create a TruckFuelMonitor with mocked dependencies and optional signal_bus."""
    es, al, ws, cp, ffs = _make_deps(feature_flags=feature_flags)
    agent = TruckFuelMonitor(
        es_service=es,
        activity_log_service=al,
        ws_manager=ws,
        confirmation_protocol=cp,
        feature_flag_service=ffs,
        signal_bus=signal_bus,
        **kwargs,
    )
    return agent


def _truck_doc(
    truck_id="TRUCK-001",
    tenant_id="default",
    fuel_level_pct=8.5,
    current_location=None,
):
    """Create a sample truck document."""
    if current_location is None:
        current_location = {"lat": 37.7749, "lng": -122.4194}
    return {
        "truck_id": truck_id,
        "tenant_id": tenant_id,
        "fuel_level_pct": fuel_level_pct,
        "current_location": current_location,
    }


def _es_response(docs):
    """Wrap documents in an ES search response structure."""
    return {
        "hits": {
            "hits": [{"_source": doc} for doc in docs],
            "total": {"value": len(docs)},
        }
    }


# ---------------------------------------------------------------------------
# Tests: Constructor defaults (Req 1.1)
# ---------------------------------------------------------------------------


class TestConstructorDefaults:
    """Tests that TruckFuelMonitor has correct default configuration."""

    def test_agent_id(self):
        """Agent ID is 'truck_fuel_monitor'."""
        agent = _make_agent()
        assert agent.agent_id == "truck_fuel_monitor"

    def test_default_poll_interval(self):
        """Default poll interval is 120 seconds."""
        agent = _make_agent()
        assert agent.poll_interval == 120

    def test_default_cooldown_minutes(self):
        """Default cooldown is 30 minutes."""
        agent = _make_agent()
        assert agent.cooldown_minutes == 30

    def test_default_fuel_threshold_pct(self):
        """Default fuel threshold is 20.0%."""
        agent = _make_agent()
        assert agent._fuel_threshold_pct == 20.0

    def test_signal_bus_stored(self):
        """Signal bus reference is stored when provided."""
        bus = MagicMock()
        agent = _make_agent(signal_bus=bus)
        assert agent._signal_bus is bus

    def test_signal_bus_defaults_to_none(self):
        """Signal bus defaults to None when not provided."""
        agent = _make_agent()
        assert agent._signal_bus is None


# ---------------------------------------------------------------------------
# Tests: _derive_severity (Req 1.5)
# ---------------------------------------------------------------------------


class TestDeriveSeverity:
    """Tests for the _derive_severity static method."""

    def test_below_10_returns_critical(self):
        """Fuel level below 10% returns CRITICAL severity."""
        assert TruckFuelMonitor._derive_severity(5.0) == Severity.CRITICAL

    def test_at_zero_returns_critical(self):
        """Fuel level at 0% returns CRITICAL severity."""
        assert TruckFuelMonitor._derive_severity(0.0) == Severity.CRITICAL

    def test_just_below_10_returns_critical(self):
        """Fuel level at 9.9% returns CRITICAL severity."""
        assert TruckFuelMonitor._derive_severity(9.9) == Severity.CRITICAL

    def test_at_10_returns_high(self):
        """Fuel level at exactly 10% returns HIGH severity (boundary: >= 10)."""
        assert TruckFuelMonitor._derive_severity(10.0) == Severity.HIGH

    def test_at_15_returns_high(self):
        """Fuel level at 15% returns HIGH severity."""
        assert TruckFuelMonitor._derive_severity(15.0) == Severity.HIGH

    def test_at_19_returns_high(self):
        """Fuel level at 19% returns HIGH severity."""
        assert TruckFuelMonitor._derive_severity(19.0) == Severity.HIGH


# ---------------------------------------------------------------------------
# Tests: monitor_cycle creates MutationRequest (Req 1.3)
# ---------------------------------------------------------------------------


class TestMonitorCycleMutationRequest:
    """Tests that monitor_cycle creates MutationRequest with correct parameters."""

    @pytest.mark.asyncio
    async def test_creates_mutation_request_with_truck_id(self):
        """MutationRequest contains the truck_id."""
        agent = _make_agent()
        truck = _truck_doc(truck_id="TRUCK-001")
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        agent._confirmation_protocol.process_mutation.assert_called_once()
        request = agent._confirmation_protocol.process_mutation.call_args[0][0]
        assert isinstance(request, MutationRequest)
        assert request.parameters["truck_id"] == "TRUCK-001"

    @pytest.mark.asyncio
    async def test_creates_mutation_request_with_fuel_level(self):
        """MutationRequest contains the fuel_level_pct."""
        agent = _make_agent()
        truck = _truck_doc(fuel_level_pct=8.5)
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        request = agent._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.parameters["fuel_level_pct"] == 8.5

    @pytest.mark.asyncio
    async def test_creates_mutation_request_with_tenant_id(self):
        """MutationRequest contains the tenant_id."""
        agent = _make_agent()
        truck = _truck_doc(tenant_id="acme-corp")
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        request = agent._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.parameters["tenant_id"] == "acme-corp"

    @pytest.mark.asyncio
    async def test_mutation_request_tool_name(self):
        """MutationRequest has tool_name='truck_fuel_alert'."""
        agent = _make_agent()
        truck = _truck_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        request = agent._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.tool_name == "truck_fuel_alert"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle broadcasts WebSocket event (Req 1.4)
# ---------------------------------------------------------------------------


class TestMonitorCycleWebSocket:
    """Tests that monitor_cycle broadcasts truck_fuel_low WebSocket event."""

    @pytest.mark.asyncio
    async def test_broadcasts_truck_fuel_low_event(self):
        """monitor_cycle broadcasts a 'truck_fuel_low' event."""
        agent = _make_agent()
        truck = _truck_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        agent._ws.broadcast_event.assert_called_once()
        event_name = agent._ws.broadcast_event.call_args[0][0]
        assert event_name == "truck_fuel_low"

    @pytest.mark.asyncio
    async def test_ws_event_contains_truck_id(self):
        """WebSocket event contains truck_id."""
        agent = _make_agent()
        truck = _truck_doc(truck_id="TRUCK-WS-01")
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        event_data = agent._ws.broadcast_event.call_args[0][1]
        assert event_data["truck_id"] == "TRUCK-WS-01"

    @pytest.mark.asyncio
    async def test_ws_event_contains_fuel_level(self):
        """WebSocket event contains fuel_level_pct."""
        agent = _make_agent()
        truck = _truck_doc(fuel_level_pct=12.3)
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        event_data = agent._ws.broadcast_event.call_args[0][1]
        assert event_data["fuel_level_pct"] == 12.3

    @pytest.mark.asyncio
    async def test_ws_event_contains_current_location(self):
        """WebSocket event contains current_location."""
        agent = _make_agent()
        loc = {"lat": 40.7128, "lng": -74.0060}
        truck = _truck_doc(current_location=loc)
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        event_data = agent._ws.broadcast_event.call_args[0][1]
        assert event_data["current_location"] == loc

    @pytest.mark.asyncio
    async def test_ws_event_contains_tenant_id(self):
        """WebSocket event contains tenant_id."""
        agent = _make_agent()
        truck = _truck_doc(tenant_id="tenant-xyz")
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        event_data = agent._ws.broadcast_event.call_args[0][1]
        assert event_data["tenant_id"] == "tenant-xyz"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle publishes RiskSignal (Req 1.5)
# ---------------------------------------------------------------------------


class TestMonitorCycleRiskSignal:
    """Tests that monitor_cycle publishes RiskSignal with correct fields."""

    @pytest.mark.asyncio
    async def test_publishes_risk_signal(self):
        """monitor_cycle publishes a RiskSignal when signal_bus is provided."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        truck = _truck_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        bus.publish.assert_called_once()
        signal = bus.publish.call_args[0][0]
        assert isinstance(signal, RiskSignal)

    @pytest.mark.asyncio
    async def test_risk_signal_entity_type_is_truck(self):
        """Published RiskSignal has entity_type='truck'."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        truck = _truck_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.entity_type == "truck"

    @pytest.mark.asyncio
    async def test_risk_signal_ttl_seconds_is_600(self):
        """Published RiskSignal has ttl_seconds=600."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        truck = _truck_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.ttl_seconds == 600

    @pytest.mark.asyncio
    async def test_risk_signal_source_agent(self):
        """Published RiskSignal has source_agent='truck_fuel_monitor'."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        truck = _truck_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.source_agent == "truck_fuel_monitor"

    @pytest.mark.asyncio
    async def test_risk_signal_severity_critical_below_10(self):
        """Published RiskSignal has severity CRITICAL for fuel < 10%."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        truck = _truck_doc(fuel_level_pct=5.0)
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.severity == Severity.CRITICAL

    @pytest.mark.asyncio
    async def test_risk_signal_severity_high_above_10(self):
        """Published RiskSignal has severity HIGH for fuel >= 10% and < 20%."""
        bus = MagicMock()
        bus.publish = AsyncMock()
        agent = _make_agent(signal_bus=bus)
        truck = _truck_doc(fuel_level_pct=15.0)
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        await agent.monitor_cycle()

        signal = bus.publish.call_args[0][0]
        assert signal.severity == Severity.HIGH

    @pytest.mark.asyncio
    async def test_no_signal_when_bus_is_none(self):
        """monitor_cycle does not crash when signal_bus is None."""
        agent = _make_agent(signal_bus=None)
        truck = _truck_doc()
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        detections, actions = await agent.monitor_cycle()

        assert "TRUCK-001" in detections
        assert len(actions) == 1


# ---------------------------------------------------------------------------
# Tests: monitor_cycle skips trucks on cooldown (Req 1.6)
# ---------------------------------------------------------------------------


class TestMonitorCycleCooldown:
    """Tests that monitor_cycle skips trucks that are on cooldown."""

    @pytest.mark.asyncio
    async def test_skips_truck_on_cooldown(self):
        """Second cycle for the same truck is skipped due to cooldown."""
        agent = _make_agent()
        truck = _truck_doc(truck_id="TRUCK-COOL")
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        # First cycle — should process
        await agent.monitor_cycle()
        assert agent._confirmation_protocol.process_mutation.call_count == 1

        # Second cycle — same truck should be skipped
        agent._confirmation_protocol.process_mutation.reset_mock()
        await agent.monitor_cycle()
        assert agent._confirmation_protocol.process_mutation.call_count == 0

    @pytest.mark.asyncio
    async def test_cooldown_truck_still_detected(self):
        """Truck on cooldown is still in detections but no action is taken."""
        agent = _make_agent()
        truck = _truck_doc(truck_id="TRUCK-COOL")
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        # First cycle
        await agent.monitor_cycle()

        # Second cycle
        detections, actions = await agent.monitor_cycle()
        assert "TRUCK-COOL" in detections
        assert len(actions) == 0


# ---------------------------------------------------------------------------
# Tests: monitor_cycle skips disabled tenants (Req 1.7)
# ---------------------------------------------------------------------------


class TestMonitorCycleDisabledTenants:
    """Tests that monitor_cycle skips trucks belonging to disabled tenants."""

    @pytest.mark.asyncio
    async def test_skips_disabled_tenant(self):
        """Trucks from disabled tenants are not processed."""
        agent = _make_agent(feature_flags=True)
        agent._feature_flags.is_enabled = AsyncMock(return_value=False)
        truck = _truck_doc(tenant_id="disabled-tenant")
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        detections, actions = await agent.monitor_cycle()

        # Truck is not added to detections when tenant is disabled
        assert len(actions) == 0
        agent._confirmation_protocol.process_mutation.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_enabled_tenant(self):
        """Trucks from enabled tenants are processed normally."""
        agent = _make_agent(feature_flags=True)
        agent._feature_flags.is_enabled = AsyncMock(return_value=True)
        truck = _truck_doc(tenant_id="enabled-tenant")
        agent._es.search_documents = AsyncMock(return_value=_es_response([truck]))

        detections, actions = await agent.monitor_cycle()

        assert len(actions) == 1
        agent._confirmation_protocol.process_mutation.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: monitor_cycle handles ES query failure (Req 1.8)
# ---------------------------------------------------------------------------


class TestMonitorCycleESFailure:
    """Tests that monitor_cycle handles ES query failure gracefully."""

    @pytest.mark.asyncio
    async def test_returns_empty_on_es_failure(self):
        """monitor_cycle returns empty detections and actions on ES failure."""
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(
            side_effect=Exception("ES connection refused")
        )

        detections, actions = await agent.monitor_cycle()

        assert detections == []
        assert actions == []

    @pytest.mark.asyncio
    async def test_does_not_crash_on_es_failure(self):
        """monitor_cycle does not raise when ES query fails."""
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(
            side_effect=RuntimeError("ES timeout")
        )

        # Should not raise
        detections, actions = await agent.monitor_cycle()
        assert isinstance(detections, list)
        assert isinstance(actions, list)
