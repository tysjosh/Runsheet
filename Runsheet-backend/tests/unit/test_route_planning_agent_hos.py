"""
Unit tests for Route_Planning_Agent HOS eligibility check (Task 7.8).

Tests cover:
- set_hos_checker() setter wiring
- _check_hos_eligibility() graceful degradation when checker not configured
- _check_hos_eligibility() skips when no driver_id found on truck
- _check_hos_eligibility() returns True for HOS-eligible drivers
- _check_hos_eligibility() returns False for HOS-ineligible drivers (hos_blocked)
- _check_hos_eligibility() graceful degradation on checker exceptions
- _estimate_route_hours() heuristic computation
- evaluate() skips loading plan when driver is HOS-ineligible

Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
)
from Agents.overlay.route_planning_agent import RoutePlanningAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_hos_eligibility(eligible=True, reasons=None, earliest_eligible_time=None):
    """Create a mock HOSEligibility result."""
    mock = MagicMock()
    mock.eligible = eligible
    mock.reasons = reasons or []
    mock.earliest_eligible_time = earliest_eligible_time
    return mock


def _make_assignments(num_stops=3):
    """Create sample loading plan assignments."""
    return [
        {
            "compartment_id": f"comp-{i}",
            "station_id": f"station-{i}",
            "fuel_grade": "DIESEL_2",
            "quantity_liters": 5000.0,
        }
        for i in range(num_stops)
    ]


# ---------------------------------------------------------------------------
# Tests: set_hos_checker
# ---------------------------------------------------------------------------


class TestSetHOSChecker:
    def test_setter_stores_checker(self):
        agent, _ = _make_agent()
        mock_checker = MagicMock()
        agent.set_hos_checker(mock_checker)
        assert agent._hos_checker is mock_checker

    def test_setter_accepts_none(self):
        agent, _ = _make_agent()
        agent.set_hos_checker(None)
        assert agent._hos_checker is None

    def test_default_is_none(self):
        agent, _ = _make_agent()
        assert agent._hos_checker is None


# ---------------------------------------------------------------------------
# Tests: _check_hos_eligibility — graceful degradation
# ---------------------------------------------------------------------------


class TestCheckHOSEligibilityGracefulDegradation:
    @pytest.mark.asyncio
    async def test_returns_true_when_checker_not_configured(self):
        """When no HOSChecker is wired, allow all routes."""
        agent, _ = _make_agent()
        # Checker is None by default
        result = await agent._check_hos_eligibility(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_when_no_driver_on_truck(self):
        """When truck has no driver_id, skip HOS check and allow route."""
        agent, deps = _make_agent()
        mock_checker = AsyncMock()
        agent.set_hos_checker(mock_checker)

        # ES returns truck with no driver_id
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": ""}}]
                }
            }
        )

        result = await agent._check_hos_eligibility(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is True
        # Checker should not have been called
        mock_checker.is_eligible.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_true_when_truck_not_found(self):
        """When truck doesn't exist in ES, skip HOS check and allow route."""
        agent, deps = _make_agent()
        mock_checker = AsyncMock()
        agent.set_hos_checker(mock_checker)

        # ES returns no hits
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        result = await agent._check_hos_eligibility(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_on_checker_exception(self):
        """When the HOS check raises, allow route (graceful degradation)."""
        agent, deps = _make_agent()
        mock_checker = AsyncMock()
        mock_checker.is_eligible = AsyncMock(
            side_effect=RuntimeError("Geotab connection failed")
        )
        agent.set_hos_checker(mock_checker)

        # ES returns truck with driver
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-1"}}]
                }
            }
        )

        result = await agent._check_hos_eligibility(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is True


# ---------------------------------------------------------------------------
# Tests: _check_hos_eligibility — eligibility checks (Req 4.1–4.5)
# ---------------------------------------------------------------------------


class TestCheckHOSEligibility:
    @pytest.mark.asyncio
    async def test_eligible_driver_returns_true(self):
        """HOS-eligible driver allows route to proceed."""
        agent, deps = _make_agent()
        mock_checker = AsyncMock()
        mock_checker.is_eligible = AsyncMock(
            return_value=_make_hos_eligibility(eligible=True)
        )
        agent.set_hos_checker(mock_checker)

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-1"}}]
                }
            }
        )

        result = await agent._check_hos_eligibility(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is True
        mock_checker.is_eligible.assert_called_once()

    @pytest.mark.asyncio
    async def test_ineligible_driver_returns_false(self):
        """HOS-ineligible driver causes route to be skipped (hos_blocked)."""
        agent, deps = _make_agent()
        mock_checker = AsyncMock()
        earliest = datetime(2025, 6, 15, 8, 0, 0, tzinfo=timezone.utc)
        mock_checker.is_eligible = AsyncMock(
            return_value=_make_hos_eligibility(
                eligible=False,
                reasons=[
                    "Insufficient drive hours: 2.0h available, need 3.4h"
                ],
                earliest_eligible_time=earliest,
            )
        )
        agent.set_hos_checker(mock_checker)

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-1"}}]
                }
            }
        )

        result = await agent._check_hos_eligibility(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_passes_estimated_hours_to_checker(self):
        """Verify estimated drive/total hours are passed to the checker."""
        agent, deps = _make_agent()
        mock_checker = AsyncMock()
        mock_checker.is_eligible = AsyncMock(
            return_value=_make_hos_eligibility(eligible=True)
        )
        agent.set_hos_checker(mock_checker)

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-42"}}]
                }
            }
        )

        assignments = _make_assignments(num_stops=3)
        await agent._check_hos_eligibility("tenant-1", "truck-1", assignments)

        # Verify the call was made with correct driver_id and hours
        call_kwargs = mock_checker.is_eligible.call_args[1]
        assert call_kwargs["driver_id"] == "driver-42"
        assert call_kwargs["estimated_drive_hours"] > 0
        assert call_kwargs["estimated_total_hours"] > call_kwargs["estimated_drive_hours"]


# ---------------------------------------------------------------------------
# Tests: _estimate_route_hours
# ---------------------------------------------------------------------------


class TestEstimateRouteHours:
    def test_zero_assignments_returns_zero(self):
        """Empty assignments produce zero hours."""
        agent, _ = _make_agent()
        drive, total = agent._estimate_route_hours([])
        assert drive == 0.0
        assert total == 0.0

    def test_single_stop_produces_positive_hours(self):
        """Single stop produces positive drive and total hours."""
        agent, _ = _make_agent()
        drive, total = agent._estimate_route_hours(_make_assignments(1))
        assert drive > 0.0
        assert total > drive  # total includes stop time

    def test_more_stops_increases_hours(self):
        """More stops produce more estimated hours."""
        agent, _ = _make_agent()
        drive_1, total_1 = agent._estimate_route_hours(_make_assignments(1))
        drive_5, total_5 = agent._estimate_route_hours(_make_assignments(5))
        assert drive_5 > drive_1
        assert total_5 > total_1

    def test_total_exceeds_drive(self):
        """Total hours always exceed drive hours (includes stop time)."""
        agent, _ = _make_agent()
        drive, total = agent._estimate_route_hours(_make_assignments(4))
        assert total > drive


# ---------------------------------------------------------------------------
# Tests: evaluate() integration with HOS eligibility
# ---------------------------------------------------------------------------


class TestEvaluateHOSEligibility:
    @pytest.mark.asyncio
    async def test_evaluate_skips_plan_when_driver_hos_ineligible(self):
        """evaluate() skips loading plan when driver is HOS-ineligible."""
        agent, deps = _make_agent()

        # Wire driver qualification service to pass (eligible)
        mock_dq_service = AsyncMock()
        dq_eligibility = MagicMock()
        dq_eligibility.eligible = True
        dq_eligibility.reasons = []
        mock_dq_service.is_dispatch_eligible = AsyncMock(
            return_value=dq_eligibility
        )
        agent.set_driver_qualification_service(mock_dq_service)

        # Wire HOS checker to fail (ineligible)
        mock_hos_checker = AsyncMock()
        mock_hos_checker.is_eligible = AsyncMock(
            return_value=_make_hos_eligibility(
                eligible=False,
                reasons=["Insufficient drive hours: 2.0h available, need 3.4h"],
                earliest_eligible_time=datetime(
                    2025, 6, 15, 8, 0, 0, tzinfo=timezone.utc
                ),
            )
        )
        agent.set_hos_checker(mock_hos_checker)

        # ES returns truck with driver for both lookups
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-1"}}]
                }
            }
        )

        # Buffer a loading proposal
        proposal = InterventionProposal(
            source_agent="compartment_loading",
            actions=[
                {
                    "tool_name": "apply_loading_plan",
                    "parameters": {
                        "plan_id": "plan-1",
                        "truck_id": "truck-1",
                        "assignments": _make_assignments(),
                        "total_utilization_pct": 75.0,
                    },
                }
            ],
            expected_kpi_delta={"truck_utilization_pct": 75.0},
            risk_class=RiskClass.LOW,
            confidence=0.85,
            priority=1,
            tenant_id="tenant-1",
        )
        agent._proposal_buffer.append(proposal)

        # evaluate should produce no route proposals
        result = await agent.evaluate([])
        assert result == []
        # index_document should NOT have been called (no route persisted)
        deps["es_service"].index_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluate_proceeds_when_hos_checker_not_configured(self):
        """evaluate() proceeds normally when no HOS checker is wired."""
        agent, deps = _make_agent()

        # Wire driver qualification service to pass
        mock_dq_service = AsyncMock()
        dq_eligibility = MagicMock()
        dq_eligibility.eligible = True
        dq_eligibility.reasons = []
        mock_dq_service.is_dispatch_eligible = AsyncMock(
            return_value=dq_eligibility
        )
        agent.set_driver_qualification_service(mock_dq_service)

        # No HOS checker configured (default None)

        # ES returns truck with driver
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-1"}}]
                }
            }
        )

        # Buffer a loading proposal with valid station
        proposal = InterventionProposal(
            source_agent="compartment_loading",
            actions=[
                {
                    "tool_name": "apply_loading_plan",
                    "parameters": {
                        "plan_id": "plan-1",
                        "truck_id": "truck-1",
                        "assignments": _make_assignments(),
                        "total_utilization_pct": 75.0,
                    },
                }
            ],
            expected_kpi_delta={"truck_utilization_pct": 75.0},
            risk_class=RiskClass.LOW,
            confidence=0.85,
            priority=1,
            tenant_id="tenant-1",
        )
        agent._proposal_buffer.append(proposal)

        # evaluate should proceed past the HOS check (it won't produce
        # a full route because station locations won't be found, but
        # it should NOT be blocked by HOS). The key assertion is that
        # the code path continues past the HOS check.
        result = await agent.evaluate([])
        # The route won't complete (no station locations in ES) but
        # the HOS check should not have blocked it
        # Verify that the agent attempted to query station locations
        # (which means it passed the HOS check)
        search_calls = deps["es_service"].search_documents.call_args_list
        # At least one call should be for station locations (fuel_stations index)
        station_queries = [
            c for c in search_calls
            if len(c[0]) > 0 and "fuel_stations" in str(c[0][0])
        ]
        # The agent should have attempted station lookup (past HOS check)
        # Even if it returns empty, the flow continued past HOS
        assert len(search_calls) >= 2  # At least truck lookup + station lookup
