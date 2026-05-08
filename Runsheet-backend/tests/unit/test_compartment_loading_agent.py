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
        """CRITICAL / HIGH / MEDIUM priorities become delivery requests.

        The agent was widened (see commit c5be838) to include MEDIUM
        priorities alongside CRITICAL and HIGH so loading plans can
        absorb near-threshold runouts in the same truck-run. LOW
        priorities remain excluded because they can safely wait for
        the next cycle.
        """
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
        assert len(requests) == 3
        station_ids = {r.station_id for r in requests}
        assert station_ids == {"s1", "s2", "s3"}

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


# ---------------------------------------------------------------------------
# Tests: _check_fuel_equipment() (Req 3.1, 3.2, 3.3, 3.5)
# ---------------------------------------------------------------------------


class TestCheckFuelEquipment:
    @pytest.mark.asyncio
    async def test_all_equipment_in_stock_returns_available(self):
        """Req 3.2: All fuel_equipment in stock → truck available."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "item_id": "hose-1",
                                "category": "fuel_equipment",
                                "status": "in_stock",
                                "location": "Depot A",
                            }
                        },
                        {
                            "_source": {
                                "item_id": "nozzle-1",
                                "category": "fuel_equipment",
                                "status": "in_stock",
                                "location": "Depot A",
                            }
                        },
                    ]
                }
            }
        )

        available, missing = await agent._check_fuel_equipment(
            "truck-1", "Depot A", "tenant-1"
        )
        assert available is True
        assert missing == []

    @pytest.mark.asyncio
    async def test_equipment_out_of_stock_returns_unavailable(self):
        """Req 3.3: Any fuel_equipment out_of_stock → truck excluded."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "item_id": "hose-1",
                                "category": "fuel_equipment",
                                "status": "in_stock",
                                "location": "Depot A",
                            }
                        },
                        {
                            "_source": {
                                "item_id": "seal-1",
                                "category": "fuel_equipment",
                                "status": "out_of_stock",
                                "location": "Depot A",
                            }
                        },
                    ]
                }
            }
        )

        available, missing = await agent._check_fuel_equipment(
            "truck-1", "Depot A", "tenant-1"
        )
        assert available is False
        assert missing == ["seal-1"]

    @pytest.mark.asyncio
    async def test_inventory_query_failure_fails_open(self):
        """Req 3.5: Inventory query failure → fail-open (include truck)."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=Exception("ES connection timeout")
        )

        available, missing = await agent._check_fuel_equipment(
            "truck-1", "Depot A", "tenant-1"
        )
        assert available is True
        assert missing == []

    @pytest.mark.asyncio
    async def test_no_equipment_at_depot_returns_available(self):
        """No fuel_equipment items at depot → truck available (nothing missing)."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        available, missing = await agent._check_fuel_equipment(
            "truck-1", "Depot A", "tenant-1"
        )
        assert available is True
        assert missing == []

    @pytest.mark.asyncio
    async def test_multiple_out_of_stock_items(self):
        """Multiple out_of_stock items are all reported."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "item_id": "hose-1",
                                "category": "fuel_equipment",
                                "status": "out_of_stock",
                                "location": "Depot A",
                            }
                        },
                        {
                            "_source": {
                                "item_id": "seal-1",
                                "category": "fuel_equipment",
                                "status": "out_of_stock",
                                "location": "Depot A",
                            }
                        },
                    ]
                }
            }
        )

        available, missing = await agent._check_fuel_equipment(
            "truck-1", "Depot A", "tenant-1"
        )
        assert available is False
        assert set(missing) == {"hose-1", "seal-1"}


# ---------------------------------------------------------------------------
# Tests: _query_trucks_with_equipment_check() (Req 3.1–3.5)
# ---------------------------------------------------------------------------


class TestQueryTrucksWithEquipmentCheck:
    @pytest.mark.asyncio
    async def test_trucks_without_depot_location_included(self):
        """Fail-open: trucks without depot_location are included."""
        agent, deps = _make_agent()

        # Return a truck without depot_location
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "compartment_id": "comp-1",
                                "truck_id": "truck-1",
                                "capacity_liters": 10000.0,
                                "allowed_grades": ["AGO"],
                                "position_index": 0,
                                "tenant_id": "tenant-1",
                            }
                        },
                    ]
                }
            }
        )

        trucks = await agent._query_trucks_with_equipment_check("tenant-1")
        assert "truck-1" in trucks

    @pytest.mark.asyncio
    async def test_truck_with_equipment_available_included(self):
        """Req 3.2: Truck with all equipment in stock is included."""
        agent, deps = _make_agent()

        # First call: truck_compartments query
        # Second call: inventory query for fuel_equipment
        call_count = [0]

        async def mock_search(index, query, size=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # truck_compartments
                return {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "compartment_id": "comp-1",
                                    "truck_id": "truck-1",
                                    "capacity_liters": 10000.0,
                                    "allowed_grades": ["AGO"],
                                    "position_index": 0,
                                    "tenant_id": "tenant-1",
                                    "depot_location": "Depot A",
                                }
                            },
                        ]
                    }
                }
            else:
                # inventory query — all in stock
                return {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "item_id": "hose-1",
                                    "category": "fuel_equipment",
                                    "status": "in_stock",
                                    "location": "Depot A",
                                }
                            },
                        ]
                    }
                }

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)

        trucks = await agent._query_trucks_with_equipment_check("tenant-1")
        assert "truck-1" in trucks

    @pytest.mark.asyncio
    async def test_truck_with_missing_equipment_excluded(self):
        """Req 3.3: Truck with out_of_stock equipment is excluded."""
        agent, deps = _make_agent()

        call_count = [0]

        async def mock_search(index, query, size=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "compartment_id": "comp-1",
                                    "truck_id": "truck-1",
                                    "capacity_liters": 10000.0,
                                    "allowed_grades": ["AGO"],
                                    "position_index": 0,
                                    "tenant_id": "tenant-1",
                                    "depot_location": "Depot A",
                                }
                            },
                        ]
                    }
                }
            else:
                return {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "item_id": "seal-1",
                                    "category": "fuel_equipment",
                                    "status": "out_of_stock",
                                    "location": "Depot A",
                                }
                            },
                        ]
                    }
                }

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)

        trucks = await agent._query_trucks_with_equipment_check("tenant-1")
        assert "truck-1" not in trucks

    @pytest.mark.asyncio
    async def test_all_trucks_excluded_publishes_critical_signal(self):
        """Req 3.5: All trucks excluded → critical RiskSignal published."""
        agent, deps = _make_agent()

        call_count = [0]

        async def mock_search(index, query, size=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "compartment_id": "comp-1",
                                    "truck_id": "truck-1",
                                    "capacity_liters": 10000.0,
                                    "allowed_grades": ["AGO"],
                                    "position_index": 0,
                                    "tenant_id": "tenant-1",
                                    "depot_location": "Depot A",
                                }
                            },
                        ]
                    }
                }
            else:
                return {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "item_id": "seal-1",
                                    "category": "fuel_equipment",
                                    "status": "out_of_stock",
                                    "location": "Depot A",
                                }
                            },
                        ]
                    }
                }

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)

        trucks = await agent._query_trucks_with_equipment_check("tenant-1")
        assert trucks == {}

        # Verify critical RiskSignal was published
        deps["signal_bus"].publish.assert_called_once()
        published_signal = deps["signal_bus"].publish.call_args[0][0]
        assert isinstance(published_signal, RiskSignal)
        assert published_signal.severity == Severity.CRITICAL
        assert published_signal.entity_type == "equipment_shortage"
        assert published_signal.tenant_id == "tenant-1"

    @pytest.mark.asyncio
    async def test_evaluate_uses_equipment_check(self):
        """evaluate() calls _query_trucks_with_equipment_check."""
        agent, deps = _make_agent()
        agent._priority_buffer.append(_make_priority_list())

        call_count = [0]

        async def mock_search(index, query, size=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # truck_compartments — truck with depot
                return {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "compartment_id": "comp-1",
                                    "truck_id": "truck-1",
                                    "capacity_liters": 10000.0,
                                    "allowed_grades": ["AGO"],
                                    "position_index": 0,
                                    "tenant_id": "tenant-1",
                                    "depot_location": "Depot A",
                                }
                            },
                        ]
                    }
                }
            else:
                # inventory — equipment in stock
                return {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "item_id": "hose-1",
                                    "category": "fuel_equipment",
                                    "status": "in_stock",
                                    "location": "Depot A",
                                }
                            },
                        ]
                    }
                }

        deps["es_service"].search_documents = AsyncMock(side_effect=mock_search)

        result = await agent.evaluate([])
        # Should produce a proposal since equipment is available
        assert len(result) == 1
        assert result[0].source_agent == "compartment_loading"


# ---------------------------------------------------------------------------
# Tests: _persist_loading_plan — Task 6.6 / Req 7.1.2
# ---------------------------------------------------------------------------
#
# After a successful mvp_load_plans write, the agent MUST update
# last_loaded_product, last_loaded_at, and state on every assigned
# compartment via the CompartmentStateRepository (atomic ES update).
# ---------------------------------------------------------------------------


class TestPersistLoadingPlanRecordsCompartmentState:
    def _make_plan(self, **overrides):
        from Agents.support.compartment_models import (
            CompartmentAssignment,
            LoadingPlan,
        )

        defaults = {
            "plan_id": "plan-1",
            "truck_id": "truck-1",
            "tenant_id": "tenant-1",
            "assignments": [
                CompartmentAssignment(
                    compartment_id="comp-1",
                    station_id="s1",
                    fuel_grade="AGO",  # NG alias canonicalizes to DIESEL_2
                    quantity_liters=3_000.0,
                    compartment_capacity_liters=5_000.0,
                ),
                CompartmentAssignment(
                    compartment_id="comp-2",
                    station_id="s2",
                    fuel_grade="PMS",  # NG alias canonicalizes to GASOLINE_REG
                    quantity_liters=2_000.0,
                    compartment_capacity_liters=5_000.0,
                ),
            ],
            "total_utilization_pct": 50.0,
            "tenant_id": "tenant-1",
        }
        defaults.update(overrides)
        return LoadingPlan(**defaults)

    @pytest.mark.asyncio
    async def test_marks_every_assigned_compartment_loaded(self):
        """Req 7.1.2: every successful assignment commits last_loaded fields."""
        agent, _ = _make_agent()
        repo = MagicMock()
        repo.mark_loaded = AsyncMock()
        agent._compartment_state_repo = repo

        plan = self._make_plan()
        await agent._persist_loading_plan(plan)

        # Two assignments → two mark_loaded calls.
        assert repo.mark_loaded.await_count == 2

        calls = repo.mark_loaded.await_args_list
        # First assignment — canonicalized AGO → DIESEL_2, truck_id-qualified doc id.
        first = calls[0].kwargs
        assert first["tenant_id"] == "tenant-1"
        assert first["compartment_doc_id"] == "truck-1_comp-1"
        assert first["product_code"] == "DIESEL_2"
        assert isinstance(first["loaded_at"], datetime)
        assert first["loaded_at"].tzinfo is timezone.utc

        second = calls[1].kwargs
        assert second["compartment_doc_id"] == "truck-1_comp-2"
        assert second["product_code"] == "GASOLINE_REG"

    @pytest.mark.asyncio
    async def test_uses_same_timestamp_for_all_assignments(self):
        """All compartments loaded on the same plan get the same stamp."""
        agent, _ = _make_agent()
        repo = MagicMock()
        repo.mark_loaded = AsyncMock()
        agent._compartment_state_repo = repo

        plan = self._make_plan()
        await agent._persist_loading_plan(plan)

        stamps = {
            call.kwargs["loaded_at"]
            for call in repo.mark_loaded.await_args_list
        }
        assert len(stamps) == 1

    @pytest.mark.asyncio
    async def test_skips_state_update_when_plan_persistence_fails(self):
        """A compartment must not be marked loaded if the plan write failed."""
        agent, deps = _make_agent()
        repo = MagicMock()
        repo.mark_loaded = AsyncMock()
        agent._compartment_state_repo = repo

        deps["es_service"].index_document = AsyncMock(
            side_effect=RuntimeError("boom")
        )

        plan = self._make_plan()
        await agent._persist_loading_plan(plan)

        repo.mark_loaded.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_per_compartment_failure_does_not_block_other_updates(self):
        """One failing mark_loaded should not prevent the sibling assignment."""
        from fuel.compartment_state_models import CompartmentNotFoundError

        agent, _ = _make_agent()
        repo = MagicMock()

        async def mark(*, tenant_id, compartment_doc_id, product_code, loaded_at):
            if compartment_doc_id == "truck-1_comp-1":
                raise CompartmentNotFoundError(tenant_id, compartment_doc_id)
            return None

        repo.mark_loaded = AsyncMock(side_effect=mark)
        agent._compartment_state_repo = repo

        plan = self._make_plan()
        # Should not raise despite the first call failing.
        await agent._persist_loading_plan(plan)

        assert repo.mark_loaded.await_count == 2

    @pytest.mark.asyncio
    async def test_no_repo_configured_is_a_no_op(self):
        """Legacy agents that lack a state repo should silently skip the write."""
        agent, _ = _make_agent()
        agent._compartment_state_repo = None

        plan = self._make_plan()
        # Should not raise even without a repository configured.
        await agent._persist_loading_plan(plan)

    @pytest.mark.asyncio
    async def test_canonical_product_code_persisted_to_state(self):
        """Legacy aliases must be canonicalized before landing on the compartment."""
        from Agents.support.compartment_models import (
            CompartmentAssignment,
            LoadingPlan,
        )

        agent, _ = _make_agent()
        repo = MagicMock()
        repo.mark_loaded = AsyncMock()
        agent._compartment_state_repo = repo

        plan = LoadingPlan(
            plan_id="plan-x",
            truck_id="truck-x",
            tenant_id="tenant-1",
            assignments=[
                CompartmentAssignment(
                    compartment_id="c1",
                    station_id="s1",
                    fuel_grade="ATK",  # NG alias for KEROSENE
                    quantity_liters=1_000.0,
                    compartment_capacity_liters=2_000.0,
                ),
                CompartmentAssignment(
                    compartment_id="c2",
                    station_id="s2",
                    fuel_grade="LPG",  # NG alias for PROPANE
                    quantity_liters=500.0,
                    compartment_capacity_liters=1_000.0,
                ),
            ],
            total_utilization_pct=50.0,
        )

        await agent._persist_loading_plan(plan)

        product_codes = [
            call.kwargs["product_code"]
            for call in repo.mark_loaded.await_args_list
        ]
        assert product_codes == ["KEROSENE", "PROPANE"]


# ---------------------------------------------------------------------------
# Tests: Constructor wires CompartmentStateRepository (Task 6.6)
# ---------------------------------------------------------------------------


class TestCompartmentStateRepoWiring:
    def test_default_repo_is_constructed_from_es_service(self):
        from fuel.compartment_state_models import CompartmentStateRepository

        agent, deps = _make_agent()
        assert isinstance(agent._compartment_state_repo, CompartmentStateRepository)
        assert agent._compartment_state_repo._es is deps["es_service"]

    def test_injected_repo_is_preserved(self):
        sentinel = MagicMock()
        agent, _ = _make_agent(compartment_state_repo=sentinel)
        assert agent._compartment_state_repo is sentinel


# ---------------------------------------------------------------------------
# Tests: Shadow mode gates compartment-state write (Task 6.6 / Req 7.1.2)
# ---------------------------------------------------------------------------
#
# The spec reserves the atomic ``last_loaded_product`` / ``last_loaded_at``
# / ``state`` write for a successful assignment commit. Shadow-mode
# evaluation still runs the full optimization so the plan can be logged
# to ``agent_shadow_proposals`` for retrospective analysis, but
# ``truck_compartments`` must stay untouched until the overlay is
# flipped to an active mode.
# ---------------------------------------------------------------------------


class TestShadowModeGate:
    def _make_plan(self):
        from Agents.support.compartment_models import (
            CompartmentAssignment,
            LoadingPlan,
        )

        return LoadingPlan(
            plan_id="plan-shadow",
            truck_id="truck-shadow",
            tenant_id="tenant-1",
            assignments=[
                CompartmentAssignment(
                    compartment_id="c1",
                    station_id="s1",
                    fuel_grade="AGO",
                    quantity_liters=2_000.0,
                    compartment_capacity_liters=5_000.0,
                ),
            ],
            total_utilization_pct=40.0,
        )

    @pytest.mark.asyncio
    async def test_persist_skips_state_write_when_commit_flag_is_false(self):
        """Req 7.1.2: shadow-mode cycles must not mutate compartment state."""
        agent, deps = _make_agent()
        repo = MagicMock()
        repo.mark_loaded = AsyncMock()
        agent._compartment_state_repo = repo

        plan = self._make_plan()
        await agent._persist_loading_plan(plan, commit_compartment_state=False)

        # Plan itself still lands in mvp_load_plans for retrospective
        # analysis in shadow mode — the spec only protects compartment
        # state, not the plan log.
        assert deps["es_service"].index_document.await_count == 1
        assert deps["es_service"].index_document.await_args.args[0] == "mvp_load_plans"

        # …but the compartment state write is suppressed.
        repo.mark_loaded.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_persist_writes_state_when_commit_flag_is_true(self):
        """Active-mode cycles continue to stamp compartment state."""
        agent, _ = _make_agent()
        repo = MagicMock()
        repo.mark_loaded = AsyncMock()
        agent._compartment_state_repo = repo

        plan = self._make_plan()
        await agent._persist_loading_plan(plan, commit_compartment_state=True)

        repo.mark_loaded.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_evaluate_skips_state_write_in_shadow_mode(self):
        """End-to-end: an evaluate() cycle in shadow mode must not mark compartments."""
        agent, deps = _make_agent()
        # Replace the default is_enabled-based mock so the base overlay
        # resolves mode=='shadow' through the richer get_overlay_state
        # path used in production.
        deps["feature_flag_service"].get_overlay_state = AsyncMock(
            return_value="shadow"
        )
        repo = MagicMock()
        repo.mark_loaded = AsyncMock()
        agent._compartment_state_repo = repo

        agent._priority_buffer.append(_make_priority_list())
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        _make_compartment_hit(
                            compartment_id="comp-1",
                            truck_id="truck-1",
                            capacity_liters=10_000.0,
                        ),
                    ]
                }
            }
        )

        proposals = await agent.evaluate([])

        # The overlay still produces the proposal for shadow-log routing.
        assert len(proposals) == 1
        # …and the plan was persisted to mvp_load_plans for analysis.
        plan_calls = [
            call
            for call in deps["es_service"].index_document.await_args_list
            if call.args and call.args[0] == "mvp_load_plans"
        ]
        assert len(plan_calls) == 1
        # But the compartment-state mutation never fired.
        repo.mark_loaded.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_evaluate_writes_state_in_active_mode(self):
        """End-to-end: an active overlay commits state once per assigned compartment."""
        agent, deps = _make_agent()
        deps["feature_flag_service"].get_overlay_state = AsyncMock(
            return_value="active_auto"
        )
        repo = MagicMock()
        repo.mark_loaded = AsyncMock()
        agent._compartment_state_repo = repo

        agent._priority_buffer.append(_make_priority_list())
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        _make_compartment_hit(
                            compartment_id="comp-1",
                            truck_id="truck-1",
                            capacity_liters=10_000.0,
                        ),
                    ]
                }
            }
        )

        await agent.evaluate([])
        repo.mark_loaded.assert_awaited()

    @pytest.mark.asyncio
    async def test_mode_resolution_failure_defaults_to_no_write(self):
        """A flaky feature-flag service must fail closed (no state mutation)."""
        agent, deps = _make_agent()

        async def _boom(*args, **kwargs):
            raise RuntimeError("flags down")

        deps["feature_flag_service"].get_overlay_state = AsyncMock(side_effect=_boom)
        deps["feature_flag_service"].is_enabled = AsyncMock(side_effect=_boom)

        # The helper should swallow the error and report non-commit.
        is_active = await agent._is_active_commit_mode("tenant-1")
        assert is_active is False
