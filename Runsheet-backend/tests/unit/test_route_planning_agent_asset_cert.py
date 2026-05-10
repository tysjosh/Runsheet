"""
Unit tests for Route_Planning_Agent asset certification check (Task 8.10).

Tests cover:
- set_asset_certification_service() setter wiring
- _check_asset_certification() graceful degradation when service not configured
- _check_asset_certification() returns True for eligible assets
- _check_asset_certification() returns False for ineligible assets (expired cert)
- _check_asset_certification() graceful degradation on service exceptions
- evaluate() skips loading plan when asset is ineligible

Validates: Requirement 13.5
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

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


def _make_asset_eligibility(eligible=True, reasons=None):
    """Create a mock AssetEligibility result."""
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
# Tests: set_asset_certification_service
# ---------------------------------------------------------------------------


class TestSetAssetCertificationService:
    def test_setter_stores_service(self):
        agent, _ = _make_agent()
        mock_service = MagicMock()
        agent.set_asset_certification_service(mock_service)
        assert agent._asset_certification_service is mock_service

    def test_setter_accepts_none(self):
        agent, _ = _make_agent()
        agent.set_asset_certification_service(None)
        assert agent._asset_certification_service is None

    def test_default_is_none(self):
        agent, _ = _make_agent()
        assert agent._asset_certification_service is None


# ---------------------------------------------------------------------------
# Tests: _check_asset_certification — graceful degradation
# ---------------------------------------------------------------------------


class TestCheckAssetCertificationGracefulDegradation:
    @pytest.mark.asyncio
    async def test_returns_true_when_service_not_configured(self):
        """When no AssetCertificationService is wired, allow all routes."""
        agent, _ = _make_agent()
        # Service is None by default
        result = await agent._check_asset_certification("tenant-1", "truck-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_on_service_exception(self):
        """When the certification check raises, allow route (graceful degradation)."""
        agent, _ = _make_agent()
        mock_service = AsyncMock()
        mock_service.is_dispatch_eligible = AsyncMock(
            side_effect=RuntimeError("ES connection failed")
        )
        agent.set_asset_certification_service(mock_service)

        result = await agent._check_asset_certification("tenant-1", "truck-1")
        assert result is True


# ---------------------------------------------------------------------------
# Tests: _check_asset_certification — eligibility checks (Req 13.5)
# ---------------------------------------------------------------------------


class TestCheckAssetCertificationEligibility:
    @pytest.mark.asyncio
    async def test_eligible_asset_returns_true(self):
        """Asset with valid certifications allows route to proceed."""
        agent, _ = _make_agent()
        mock_service = AsyncMock()
        mock_service.is_dispatch_eligible = AsyncMock(
            return_value=_make_asset_eligibility(eligible=True)
        )
        agent.set_asset_certification_service(mock_service)

        result = await agent._check_asset_certification("tenant-1", "truck-1")
        assert result is True
        mock_service.is_dispatch_eligible.assert_called_once_with(
            "tenant-1", "truck-1"
        )

    @pytest.mark.asyncio
    async def test_ineligible_asset_returns_false(self):
        """Asset with expired DOT cert causes route to be skipped (Req 13.5)."""
        agent, _ = _make_agent()
        mock_service = AsyncMock()
        mock_service.is_dispatch_eligible = AsyncMock(
            return_value=_make_asset_eligibility(
                eligible=False,
                reasons=[
                    "DOT cargo tank certification 'V_test' (cert_id=cert-1) is expired"
                ],
            )
        )
        agent.set_asset_certification_service(mock_service)

        result = await agent._check_asset_certification("tenant-1", "truck-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_passes_correct_tenant_and_asset_id(self):
        """Verify tenant_id and truck_id are correctly passed to the service."""
        agent, _ = _make_agent()
        mock_service = AsyncMock()
        mock_service.is_dispatch_eligible = AsyncMock(
            return_value=_make_asset_eligibility(eligible=True)
        )
        agent.set_asset_certification_service(mock_service)

        await agent._check_asset_certification("tenant-42", "truck-99")

        mock_service.is_dispatch_eligible.assert_called_once_with(
            "tenant-42", "truck-99"
        )


# ---------------------------------------------------------------------------
# Tests: evaluate() integration with asset certification
# ---------------------------------------------------------------------------


class TestEvaluateAssetCertification:
    @pytest.mark.asyncio
    async def test_evaluate_skips_plan_when_asset_ineligible(self):
        """evaluate() skips loading plan when asset has expired cert."""
        agent, deps = _make_agent()

        # Wire driver qualification service to return eligible
        mock_driver_service = AsyncMock()
        mock_driver_eligibility = MagicMock()
        mock_driver_eligibility.eligible = True
        mock_driver_eligibility.reasons = []
        mock_driver_service.is_dispatch_eligible = AsyncMock(
            return_value=mock_driver_eligibility
        )
        agent.set_driver_qualification_service(mock_driver_service)

        # Wire HOS checker to return eligible
        mock_hos = AsyncMock()
        mock_hos_eligibility = MagicMock()
        mock_hos_eligibility.eligible = True
        mock_hos_eligibility.reasons = []
        mock_hos.is_eligible = AsyncMock(return_value=mock_hos_eligibility)
        agent.set_hos_checker(mock_hos)

        # Wire asset certification service to return ineligible
        mock_asset_service = AsyncMock()
        mock_asset_service.is_dispatch_eligible = AsyncMock(
            return_value=_make_asset_eligibility(
                eligible=False,
                reasons=[
                    "DOT cargo tank certification 'K_test' (cert_id=cert-2) is expired"
                ],
            )
        )
        agent.set_asset_certification_service(mock_asset_service)

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
