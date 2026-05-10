"""
Unit tests for Route_Planning_Agent DeliveryFilter wiring (Task 15.5).

Tests cover:
- set_delivery_filter() setter wiring
- _apply_delivery_filter() graceful degradation when filter not configured
- _apply_delivery_filter() returns FilteredCandidates when filter is configured
- _apply_delivery_filter() graceful degradation on filter exceptions
- _build_delivery_candidates_from_proposals() extracts candidates from proposals
- evaluate() calls the delivery filter before the solver runs

Validates: Requirement 14.5
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
)
from Agents.overlay.route_planning_agent import RoutePlanningAgent
from compliance.services.delivery_filter import (
    CustomerType,
    DeliveryCandidate,
    FilteredCandidates,
)


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


def _make_proposal_with_delivery_metadata(
    *,
    customer_type="will_call",
    order_id="order-1",
    order_status="ready_for_dispatch",
    customer_id="cust-1",
    plan_id="plan-1",
    truck_id="truck-1",
    tank_level_percent=None,
    forecast_days_to_empty=None,
    planning_horizon_days=None,
    tenant_id="tenant-1",
):
    """Create a loading proposal with delivery candidate metadata."""
    return InterventionProposal(
        source_agent="compartment_loading",
        actions=[
            {
                "tool_name": "apply_loading_plan",
                "parameters": {
                    "plan_id": plan_id,
                    "truck_id": truck_id,
                    "customer_id": customer_id,
                    "customer_type": customer_type,
                    "order_id": order_id,
                    "order_status": order_status,
                    "tank_level_percent": tank_level_percent,
                    "forecast_days_to_empty": forecast_days_to_empty,
                    "planning_horizon_days": planning_horizon_days,
                    "assignments": [
                        {
                            "compartment_id": "comp-1",
                            "station_id": "station-1",
                            "fuel_grade": "DIESEL_2",
                            "quantity_liters": 5000.0,
                        }
                    ],
                    "total_utilization_pct": 75.0,
                },
            }
        ],
        expected_kpi_delta={"truck_utilization_pct": 75.0},
        risk_class=RiskClass.LOW,
        confidence=0.85,
        priority=1,
        tenant_id=tenant_id,
    )


def _make_proposal_without_delivery_metadata(tenant_id="tenant-1"):
    """Create a loading proposal WITHOUT delivery candidate metadata."""
    return InterventionProposal(
        source_agent="compartment_loading",
        actions=[
            {
                "tool_name": "apply_loading_plan",
                "parameters": {
                    "plan_id": "plan-2",
                    "truck_id": "truck-2",
                    "assignments": [
                        {
                            "compartment_id": "comp-1",
                            "station_id": "station-1",
                            "fuel_grade": "DIESEL_2",
                            "quantity_liters": 5000.0,
                        }
                    ],
                    "total_utilization_pct": 80.0,
                },
            }
        ],
        expected_kpi_delta={"truck_utilization_pct": 80.0},
        risk_class=RiskClass.LOW,
        confidence=0.90,
        priority=1,
        tenant_id=tenant_id,
    )


# ---------------------------------------------------------------------------
# Tests: set_delivery_filter
# ---------------------------------------------------------------------------


class TestSetDeliveryFilter:
    def test_setter_stores_filter(self):
        agent, _ = _make_agent()
        mock_filter = MagicMock()
        agent.set_delivery_filter(mock_filter)
        assert agent._delivery_filter is mock_filter

    def test_setter_accepts_none(self):
        agent, _ = _make_agent()
        agent.set_delivery_filter(None)
        assert agent._delivery_filter is None

    def test_default_is_none(self):
        agent, _ = _make_agent()
        assert agent._delivery_filter is None


# ---------------------------------------------------------------------------
# Tests: _apply_delivery_filter — graceful degradation
# ---------------------------------------------------------------------------


class TestApplyDeliveryFilterGracefulDegradation:
    @pytest.mark.asyncio
    async def test_returns_none_when_filter_not_configured(self):
        """When no DeliveryFilter is wired, returns None (graceful degradation)."""
        agent, _ = _make_agent()
        # Filter is None by default
        result = await agent._apply_delivery_filter()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_candidates_in_buffer(self):
        """When buffer has no proposals with delivery metadata, returns None."""
        agent, _ = _make_agent()
        mock_filter = AsyncMock()
        agent.set_delivery_filter(mock_filter)

        # Buffer is empty
        result = await agent._apply_delivery_filter()
        assert result is None
        mock_filter.partition_candidates.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_on_filter_exception(self):
        """When the filter raises, returns None (graceful degradation)."""
        agent, _ = _make_agent()
        mock_filter = AsyncMock()
        mock_filter.partition_candidates = AsyncMock(
            side_effect=RuntimeError("ES connection failed")
        )
        agent.set_delivery_filter(mock_filter)

        # Add a proposal with delivery metadata
        proposal = _make_proposal_with_delivery_metadata()
        agent._proposal_buffer.append(proposal)

        result = await agent._apply_delivery_filter()
        assert result is None


# ---------------------------------------------------------------------------
# Tests: _apply_delivery_filter — successful filtering
# ---------------------------------------------------------------------------


class TestApplyDeliveryFilterSuccess:
    @pytest.mark.asyncio
    async def test_calls_partition_candidates_with_extracted_candidates(self):
        """Filter is called with DeliveryCandidate objects from proposals."""
        agent, _ = _make_agent()
        mock_filter = AsyncMock()
        expected_result = FilteredCandidates(
            will_call=[
                DeliveryCandidate(
                    candidate_id="plan-1",
                    customer_id="cust-1",
                    customer_type=CustomerType.WILL_CALL,
                    order_id="order-1",
                    order_status="ready_for_dispatch",
                )
            ],
            auto_fill=[],
            keep_full=[],
            excluded=[],
        )
        mock_filter.partition_candidates = AsyncMock(
            return_value=expected_result
        )
        agent.set_delivery_filter(mock_filter)

        # Add a will_call proposal
        proposal = _make_proposal_with_delivery_metadata(
            customer_type="will_call",
            order_id="order-1",
            order_status="ready_for_dispatch",
        )
        agent._proposal_buffer.append(proposal)

        result = await agent._apply_delivery_filter()

        assert result is expected_result
        mock_filter.partition_candidates.assert_called_once()
        # Verify the candidates passed to the filter
        call_args = mock_filter.partition_candidates.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0].candidate_id == "plan-1"
        assert call_args[0].customer_type == CustomerType.WILL_CALL
        assert call_args[0].order_id == "order-1"
        assert call_args[0].order_status == "ready_for_dispatch"

    @pytest.mark.asyncio
    async def test_filters_multiple_candidates(self):
        """Multiple proposals with delivery metadata are all passed to filter."""
        agent, _ = _make_agent()
        mock_filter = AsyncMock()
        mock_filter.partition_candidates = AsyncMock(
            return_value=FilteredCandidates(
                will_call=[],
                auto_fill=[],
                keep_full=[],
                excluded=[],
            )
        )
        agent.set_delivery_filter(mock_filter)

        # Add multiple proposals with different customer types
        agent._proposal_buffer.append(
            _make_proposal_with_delivery_metadata(
                customer_type="will_call",
                plan_id="plan-1",
                customer_id="cust-1",
            )
        )
        agent._proposal_buffer.append(
            _make_proposal_with_delivery_metadata(
                customer_type="auto_fill",
                plan_id="plan-2",
                customer_id="cust-2",
                order_id="order-2",
                forecast_days_to_empty=3.0,
                planning_horizon_days=7.0,
            )
        )
        agent._proposal_buffer.append(
            _make_proposal_with_delivery_metadata(
                customer_type="keep_full",
                plan_id="plan-3",
                customer_id="cust-3",
                order_id="order-3",
                tank_level_percent=20.0,
            )
        )

        result = await agent._apply_delivery_filter()

        call_args = mock_filter.partition_candidates.call_args[0][0]
        assert len(call_args) == 3
        types = {c.customer_type for c in call_args}
        assert types == {
            CustomerType.WILL_CALL,
            CustomerType.AUTO_FILL,
            CustomerType.KEEP_FULL,
        }

    @pytest.mark.asyncio
    async def test_skips_proposals_without_delivery_metadata(self):
        """Proposals without customer_type metadata are not passed to filter."""
        agent, _ = _make_agent()
        mock_filter = AsyncMock()
        mock_filter.partition_candidates = AsyncMock(
            return_value=FilteredCandidates()
        )
        agent.set_delivery_filter(mock_filter)

        # Add one proposal with metadata and one without
        agent._proposal_buffer.append(
            _make_proposal_with_delivery_metadata(
                customer_type="will_call",
                plan_id="plan-1",
            )
        )
        agent._proposal_buffer.append(
            _make_proposal_without_delivery_metadata()
        )

        await agent._apply_delivery_filter()

        # Only the proposal with metadata should produce a candidate
        call_args = mock_filter.partition_candidates.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0].candidate_id == "plan-1"


# ---------------------------------------------------------------------------
# Tests: _build_delivery_candidates_from_proposals
# ---------------------------------------------------------------------------


class TestBuildDeliveryCandidatesFromProposals:
    def test_extracts_will_call_candidate(self):
        """Extracts a will_call candidate with correct fields."""
        agent, _ = _make_agent()
        proposal = _make_proposal_with_delivery_metadata(
            customer_type="will_call",
            order_id="order-1",
            order_status="ready_for_dispatch",
            customer_id="cust-1",
            plan_id="plan-1",
        )
        agent._proposal_buffer.append(proposal)

        candidates = agent._build_delivery_candidates_from_proposals()

        assert len(candidates) == 1
        c = candidates[0]
        assert c.candidate_id == "plan-1"
        assert c.customer_id == "cust-1"
        assert c.customer_type == CustomerType.WILL_CALL
        assert c.order_id == "order-1"
        assert c.order_status == "ready_for_dispatch"

    def test_extracts_auto_fill_candidate(self):
        """Extracts an auto_fill candidate with forecast data."""
        agent, _ = _make_agent()
        proposal = _make_proposal_with_delivery_metadata(
            customer_type="auto_fill",
            order_id="order-2",
            order_status="scheduled",
            customer_id="cust-2",
            plan_id="plan-2",
            forecast_days_to_empty=5.0,
            planning_horizon_days=7.0,
        )
        agent._proposal_buffer.append(proposal)

        candidates = agent._build_delivery_candidates_from_proposals()

        assert len(candidates) == 1
        c = candidates[0]
        assert c.customer_type == CustomerType.AUTO_FILL
        assert c.forecast_days_to_empty == 5.0
        assert c.planning_horizon_days == 7.0

    def test_extracts_keep_full_candidate(self):
        """Extracts a keep_full candidate with tank level data."""
        agent, _ = _make_agent()
        proposal = _make_proposal_with_delivery_metadata(
            customer_type="keep_full",
            order_id="order-3",
            order_status="scheduled",
            customer_id="cust-3",
            plan_id="plan-3",
            tank_level_percent=20.0,
        )
        agent._proposal_buffer.append(proposal)

        candidates = agent._build_delivery_candidates_from_proposals()

        assert len(candidates) == 1
        c = candidates[0]
        assert c.customer_type == CustomerType.KEEP_FULL
        assert c.tank_level_percent == 20.0

    def test_skips_proposals_without_customer_type(self):
        """Proposals without customer_type are skipped."""
        agent, _ = _make_agent()
        proposal = _make_proposal_without_delivery_metadata()
        agent._proposal_buffer.append(proposal)

        candidates = agent._build_delivery_candidates_from_proposals()

        assert len(candidates) == 0

    def test_skips_invalid_customer_type(self):
        """Proposals with invalid customer_type are skipped."""
        agent, _ = _make_agent()
        proposal = InterventionProposal(
            source_agent="compartment_loading",
            actions=[
                {
                    "tool_name": "apply_loading_plan",
                    "parameters": {
                        "plan_id": "plan-bad",
                        "truck_id": "truck-1",
                        "customer_id": "cust-1",
                        "customer_type": "invalid_type",
                        "assignments": [],
                        "total_utilization_pct": 50.0,
                    },
                }
            ],
            expected_kpi_delta={},
            risk_class=RiskClass.LOW,
            confidence=0.5,
            priority=1,
            tenant_id="tenant-1",
        )
        agent._proposal_buffer.append(proposal)

        candidates = agent._build_delivery_candidates_from_proposals()

        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# Tests: evaluate() integration with delivery filter
# ---------------------------------------------------------------------------


class TestEvaluateDeliveryFilter:
    @pytest.mark.asyncio
    async def test_evaluate_calls_filter_before_solver(self):
        """evaluate() calls _apply_delivery_filter at the top before processing."""
        agent, deps = _make_agent()

        # Wire a mock delivery filter
        mock_filter = AsyncMock()
        mock_filter.partition_candidates = AsyncMock(
            return_value=FilteredCandidates(
                will_call=[
                    DeliveryCandidate(
                        candidate_id="plan-1",
                        customer_id="cust-1",
                        customer_type=CustomerType.WILL_CALL,
                        order_id="order-1",
                        order_status="ready_for_dispatch",
                    )
                ],
                auto_fill=[],
                keep_full=[],
                excluded=[],
            )
        )
        agent.set_delivery_filter(mock_filter)

        # Buffer a proposal with delivery metadata
        proposal = _make_proposal_with_delivery_metadata()
        agent._proposal_buffer.append(proposal)

        # evaluate will proceed through the filter then hit other checks
        # (driver eligibility, etc.) — we don't need to mock all of those
        # for this test; we just verify the filter was called
        await agent.evaluate([])

        # Verify the filter was called
        mock_filter.partition_candidates.assert_called_once()

    @pytest.mark.asyncio
    async def test_evaluate_proceeds_without_filter(self):
        """evaluate() works normally when no filter is configured."""
        agent, deps = _make_agent()

        # No filter configured (default)
        assert agent._delivery_filter is None

        # Buffer a proposal
        proposal = _make_proposal_with_delivery_metadata()
        agent._proposal_buffer.append(proposal)

        # evaluate should not raise — graceful degradation
        result = await agent.evaluate([])
        # Result may be empty due to other checks failing (no station locations, etc.)
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_evaluate_continues_on_filter_failure(self):
        """evaluate() continues processing when filter raises an exception."""
        agent, deps = _make_agent()

        # Wire a filter that raises
        mock_filter = AsyncMock()
        mock_filter.partition_candidates = AsyncMock(
            side_effect=RuntimeError("Filter crashed")
        )
        agent.set_delivery_filter(mock_filter)

        # Buffer a proposal
        proposal = _make_proposal_with_delivery_metadata()
        agent._proposal_buffer.append(proposal)

        # evaluate should not raise — graceful degradation
        result = await agent.evaluate([])
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_evaluate_empty_buffer_skips_filter(self):
        """evaluate() with empty buffer returns early without calling filter."""
        agent, _ = _make_agent()

        mock_filter = AsyncMock()
        mock_filter.partition_candidates = AsyncMock()
        agent.set_delivery_filter(mock_filter)

        # Empty buffer
        result = await agent.evaluate([])
        assert result == []
        # Filter should not be called when buffer is empty
        mock_filter.partition_candidates.assert_not_called()
