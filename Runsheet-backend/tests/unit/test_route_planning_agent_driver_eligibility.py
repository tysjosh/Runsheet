"""
Unit tests for Route_Planning_Agent driver eligibility check (Task 6.9).

Tests cover:
- _find_available_asset() graceful degradation when service not configured
- _find_available_asset() skips when no driver_id found on truck
- _find_available_asset() returns True for eligible drivers
- _find_available_asset() returns False for ineligible drivers (Req 5.5)
- _find_available_asset() handles HAZMAT route requirements (Req 5.6)
- _find_available_asset() handles tanker route requirements (Req 5.7)
- _find_available_asset() graceful degradation on service exceptions
- _build_route_requirements() derives HAZMAT/tanker from assignments
- set_driver_qualification_service() setter wiring
- evaluate() skips loading plan when driver is ineligible

Validates: Requirements 5.5, 5.6, 5.7
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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


def _make_eligibility(eligible=True, reasons=None):
    """Create a mock DriverEligibility result."""
    mock = MagicMock()
    mock.eligible = eligible
    mock.reasons = reasons or []
    return mock


def _make_assignments(fuel_grades=None):
    """Create sample loading plan assignments."""
    if fuel_grades is None:
        fuel_grades = ["DIESEL_2"]
    return [
        {
            "compartment_id": f"comp-{i}",
            "station_id": f"station-{i}",
            "fuel_grade": grade,
            "quantity_liters": 5000.0,
        }
        for i, grade in enumerate(fuel_grades)
    ]


# ---------------------------------------------------------------------------
# Tests: set_driver_qualification_service
# ---------------------------------------------------------------------------


class TestSetDriverQualificationService:
    def test_setter_stores_service(self):
        agent, _ = _make_agent()
        mock_service = MagicMock()
        agent.set_driver_qualification_service(mock_service)
        assert agent._driver_qualification_service is mock_service

    def test_setter_accepts_none(self):
        agent, _ = _make_agent()
        agent.set_driver_qualification_service(None)
        assert agent._driver_qualification_service is None

    def test_default_is_none(self):
        agent, _ = _make_agent()
        assert agent._driver_qualification_service is None


# ---------------------------------------------------------------------------
# Tests: _find_available_asset — graceful degradation
# ---------------------------------------------------------------------------


class TestFindAvailableAssetGracefulDegradation:
    @pytest.mark.asyncio
    async def test_returns_true_when_service_not_configured(self):
        """When no DriverQualificationService is wired, allow all routes."""
        agent, _ = _make_agent()
        # Service is None by default
        result = await agent._find_available_asset(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_when_no_driver_on_truck(self):
        """When truck has no driver_id, skip check and allow route."""
        agent, deps = _make_agent()
        mock_service = AsyncMock()
        agent.set_driver_qualification_service(mock_service)

        # ES returns truck with no driver_id
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": ""}}]
                }
            }
        )

        result = await agent._find_available_asset(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is True
        # Service should not have been called
        mock_service.is_dispatch_eligible.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_true_when_truck_not_found(self):
        """When truck doesn't exist in ES, skip check and allow route."""
        agent, deps = _make_agent()
        mock_service = AsyncMock()
        agent.set_driver_qualification_service(mock_service)

        # ES returns no hits
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        result = await agent._find_available_asset(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_on_service_exception(self):
        """When the eligibility check raises, allow route (graceful degradation)."""
        agent, deps = _make_agent()
        mock_service = AsyncMock()
        mock_service.is_dispatch_eligible = AsyncMock(
            side_effect=RuntimeError("ES connection failed")
        )
        agent.set_driver_qualification_service(mock_service)

        # ES returns truck with driver
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-1"}}]
                }
            }
        )

        result = await agent._find_available_asset(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is True


# ---------------------------------------------------------------------------
# Tests: _find_available_asset — eligibility checks (Req 5.5, 5.6, 5.7)
# ---------------------------------------------------------------------------


class TestFindAvailableAssetEligibility:
    @pytest.mark.asyncio
    async def test_eligible_driver_returns_true(self):
        """Eligible driver allows route to proceed."""
        agent, deps = _make_agent()
        mock_service = AsyncMock()
        mock_service.is_dispatch_eligible = AsyncMock(
            return_value=_make_eligibility(eligible=True)
        )
        agent.set_driver_qualification_service(mock_service)

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-1"}}]
                }
            }
        )

        result = await agent._find_available_asset(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is True
        mock_service.is_dispatch_eligible.assert_called_once()

    @pytest.mark.asyncio
    async def test_suspended_driver_returns_false(self):
        """Suspended driver causes route to be skipped (Req 5.5)."""
        agent, deps = _make_agent()
        mock_service = AsyncMock()
        mock_service.is_dispatch_eligible = AsyncMock(
            return_value=_make_eligibility(
                eligible=False,
                reasons=["Driver status is 'suspended', must be 'active'"],
            )
        )
        agent.set_driver_qualification_service(mock_service)

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-1"}}]
                }
            }
        )

        result = await agent._find_available_asset(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_hazmat_endorsement_returns_false(self):
        """Driver without HAZMAT endorsement excluded from HAZMAT routes (Req 5.6)."""
        agent, deps = _make_agent()
        mock_service = AsyncMock()
        mock_service.is_dispatch_eligible = AsyncMock(
            return_value=_make_eligibility(
                eligible=False,
                reasons=["Route requires HAZMAT endorsement but driver has none"],
            )
        )
        agent.set_driver_qualification_service(mock_service)

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-1"}}]
                }
            }
        )

        result = await agent._find_available_asset(
            "tenant-1", "truck-1", _make_assignments(["GASOLINE_REG"])
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_tanker_endorsement_returns_false(self):
        """Driver without tanker endorsement excluded from tanker routes (Req 5.7)."""
        agent, deps = _make_agent()
        mock_service = AsyncMock()
        mock_service.is_dispatch_eligible = AsyncMock(
            return_value=_make_eligibility(
                eligible=False,
                reasons=["Route requires tanker endorsement but driver has none"],
            )
        )
        agent.set_driver_qualification_service(mock_service)

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-1"}}]
                }
            }
        )

        result = await agent._find_available_asset(
            "tenant-1", "truck-1", _make_assignments()
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_passes_correct_route_requirements(self):
        """Verify route_requirements dict is correctly built and passed."""
        agent, deps = _make_agent()
        mock_service = AsyncMock()
        mock_service.is_dispatch_eligible = AsyncMock(
            return_value=_make_eligibility(eligible=True)
        )
        agent.set_driver_qualification_service(mock_service)

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": {"driver_id": "driver-42"}}]
                }
            }
        )

        assignments = _make_assignments(["DIESEL_2"])
        await agent._find_available_asset("tenant-1", "truck-1", assignments)

        # Verify the call was made with correct tenant_id, driver_id
        call_args = mock_service.is_dispatch_eligible.call_args
        assert call_args[0][0] == "tenant-1"  # tenant_id
        assert call_args[0][1] == "driver-42"  # driver_id
        # route_requirements should have requires_hazmat and requires_tanker
        route_reqs = call_args[0][2]
        assert "requires_hazmat" in route_reqs
        assert "requires_tanker" in route_reqs


# ---------------------------------------------------------------------------
# Tests: _build_route_requirements
# ---------------------------------------------------------------------------


class TestBuildRouteRequirements:
    def test_diesel_requires_hazmat_and_tanker(self):
        """Diesel fuel requires both HAZMAT and tanker endorsements."""
        agent, _ = _make_agent()
        assignments = _make_assignments(["DIESEL_2"])
        reqs = agent._build_route_requirements(assignments)
        assert reqs["requires_hazmat"] is True
        assert reqs["requires_tanker"] is True
        assert reqs["min_cdl_class"] == "A"

    def test_gasoline_requires_hazmat_and_tanker(self):
        """Gasoline requires both HAZMAT and tanker endorsements."""
        agent, _ = _make_agent()
        assignments = _make_assignments(["GASOLINE_REG"])
        reqs = agent._build_route_requirements(assignments)
        assert reqs["requires_hazmat"] is True
        assert reqs["requires_tanker"] is True

    def test_propane_requires_hazmat_and_tanker(self):
        """Propane (LPG) requires both HAZMAT and tanker endorsements."""
        agent, _ = _make_agent()
        assignments = _make_assignments(["PROPANE"])
        reqs = agent._build_route_requirements(assignments)
        assert reqs["requires_hazmat"] is True
        assert reqs["requires_tanker"] is True

    def test_empty_assignments_no_tanker(self):
        """Empty assignments means no tanker requirement."""
        agent, _ = _make_agent()
        reqs = agent._build_route_requirements([])
        assert reqs["requires_hazmat"] is False
        assert reqs["requires_tanker"] is False
        assert reqs["min_cdl_class"] is None

    def test_unknown_fuel_grade_assumes_hazmat(self):
        """Unknown fuel grades are conservatively treated as HAZMAT."""
        agent, _ = _make_agent()
        assignments = _make_assignments(["UNKNOWN_FUEL_XYZ"])
        reqs = agent._build_route_requirements(assignments)
        assert reqs["requires_hazmat"] is True

    def test_def_is_not_hazmat(self):
        """DEF (Diesel Exhaust Fluid) is non-fuel and not HAZMAT."""
        agent, _ = _make_agent()
        assignments = _make_assignments(["DEF"])
        reqs = agent._build_route_requirements(assignments)
        assert reqs["requires_hazmat"] is False
        # Still requires tanker since it's a liquid delivery
        assert reqs["requires_tanker"] is True


# ---------------------------------------------------------------------------
# Tests: evaluate() integration with driver eligibility
# ---------------------------------------------------------------------------


class TestEvaluateDriverEligibility:
    @pytest.mark.asyncio
    async def test_evaluate_skips_plan_when_driver_ineligible(self):
        """evaluate() skips loading plan when driver is ineligible."""
        agent, deps = _make_agent()
        mock_service = AsyncMock()
        mock_service.is_dispatch_eligible = AsyncMock(
            return_value=_make_eligibility(
                eligible=False,
                reasons=["Driver status is 'suspended', must be 'active'"],
            )
        )
        agent.set_driver_qualification_service(mock_service)

        # ES returns truck with driver for the eligibility lookup
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
