"""
Unit tests for the CompartmentLoadingAgent overlay agent.

Tests cover:
- Constructor and agent_id configuration
- Signal subscription setup (DeliveryPriorityList messages)
- _on_signal() buffering of DeliveryPriorityList
- evaluate() with empty priority buffer
- evaluate() builds delivery requests from priorities (Req 3.1)
- evaluate() queries trucks and compartments (Req 3.1)
- evaluate() runs feasibility + optimization (Req 3.3, 3.4)
- evaluate() persists loading plans to mvp_load_plans (Req 3.9)
- evaluate() produces InterventionProposals with loading plan actions
- _build_delivery_requests() filters by priority bucket
- _query_trucks() parses compartments from ES
- _build_proposal() risk classification

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from Agents.overlay.compartment_loading_agent import (
    DEFAULT_MIN_DROP_LITERS,
    DEFAULT_UNCERTAINTY_BUFFER_PCT,
    CompartmentLoadingAgent,
)
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_priority_list(
    priorities=None,
    tenant_id="tenant-1",
    run_id="run-1",
):
    if priorities is None:
        priorities = [
            DeliveryPriority(
                station_id="station-1",
                fuel_grade=FuelGrade.AGO,
                priority_score=0.9,
                priority_bucket=PriorityBucket.CRITICAL,
                reasons=["high_runout_risk"],
            ),
        ]
    return DeliveryPriorityList(
        priorities=priorities,
        scoring_weights={"runout_risk_24h": 0.4},
        tenant_id=tenant_id,
        run_id=run_id,
    )


def _make_compartment_hit(
    compartment_id="comp-1",
    truck_id="truck-1",
    capacity_liters=10000.0,
    allowed_grades=None,
    position_index=0,
    tenant_id="tenant-1",
):
    if allowed_grades is None:
        allowed_grades = ["AGO", "PMS"]
    return {
        "_source": {
            "compartment_id": compartment_id,
            "truck_id": truck_id,
            "capacity_liters": capacity_liters,
            "allowed_grades": allowed_grades,
            "position_index": position_index,
            "tenant_id": tenant_id,
        }
    }


def _make_deps():
    """Create mocked dependencies for the CompartmentLoadingAgent."""
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
    return CompartmentLoadingAgent(**deps), deps


# ---------------------------------------------------------------------------
# Tests: Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_agent_id(self):
        agent, _ = _make_agent()
        assert agent.agent_id == "compartment_loading"

    def test_subscription_to_delivery_priority_list(self):
        agent, _ = _make_agent()
        assert len(agent._subscription_specs) == 1
        spec = agent._subscription_specs[0]
        assert spec["message_type"] is DeliveryPriorityList

    def test_default_poll_interval(self):
        agent, _ = _make_agent()
        assert agent.poll_interval == 60

    def test_default_cooldown(self):
        agent, _ = _make_agent()
        assert agent.cooldown_minutes == 30

    def test_priority_buffer_initially_empty(self):
        agent, _ = _make_agent()
        assert agent._priority_buffer == []


# ---------------------------------------------------------------------------
# Tests: _on_signal() — DeliveryPriorityList buffering
# ---------------------------------------------------------------------------


class TestOnSignal:
    @pytest.mark.asyncio
    async def test_buffers_delivery_priority_list(self):
        agent, _ = _make_agent()
        priority_list = _make_priority_list()
        await agent._on_signal(priority_list)
        assert len(agent._priority_buffer) == 1
        assert agent._priority_buffer[0] is priority_list

    @pytest.mark.asyncio
    async def test_non_priority_list_goes_to_parent(self):
        """Non-DeliveryPriorityList signals go to the parent signal buffer."""
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
        assert len(agent._priority_buffer) == 0


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
    async def test_no_trucks_returns_empty(self):
        """When no trucks exist, evaluate returns empty."""
        agent, deps = _make_agent()
        agent._priority_buffer.append(_make_priority_list())

        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        result = await agent.evaluate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_produces_proposals_with_trucks(self):
        """Req 3.4: Produces loading plan proposals when trucks are available."""
        agent, deps = _make_agent()
        agent._priority_buffer.append(_make_priority_list())

        # Return compartments for a truck
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        _make_compartment_hit(
                            compartment_id="comp-1",
                            truck_id="truck-1",
                            capacity_liters=10000.0,
                        ),
                        _make_compartment_hit(
                            compartment_id="comp-2",
                            truck_id="truck-1",
                            capacity_liters=8000.0,
                            position_index=1,
                        ),
                    ]
                }
            }
        )

        result = await agent.evaluate([])
        assert len(result) == 1
        assert isinstance(result[0], InterventionProposal)
        assert result[0].source_agent == "compartment_loading"

    @pytest.mark.asyncio
    async def test_persists_loading_plan_to_es(self):
        """Req 3.9: Loading plans persisted to mvp_load_plans."""
        agent, deps = _make_agent()
        agent._priority_buffer.append(_make_priority_list())

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        _make_compartment_hit(
                            compartment_id="comp-1",
                            truck_id="truck-1",
                            capacity_liters=10000.0,
                        ),
                    ]
                }
            }
        )

        await agent.evaluate([])

        # index_document should be called for the loading plan
        assert deps["es_service"].index_document.call_count >= 1
        call_args = deps["es_service"].index_document.call_args
        assert call_args[0][0] == "mvp_load_plans"

    @pytest.mark.asyncio
    async def test_proposal_contains_loading_plan_action(self):
        """Proposal actions should contain apply_loading_plan tool."""
        agent, deps = _make_agent()
        agent._priority_buffer.append(_make_priority_list())

        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        _make_compartment_hit(
                            compartment_id="comp-1",
                            truck_id="truck-1",
                            capacity_liters=10000.0,
                        ),
                    ]
                }
            }
        )

        result = await agent.evaluate([])
        assert len(result) == 1
        actions = result[0].actions
        assert len(actions) == 1
        assert actions[0]["tool_name"] == "apply_loading_plan"
        assert "plan_id" in actions[0]["parameters"]
        assert actions[0]["parameters"]["truck_id"] == "truck-1"

    @pytest.mark.asyncio
    async def test_clears_buffer_after_evaluate(self):
        """Priority buffer should be cleared after evaluation."""
        agent, deps = _make_agent()
        agent._priority_buffer.append(_make_priority_list())

        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        await agent.evaluate([])
        assert len(agent._priority_buffer) == 0


# ---------------------------------------------------------------------------
# Tests: _build_delivery_requests()
# ---------------------------------------------------------------------------


class TestBuildDeliveryRequests:
    def test_filters_critical_and_high_only(self):
        """Only CRITICAL and HIGH priorities become delivery requests."""
        agent, _ = _make_agent()
        priority_list = _make_priority_list(
            priorities=[
                DeliveryPriority(
                    station_id="s1",
                    fuel_grade=FuelGrade.AGO,
                    priority_score=0.9,
                    priority_bucket=PriorityBucket.CRITICAL,
                ),
                DeliveryPriority(
                    station_id="s2",
                    fuel_grade=FuelGrade.PMS,
                    priority_score=0.7,
                    priority_bucket=PriorityBucket.HIGH,
                ),
                DeliveryPriority(
                    station_id="s3",
                    fuel_grade=FuelGrade.ATK,
                    priority_score=0.4,
                    priority_bucket=PriorityBucket.MEDIUM,
                ),
                DeliveryPriority(
                    station_id="s4",
                    fuel_grade=FuelGrade.LPG,
                    priority_score=0.1,
                    priority_bucket=PriorityBucket.LOW,
                ),
            ]
        )
        requests = agent._build_delivery_requests(priority_list)
        assert len(requests) == 2
        station_ids = {r.station_id for r in requests}
        assert station_ids == {"s1", "s2"}

    def test_empty_priorities_returns_empty(self):
        agent, _ = _make_agent()
        priority_list = _make_priority_list(priorities=[])
        requests = agent._build_delivery_requests(priority_list)
        assert requests == []

    def test_delivery_request_has_min_drop(self):
        """Req 3.5: Delivery requests include min_drop_liters."""
        agent, _ = _make_agent()
        priority_list = _make_priority_list()
        requests = agent._build_delivery_requests(priority_list)
        assert len(requests) == 1
        assert requests[0].min_drop_liters == DEFAULT_MIN_DROP_LITERS

    def test_higher_priority_gets_larger_quantity(self):
        """Higher priority score should result in larger delivery quantity."""
        agent, _ = _make_agent()
        priority_list = _make_priority_list(
            priorities=[
                DeliveryPriority(
                    station_id="s1",
                    fuel_grade=FuelGrade.AGO,
                    priority_score=0.95,
                    priority_bucket=PriorityBucket.CRITICAL,
                ),
                DeliveryPriority(
                    station_id="s2",
                    fuel_grade=FuelGrade.AGO,
                    priority_score=0.65,
                    priority_bucket=PriorityBucket.HIGH,
                ),
            ]
        )
        requests = agent._build_delivery_requests(priority_list)
        assert len(requests) == 2
        qty_s1 = next(r.quantity_liters for r in requests if r.station_id == "s1")
        qty_s2 = next(r.quantity_liters for r in requests if r.station_id == "s2")
        assert qty_s1 > qty_s2


# ---------------------------------------------------------------------------
# Tests: _build_proposal()
# ---------------------------------------------------------------------------


class TestBuildProposal:
    def test_proposal_risk_class_low_when_fully_served(self):
        """Risk class should be LOW when all demand is served."""
        from Agents.support.compartment_models import (
            CompartmentAssignment,
            FeasibilityResult,
            LoadingPlan,
        )

        agent, _ = _make_agent()
        plan = LoadingPlan(
            truck_id="truck-1",
            assignments=[
                CompartmentAssignment(
                    compartment_id="c1",
                    station_id="s1",
                    fuel_grade="AGO",
                    quantity_liters=5000.0,
                    compartment_capacity_liters=10000.0,
                )
            ],
            total_utilization_pct=50.0,
            unserved_demand_liters=0.0,
            tenant_id="tenant-1",
        )
        feasibility = FeasibilityResult(feasible=True, max_utilization_pct=50.0)

        proposal = agent._build_proposal(plan, feasibility, "tenant-1")
        assert proposal.risk_class == RiskClass.LOW

    def test_proposal_risk_class_medium_when_unserved(self):
        """Risk class should be MEDIUM when there is unserved demand."""
        from Agents.support.compartment_models import (
            CompartmentAssignment,
            FeasibilityResult,
            LoadingPlan,
        )

        agent, _ = _make_agent()
        plan = LoadingPlan(
            truck_id="truck-1",
            assignments=[
                CompartmentAssignment(
                    compartment_id="c1",
                    station_id="s1",
                    fuel_grade="AGO",
                    quantity_liters=5000.0,
                    compartment_capacity_liters=10000.0,
                )
            ],
            total_utilization_pct=50.0,
            unserved_demand_liters=2000.0,
            tenant_id="tenant-1",
        )
        feasibility = FeasibilityResult(feasible=True, max_utilization_pct=50.0)

        proposal = agent._build_proposal(plan, feasibility, "tenant-1")
        assert proposal.risk_class == RiskClass.MEDIUM
