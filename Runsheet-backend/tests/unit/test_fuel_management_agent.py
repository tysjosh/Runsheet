"""
Unit tests for the Fuel Management Agent.

Tests the FuelManagementAgent autonomous agent including monitor_cycle,
cooldown enforcement, refill quantity/priority calculations, Confirmation
Protocol integration, WebSocket fuel_alert broadcasts, and edge cases.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from Agents.autonomous.fuel_management_agent import (
    FuelManagementAgent,
    FUEL_STATIONS_INDEX,
    DEFAULT_DAYS_THRESHOLD,
)
from Agents.confirmation_protocol import MutationRequest, MutationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps():
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
            result="Successfully executed request_fuel_refill",
            confirmation_method="immediate",
        )
    )

    return es_service, activity_log, ws_manager, confirmation_protocol


def _make_agent(
    poll_interval=300,
    cooldown_minutes=120,
    days_threshold=DEFAULT_DAYS_THRESHOLD,
):
    """Create a FuelManagementAgent with mocked dependencies."""
    es, al, ws, cp = _make_deps()
    agent = FuelManagementAgent(
        es_service=es,
        activity_log_service=al,
        ws_manager=ws,
        confirmation_protocol=cp,
        poll_interval=poll_interval,
        cooldown_minutes=cooldown_minutes,
        days_threshold=days_threshold,
    )
    return agent


def _fuel_station(
    station_id="STN-001",
    tenant_id="default",
    status="critical",
    capacity_liters=10000,
    current_stock_liters=1000,
    days_until_empty=2.0,
    name="Station Alpha",
    fuel_type="AGO",
):
    """Create a sample fuel station document."""
    return {
        "station_id": station_id,
        "tenant_id": tenant_id,
        "status": status,
        "capacity_liters": capacity_liters,
        "current_stock_liters": current_stock_liters,
        "days_until_empty": days_until_empty,
        "name": name,
        "fuel_type": fuel_type,
        "daily_consumption_rate": 500,
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
# Tests: __init__
# ---------------------------------------------------------------------------


class TestInit:
    """Tests for agent initialisation."""

    def test_agent_id(self):
        agent = _make_agent()
        assert agent.agent_id == "fuel_management_agent"

    def test_default_poll_interval(self):
        es, al, ws, cp = _make_deps()
        agent = FuelManagementAgent(
            es_service=es,
            activity_log_service=al,
            ws_manager=ws,
            confirmation_protocol=cp,
        )
        assert agent.poll_interval == 300

    def test_default_cooldown(self):
        es, al, ws, cp = _make_deps()
        agent = FuelManagementAgent(
            es_service=es,
            activity_log_service=al,
            ws_manager=ws,
            confirmation_protocol=cp,
        )
        assert agent.cooldown_minutes == 120

    def test_custom_poll_interval(self):
        agent = _make_agent(poll_interval=600)
        assert agent.poll_interval == 600

    def test_custom_cooldown(self):
        agent = _make_agent(cooldown_minutes=60)
        assert agent.cooldown_minutes == 60

    def test_custom_days_threshold(self):
        agent = _make_agent(days_threshold=3)
        assert agent._days_threshold == 3

    def test_default_days_threshold(self):
        agent = _make_agent()
        assert agent._days_threshold == DEFAULT_DAYS_THRESHOLD

    def test_stores_es_service(self):
        agent = _make_agent()
        assert agent._es is not None

    def test_feature_flags_optional(self):
        es, al, ws, cp = _make_deps()
        agent = FuelManagementAgent(
            es_service=es,
            activity_log_service=al,
            ws_manager=ws,
            confirmation_protocol=cp,
        )
        assert agent._feature_flags is None


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — no flagged stations
# ---------------------------------------------------------------------------


class TestMonitorCycleNoStations:
    """Tests for monitor_cycle when no stations are flagged."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_flagged_stations(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        detections, actions = await agent.monitor_cycle()
        assert detections == []
        assert actions == []

    @pytest.mark.asyncio
    async def test_queries_fuel_stations_index(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent.monitor_cycle()

        call_args = agent._es.search_documents.call_args
        assert call_args[0][0] == FUEL_STATIONS_INDEX

    @pytest.mark.asyncio
    async def test_query_includes_critical_status_filter(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent.monitor_cycle()

        call_args = agent._es.search_documents.call_args
        query = call_args[0][1]
        should_clauses = query["query"]["bool"]["should"]
        status_clause = next(
            c for c in should_clauses if "term" in c and "status" in c["term"]
        )
        assert status_clause["term"]["status"] == "critical"

    @pytest.mark.asyncio
    async def test_query_includes_days_until_empty_filter(self):
        agent = _make_agent(days_threshold=5)
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent.monitor_cycle()

        call_args = agent._es.search_documents.call_args
        query = call_args[0][1]
        should_clauses = query["query"]["bool"]["should"]
        range_clause = next(c for c in should_clauses if "range" in c)
        assert range_clause["range"]["days_until_empty"]["lt"] == 5


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — with flagged stations
# ---------------------------------------------------------------------------


class TestMonitorCycleWithRefill:
    """Tests for monitor_cycle when flagged stations are found."""

    @pytest.mark.asyncio
    async def test_detects_critical_station(self):
        agent = _make_agent()
        station = _fuel_station()

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        assert "STN-001" in detections

    @pytest.mark.asyncio
    async def test_creates_refill_request(self):
        agent = _make_agent()
        station = _fuel_station()

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        assert len(actions) == 1
        assert actions[0]["action"] == "refill_request"
        assert actions[0]["station_id"] == "STN-001"

    @pytest.mark.asyncio
    async def test_calculates_correct_refill_quantity(self):
        agent = _make_agent()
        # capacity=10000, stock=1000 → refill = 0.8*10000 - 1000 = 7000
        station = _fuel_station(capacity_liters=10000, current_stock_liters=1000)

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        assert actions[0]["quantity_liters"] == 7000.0

    @pytest.mark.asyncio
    async def test_calculates_correct_priority_critical(self):
        agent = _make_agent()
        station = _fuel_station(days_until_empty=0.5)

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        assert actions[0]["priority"] == "critical"

    @pytest.mark.asyncio
    async def test_calculates_correct_priority_high(self):
        agent = _make_agent()
        station = _fuel_station(days_until_empty=2.0)

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        assert actions[0]["priority"] == "high"

    @pytest.mark.asyncio
    async def test_calculates_correct_priority_medium(self):
        agent = _make_agent()
        station = _fuel_station(days_until_empty=4.0)

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        assert actions[0]["priority"] == "medium"

    @pytest.mark.asyncio
    async def test_calls_confirmation_protocol(self):
        agent = _make_agent()
        station = _fuel_station()

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        await agent.monitor_cycle()

        agent._confirmation_protocol.process_mutation.assert_called_once()
        call_args = agent._confirmation_protocol.process_mutation.call_args
        request = call_args[0][0]
        assert isinstance(request, MutationRequest)
        assert request.tool_name == "request_fuel_refill"
        assert request.parameters["station_id"] == "STN-001"
        assert request.parameters["quantity_liters"] == 7000.0
        assert request.parameters["priority"] == "high"
        assert request.agent_id == "fuel_management_agent"

    @pytest.mark.asyncio
    async def test_broadcasts_fuel_alert(self):
        agent = _make_agent()
        station = _fuel_station()

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        await agent.monitor_cycle()

        agent._ws.broadcast_event.assert_called_once()
        call_args = agent._ws.broadcast_event.call_args
        assert call_args[0][0] == "fuel_alert"
        payload = call_args[0][1]
        assert payload["station_id"] == "STN-001"
        assert payload["station_name"] == "Station Alpha"
        assert payload["fuel_type"] == "AGO"
        assert payload["current_stock_liters"] == 1000
        assert payload["capacity_liters"] == 10000
        assert payload["days_until_empty"] == 2.0
        assert payload["refill_quantity"] == 7000.0
        assert payload["priority"] == "high"
        assert payload["tenant_id"] == "default"

    @pytest.mark.asyncio
    async def test_sets_cooldown_after_refill_request(self):
        agent = _make_agent()
        station = _fuel_station()

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        await agent.monitor_cycle()
        assert agent._is_on_cooldown("STN-001") is True

    @pytest.mark.asyncio
    async def test_multiple_flagged_stations(self):
        agent = _make_agent()
        station1 = _fuel_station(station_id="STN-001")
        station2 = _fuel_station(station_id="STN-002", days_until_empty=0.5)

        agent._es.search_documents = AsyncMock(
            return_value=_es_response([station1, station2])
        )

        detections, actions = await agent.monitor_cycle()
        assert len(detections) == 2
        assert len(actions) == 2
        assert actions[0]["station_id"] == "STN-001"
        assert actions[1]["station_id"] == "STN-002"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — cooldown enforcement
# ---------------------------------------------------------------------------


class TestMonitorCycleCooldown:
    """Tests for cooldown enforcement in monitor_cycle."""

    @pytest.mark.asyncio
    async def test_skips_station_on_cooldown(self):
        agent = _make_agent()
        station = _fuel_station()

        # Pre-set cooldown for the station
        agent._set_cooldown("STN-001")

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        # Station is detected but no action taken
        assert "STN-001" in detections
        assert len(actions) == 0

    @pytest.mark.asyncio
    async def test_does_not_call_protocol_for_cooldown_station(self):
        agent = _make_agent()
        station = _fuel_station()

        agent._set_cooldown("STN-001")

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        await agent.monitor_cycle()
        agent._confirmation_protocol.process_mutation.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_broadcast_for_cooldown_station(self):
        agent = _make_agent()
        station = _fuel_station()

        agent._set_cooldown("STN-001")

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        await agent.monitor_cycle()
        agent._ws.broadcast_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_station_after_cooldown_expires(self):
        agent = _make_agent(cooldown_minutes=120)
        station = _fuel_station()

        # Set cooldown in the past (expired)
        agent._cooldown_tracker["STN-001"] = datetime.now(timezone.utc) - timedelta(
            minutes=130
        )

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        assert len(actions) == 1
        assert actions[0]["action"] == "refill_request"

    @pytest.mark.asyncio
    async def test_mixed_cooldown_and_fresh_stations(self):
        agent = _make_agent()
        station_cooldown = _fuel_station(station_id="STN-001")
        station_fresh = _fuel_station(station_id="STN-002")

        agent._set_cooldown("STN-001")

        agent._es.search_documents = AsyncMock(
            return_value=_es_response([station_cooldown, station_fresh])
        )

        detections, actions = await agent.monitor_cycle()
        assert len(detections) == 2
        assert len(actions) == 1
        assert actions[0]["station_id"] == "STN-002"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — tenant_id handling
# ---------------------------------------------------------------------------


class TestMonitorCycleTenantId:
    """Tests for tenant_id handling in monitor_cycle."""

    @pytest.mark.asyncio
    async def test_uses_station_tenant_id(self):
        agent = _make_agent()
        station = _fuel_station(tenant_id="tenant-42")

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        await agent.monitor_cycle()

        request = agent._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.tenant_id == "tenant-42"

    @pytest.mark.asyncio
    async def test_defaults_to_default_tenant(self):
        agent = _make_agent()
        station = _fuel_station()
        del station["tenant_id"]  # Remove tenant_id to test default

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        await agent.monitor_cycle()

        request = agent._confirmation_protocol.process_mutation.call_args[0][0]
        assert request.tenant_id == "default"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — edge cases
# ---------------------------------------------------------------------------


class TestMonitorCycleEdgeCases:
    """Tests for edge cases in monitor_cycle."""

    @pytest.mark.asyncio
    async def test_skips_station_already_above_target(self):
        """Station at 90% capacity should produce zero refill quantity and be skipped."""
        agent = _make_agent()
        # capacity=10000, stock=9000 → refill = 0.8*10000 - 9000 = -1000 → 0
        station = _fuel_station(
            capacity_liters=10000,
            current_stock_liters=9000,
            status="critical",  # Still flagged by status
        )

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        assert "STN-001" in detections
        assert len(actions) == 0
        # Should not call protocol or broadcast
        agent._confirmation_protocol.process_mutation.assert_not_called()
        agent._ws.broadcast_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_station_at_exactly_target(self):
        """Station at exactly 80% capacity should produce zero refill and be skipped."""
        agent = _make_agent()
        # capacity=10000, stock=8000 → refill = 0.8*10000 - 8000 = 0
        station = _fuel_station(
            capacity_liters=10000,
            current_stock_liters=8000,
            status="critical",
        )

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        assert len(actions) == 0

    @pytest.mark.asyncio
    async def test_station_with_zero_capacity(self):
        """Station with zero capacity should produce zero refill and be skipped."""
        agent = _make_agent()
        station = _fuel_station(capacity_liters=0, current_stock_liters=0)

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        assert len(actions) == 0

    @pytest.mark.asyncio
    async def test_fuel_alert_includes_urgency_fields(self):
        """Verify the fuel_alert payload includes all required urgency fields."""
        agent = _make_agent()
        station = _fuel_station(
            station_id="STN-URGENT",
            days_until_empty=0.3,
            current_stock_liters=200,
            capacity_liters=5000,
            name="Urgent Station",
            fuel_type="PMS",
            status="critical",
        )

        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        await agent.monitor_cycle()

        payload = agent._ws.broadcast_event.call_args[0][1]
        assert payload["station_id"] == "STN-URGENT"
        assert payload["priority"] == "critical"
        assert payload["status"] == "critical"
        assert payload["refill_quantity"] == 3800.0  # 0.8*5000 - 200

    @pytest.mark.asyncio
    async def test_confirmation_result_stored_in_action(self):
        """Verify the MutationResult is stored in the action dict."""
        agent = _make_agent()
        station = _fuel_station()

        expected_result = MutationResult(
            executed=False,
            approval_id="approval-123",
            risk_level="medium",
            confirmation_method="approval_queue",
        )
        agent._confirmation_protocol.process_mutation = AsyncMock(
            return_value=expected_result
        )
        agent._es.search_documents = AsyncMock(return_value=_es_response([station]))

        detections, actions = await agent.monitor_cycle()
        assert actions[0]["result"] is expected_result
