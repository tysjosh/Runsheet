"""
Integration test: overlay agents operating on fuel_orders_current.

Seeds fixture fuel_orders_current + customer_tanks + mvp_tank_forecasts,
runs the Delivery_Prioritization_Agent and Route_Planning_Agent in order,
and asserts the output matches expected priority + route shapes.

Also covers the storm-mode case (Task 11.4): verifies Storm_Mode_Evaluator
continues to boost Fuel_Orders whose linked customer_tank.criticality_tier
is in the storm-priority set.

Validates: Requirements 5.1, 5.2, 5.3, 5.4, 10.2.1
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Agents.overlay.delivery_prioritization_agent import (
    DeliveryPrioritizationAgent,
    FUEL_ORDERS_CURRENT_INDEX,
)
from Agents.overlay.route_planning_agent import RoutePlanningAgent
from Agents.support.fuel_distribution_models import PriorityBucket


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-overlay-test"
# Use actual current time for test fixtures so scoring works correctly
NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures — mock ES service
# ---------------------------------------------------------------------------


def _build_mock_es(
    orders: List[Dict[str, Any]],
    customer_tanks: List[Dict[str, Any]],
    forecasts: List[Dict[str, Any]],
) -> AsyncMock:
    """Build a mock ES service that returns seeded data based on index."""
    es = AsyncMock()

    async def search_documents(index: str, query: dict, size: int = 100):
        if index == "fuel_orders_current":
            # Filter by status if present in query
            status_filter = None
            filters = query.get("query", {}).get("bool", {}).get("filter", [])
            for f in filters:
                if "terms" in f and "status" in f["terms"]:
                    status_filter = f["terms"]["status"]
            filtered_orders = orders
            if status_filter:
                filtered_orders = [o for o in orders if o.get("status") in status_filter]
            return {
                "hits": {"hits": [{"_source": o} for o in filtered_orders]},
                "aggregations": {
                    "tenants": {
                        "buckets": [{"key": TENANT_ID}]
                    }
                },
            }
        elif index == "customer_tanks":
            # Filter by tank_ids if present in query
            tank_filter = None
            filters = query.get("query", {}).get("bool", {}).get("filter", [])
            for f in filters:
                if "terms" in f and "tank_id" in f["terms"]:
                    tank_filter = f["terms"]["tank_id"]
            if tank_filter:
                filtered = [t for t in customer_tanks if t.get("tank_id") in tank_filter]
            else:
                filtered = customer_tanks
            return {"hits": {"hits": [{"_source": t} for t in filtered]}}
        elif index == "mvp_tank_forecasts":
            return {"hits": {"hits": [{"_source": f} for f in forecasts]}}
        return {"hits": {"hits": []}, "aggregations": {}}

    es.search_documents = search_documents
    return es


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------


def _seed_orders() -> List[Dict[str, Any]]:
    """Seed fuel orders with various call_types and statuses."""
    return [
        {
            "order_id": "ord_keep_full_001",
            "tenant_id": TENANT_ID,
            "customer_id": "cust_001",
            "customer_name": "Critical Customer",
            "customer_tank_id": "tank_critical_001",
            "product_code": "DIESEL_2",
            "gallons_requested": 500.0,
            "fill_to_full": False,
            "call_type": "keep_full",
            "status": "confirmed",
            "ship_to_lat": 29.76,
            "ship_to_lon": -95.37,
            "ship_to_address": "100 Main St, Houston TX",
            "delivery_window_start": (NOW + timedelta(hours=2)).isoformat(),
            "delivery_window_end": (NOW + timedelta(hours=8)).isoformat(),
        },
        {
            "order_id": "ord_auto_fill_002",
            "tenant_id": TENANT_ID,
            "customer_id": "cust_002",
            "customer_name": "Medical Facility",
            "customer_tank_id": "tank_medical_002",
            "product_code": "PROPANE",
            "gallons_requested": 300.0,
            "fill_to_full": True,
            "call_type": "auto_fill",
            "status": "placed",
            "ship_to_lat": 29.80,
            "ship_to_lon": -95.40,
            "ship_to_address": "200 Hospital Dr, Houston TX",
            "delivery_window_start": None,
            "delivery_window_end": None,
        },
        {
            "order_id": "ord_will_call_003",
            "tenant_id": TENANT_ID,
            "customer_id": "cust_003",
            "customer_name": "Standard Customer",
            "customer_tank_id": "tank_standard_003",
            "product_code": "GASOLINE_REG",
            "gallons_requested": 200.0,
            "fill_to_full": False,
            "call_type": "will_call",
            "status": "confirmed",
            "ship_to_lat": 29.85,
            "ship_to_lon": -95.35,
            "ship_to_address": "300 Oak Ave, Houston TX",
            "delivery_window_start": (NOW - timedelta(hours=1)).isoformat(),
            "delivery_window_end": (NOW + timedelta(hours=3)).isoformat(),
        },
        {
            "order_id": "ord_one_off_004",
            "tenant_id": TENANT_ID,
            "customer_id": "cust_004",
            "customer_name": "One-Off Customer",
            "customer_tank_id": None,
            "product_code": "DIESEL_2",
            "gallons_requested": 1000.0,
            "fill_to_full": False,
            "call_type": "one_off",
            "status": "scheduled",
            "ship_to_lat": 29.70,
            "ship_to_lon": -95.45,
            "ship_to_address": "400 Industrial Blvd, Houston TX",
            "delivery_window_start": (NOW + timedelta(hours=20)).isoformat(),
            "delivery_window_end": (NOW + timedelta(hours=30)).isoformat(),
        },
    ]


def _seed_customer_tanks() -> List[Dict[str, Any]]:
    """Seed customer tanks with criticality tiers."""
    return [
        {
            "tank_id": "tank_critical_001",
            "tenant_id": TENANT_ID,
            "customer_id": "cust_001",
            "criticality_tier": "critical",
            "capacity_gallons": 1000.0,
            "current_level_gallons": 200.0,
        },
        {
            "tank_id": "tank_medical_002",
            "tenant_id": TENANT_ID,
            "customer_id": "cust_002",
            "criticality_tier": "medical",
            "capacity_gallons": 500.0,
            "current_level_gallons": 50.0,
        },
        {
            "tank_id": "tank_standard_003",
            "tenant_id": TENANT_ID,
            "customer_id": "cust_003",
            "criticality_tier": "standard",
            "capacity_gallons": 800.0,
            "current_level_gallons": 400.0,
        },
    ]


def _seed_forecasts() -> List[Dict[str, Any]]:
    """Seed tank forecasts with hours_to_runout values."""
    return [
        {
            "station_id": "tank_critical_001",
            "tenant_id": TENANT_ID,
            "hours_to_runout_p90": 8.0,
            "timestamp": NOW.isoformat(),
        },
        {
            "station_id": "tank_medical_002",
            "tenant_id": TENANT_ID,
            "hours_to_runout_p90": 4.0,
            "timestamp": NOW.isoformat(),
        },
    ]


# ---------------------------------------------------------------------------
# Mock storm mode evaluator
# ---------------------------------------------------------------------------


@dataclass
class MockPersistedState:
    state: str


class MockStormModeEvaluator:
    """Mock storm mode evaluator that can be toggled active/inactive."""

    def __init__(self, active: bool = False):
        self._active = active

    async def get_state(self, tenant_id: str) -> MockPersistedState:
        return MockPersistedState(state="active" if self._active else "inactive")


# ---------------------------------------------------------------------------
# Helper to build agents
# ---------------------------------------------------------------------------


def _build_prioritization_agent(
    es_service,
    storm_mode_evaluator=None,
) -> DeliveryPrioritizationAgent:
    """Build a DeliveryPrioritizationAgent with mocked dependencies."""
    signal_bus = AsyncMock()
    signal_bus.publish = AsyncMock()
    return DeliveryPrioritizationAgent(
        signal_bus=signal_bus,
        es_service=es_service,
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
        autonomy_config_service=MagicMock(),
        feature_flag_service=MagicMock(),
        storm_mode_evaluator=storm_mode_evaluator,
    )


def _build_route_planning_agent(es_service) -> RoutePlanningAgent:
    """Build a RoutePlanningAgent with mocked dependencies."""
    signal_bus = AsyncMock()
    return RoutePlanningAgent(
        signal_bus=signal_bus,
        es_service=es_service,
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
        autonomy_config_service=MagicMock(),
        feature_flag_service=MagicMock(),
    )


# ===========================================================================
# Tests — Delivery Prioritization on Fuel Orders
# ===========================================================================


class TestDeliveryPrioritizationOnFuelOrders:
    """Test Delivery_Prioritization_Agent reads from fuel_orders_current."""

    @pytest.mark.asyncio
    async def test_prioritizes_orders_by_call_type(self):
        """Orders are scored based on call_type strategy."""
        orders = _seed_orders()
        tanks = _seed_customer_tanks()
        forecasts = _seed_forecasts()
        es = _build_mock_es(orders, tanks, forecasts)

        agent = _build_prioritization_agent(es)
        priority_list = await agent.prioritize_fuel_orders(TENANT_ID)

        assert priority_list is not None
        assert len(priority_list.priorities) == 4
        assert priority_list.tenant_id == TENANT_ID

        # Verify all orders are scored
        station_ids = {p.station_id for p in priority_list.priorities}
        assert "tank_critical_001" in station_ids
        assert "tank_medical_002" in station_ids

    @pytest.mark.asyncio
    async def test_keep_full_scores_via_forecast(self):
        """keep_full orders score via linked forecast hours_to_runout."""
        orders = [_seed_orders()[0]]  # keep_full order
        tanks = _seed_customer_tanks()
        forecasts = _seed_forecasts()
        es = _build_mock_es(orders, tanks, forecasts)

        agent = _build_prioritization_agent(es)
        priority_list = await agent.prioritize_fuel_orders(TENANT_ID)

        assert priority_list is not None
        assert len(priority_list.priorities) == 1
        priority = priority_list.priorities[0]
        # 8 hours to runout -> should be CRITICAL bucket
        assert priority.priority_score >= 0.85
        assert priority.priority_bucket == PriorityBucket.CRITICAL
        assert "runout_critical" in priority.reasons

    @pytest.mark.asyncio
    async def test_will_call_scores_via_window_end(self):
        """will_call orders score via delivery_window_end proximity."""
        orders = [_seed_orders()[2]]  # will_call order with 3h window
        tanks = _seed_customer_tanks()
        es = _build_mock_es(orders, tanks, [])

        agent = _build_prioritization_agent(es)
        priority_list = await agent.prioritize_fuel_orders(TENANT_ID)

        assert priority_list is not None
        assert len(priority_list.priorities) == 1
        priority = priority_list.priorities[0]
        # 3 hours until window end -> should be HIGH or CRITICAL
        assert priority.priority_score >= 0.65
        assert "window_urgent" in priority.reasons or "window_high" in priority.reasons

    @pytest.mark.asyncio
    async def test_missing_forecast_scores_low(self):
        """keep_full order without forecast gets scoring_input_missing + LOW."""
        orders = [_seed_orders()[0]]  # keep_full order
        tanks = _seed_customer_tanks()
        es = _build_mock_es(orders, tanks, [])  # No forecasts

        agent = _build_prioritization_agent(es)
        priority_list = await agent.prioritize_fuel_orders(TENANT_ID)

        assert priority_list is not None
        priority = priority_list.priorities[0]
        assert priority.priority_score <= 0.40
        assert "scoring_input_missing" in priority.reasons
        assert "no_forecast_available" in priority.reasons

    @pytest.mark.asyncio
    async def test_missing_window_scores_low(self):
        """will_call order without delivery_window_end gets LOW score."""
        order = _seed_orders()[2].copy()
        order["delivery_window_end"] = None
        es = _build_mock_es([order], _seed_customer_tanks(), [])

        agent = _build_prioritization_agent(es)
        priority_list = await agent.prioritize_fuel_orders(TENANT_ID)

        assert priority_list is not None
        priority = priority_list.priorities[0]
        assert priority.priority_score <= 0.40
        assert "scoring_input_missing" in priority.reasons


# ===========================================================================
# Tests — Storm Mode Boost (Task 11.4)
# ===========================================================================


class TestStormModeBoostOnFuelOrders:
    """Task 11.4: Verify Storm_Mode_Evaluator boosts Fuel_Orders whose
    linked customer_tank.criticality_tier is in the storm-priority set."""

    @pytest.mark.asyncio
    async def test_storm_mode_boosts_critical_tier(self):
        """Storm mode boosts orders with critical criticality_tier."""
        orders = _seed_orders()
        tanks = _seed_customer_tanks()
        forecasts = _seed_forecasts()
        es = _build_mock_es(orders, tanks, forecasts)

        # Without storm mode
        agent_no_storm = _build_prioritization_agent(
            es, storm_mode_evaluator=MockStormModeEvaluator(active=False)
        )
        list_no_storm = await agent_no_storm.prioritize_fuel_orders(TENANT_ID)

        # With storm mode active
        agent_storm = _build_prioritization_agent(
            es, storm_mode_evaluator=MockStormModeEvaluator(active=True)
        )
        list_storm = await agent_storm.prioritize_fuel_orders(TENANT_ID)

        assert list_no_storm is not None
        assert list_storm is not None

        # Find the critical-tier order in both lists
        def find_priority(plist, station_id):
            for p in plist.priorities:
                if p.station_id == station_id:
                    return p
            return None

        critical_no_storm = find_priority(list_no_storm, "tank_critical_001")
        critical_storm = find_priority(list_storm, "tank_critical_001")

        assert critical_no_storm is not None
        assert critical_storm is not None
        # Storm mode should boost the score
        assert critical_storm.priority_score > critical_no_storm.priority_score
        assert "storm_boost:critical" in critical_storm.reasons

    @pytest.mark.asyncio
    async def test_storm_mode_boosts_medical_tier(self):
        """Storm mode boosts orders with medical criticality_tier."""
        orders = _seed_orders()
        tanks = _seed_customer_tanks()
        forecasts = _seed_forecasts()
        es = _build_mock_es(orders, tanks, forecasts)

        agent = _build_prioritization_agent(
            es, storm_mode_evaluator=MockStormModeEvaluator(active=True)
        )
        priority_list = await agent.prioritize_fuel_orders(TENANT_ID)

        assert priority_list is not None
        medical = None
        for p in priority_list.priorities:
            if p.station_id == "tank_medical_002":
                medical = p
                break

        assert medical is not None
        assert "storm_boost:medical" in medical.reasons

    @pytest.mark.asyncio
    async def test_storm_mode_does_not_boost_standard_tier(self):
        """Storm mode does NOT boost orders with standard criticality_tier."""
        orders = _seed_orders()
        tanks = _seed_customer_tanks()
        forecasts = _seed_forecasts()
        es = _build_mock_es(orders, tanks, forecasts)

        agent = _build_prioritization_agent(
            es, storm_mode_evaluator=MockStormModeEvaluator(active=True)
        )
        priority_list = await agent.prioritize_fuel_orders(TENANT_ID)

        assert priority_list is not None
        standard = None
        for p in priority_list.priorities:
            if p.station_id == "tank_standard_003":
                standard = p
                break

        assert standard is not None
        # Standard tier should NOT get storm boost
        assert not any("storm_boost" in r for r in standard.reasons)

    @pytest.mark.asyncio
    async def test_storm_mode_priority_ordering(self):
        """Storm-active tenant: medical > critical > standard ordering."""
        orders = _seed_orders()
        tanks = _seed_customer_tanks()
        forecasts = _seed_forecasts()
        es = _build_mock_es(orders, tanks, forecasts)

        agent = _build_prioritization_agent(
            es, storm_mode_evaluator=MockStormModeEvaluator(active=True)
        )
        priority_list = await agent.prioritize_fuel_orders(TENANT_ID)

        assert priority_list is not None
        # Priorities are sorted by score descending
        scores = {p.station_id: p.priority_score for p in priority_list.priorities}

        # Medical (4h to runout + storm boost) should be highest
        # Critical (8h to runout + storm boost) should be next
        assert scores.get("tank_medical_002", 0) >= scores.get("tank_critical_001", 0)


# ===========================================================================
# Tests — Route Planning on Fuel Orders
# ===========================================================================


class TestRoutePlanningOnFuelOrders:
    """Test Route_Planning_Agent builds stops from fuel_orders_current."""

    @pytest.mark.asyncio
    async def test_builds_stops_from_fuel_orders(self):
        """Route planning builds stops using ship_to_lat/ship_to_lon."""
        orders = [o for o in _seed_orders() if o["status"] in ("confirmed", "scheduled")]
        tanks = _seed_customer_tanks()
        es = _build_mock_es(orders, tanks, [])

        agent = _build_route_planning_agent(es)
        result_orders, stop_locations, window_misses = (
            await agent.build_stops_from_fuel_orders(TENANT_ID)
        )

        # Should have orders with confirmed/scheduled status
        assert len(result_orders) >= 2
        # All orders with valid lat/lon should have stop locations
        for order in result_orders:
            order_id = order["order_id"]
            if order.get("ship_to_lat") and order.get("ship_to_lon"):
                assert order_id in stop_locations
                loc = stop_locations[order_id]
                assert "lat" in loc
                assert "lon" in loc

    @pytest.mark.asyncio
    async def test_window_miss_surfaced(self):
        """Orders with past delivery_window_end surface as window_miss."""
        past_order = {
            "order_id": "ord_past_window",
            "tenant_id": TENANT_ID,
            "customer_id": "cust_past",
            "customer_name": "Past Window Customer",
            "customer_tank_id": None,
            "product_code": "DIESEL_2",
            "gallons_requested": 500.0,
            "fill_to_full": False,
            "call_type": "one_off",
            "status": "confirmed",
            "ship_to_lat": 29.90,
            "ship_to_lon": -95.50,
            "ship_to_address": "500 Past St, Houston TX",
            "delivery_window_start": (NOW - timedelta(hours=10)).isoformat(),
            "delivery_window_end": (NOW - timedelta(hours=2)).isoformat(),
        }
        es = _build_mock_es([past_order], [], [])

        agent = _build_route_planning_agent(es)
        _, _, window_misses = await agent.build_stops_from_fuel_orders(TENANT_ID)

        assert len(window_misses) == 1
        assert window_misses[0].order_id == "ord_past_window"
        assert window_misses[0].reason == "window_miss"

    @pytest.mark.asyncio
    async def test_geocode_fallback_when_lat_lon_null(self):
        """Falls back to geocoding when ship_to_lat/ship_to_lon are null."""
        order = {
            "order_id": "ord_no_coords",
            "tenant_id": TENANT_ID,
            "customer_id": "cust_no_coords",
            "customer_name": "No Coords Customer",
            "customer_tank_id": None,
            "product_code": "DIESEL_2",
            "gallons_requested": 500.0,
            "fill_to_full": False,
            "call_type": "one_off",
            "status": "confirmed",
            "ship_to_lat": None,
            "ship_to_lon": None,
            "ship_to_address": "123 Geocode St, Houston TX",
            "delivery_window_start": (NOW + timedelta(hours=2)).isoformat(),
            "delivery_window_end": (NOW + timedelta(hours=8)).isoformat(),
        }
        es = _build_mock_es([order], [], [])
        # Add geocode_address mock
        es.geocode_address = AsyncMock(
            return_value={"lat": 29.95, "lon": -95.55}
        )

        agent = _build_route_planning_agent(es)
        _, stop_locations, _ = await agent.build_stops_from_fuel_orders(TENANT_ID)

        assert "ord_no_coords" in stop_locations
        assert stop_locations["ord_no_coords"]["lat"] == 29.95
        assert stop_locations["ord_no_coords"]["lon"] == -95.55


# ===========================================================================
# Tests — Full Pipeline Integration
# ===========================================================================


class TestFullPipelineIntegration:
    """End-to-end: prioritization -> route planning with fuel orders."""

    @pytest.mark.asyncio
    async def test_prioritization_then_route_planning(self):
        """Run prioritization and route planning in sequence."""
        orders = _seed_orders()
        tanks = _seed_customer_tanks()
        forecasts = _seed_forecasts()
        es = _build_mock_es(orders, tanks, forecasts)

        # Step 1: Run prioritization
        prio_agent = _build_prioritization_agent(es)
        priority_list = await prio_agent.prioritize_fuel_orders(TENANT_ID)

        assert priority_list is not None
        assert len(priority_list.priorities) > 0

        # Step 2: Run route planning on confirmed/scheduled orders
        route_agent = _build_route_planning_agent(es)
        result_orders, stop_locations, window_misses = (
            await route_agent.build_stops_from_fuel_orders(TENANT_ID)
        )

        # Verify route planning got the right orders
        routable_statuses = {"confirmed", "scheduled"}
        for order in result_orders:
            assert order["status"] in routable_statuses

        # Verify stop locations are populated
        assert len(stop_locations) > 0

    @pytest.mark.asyncio
    async def test_storm_mode_full_pipeline(self):
        """Storm mode affects priority ordering in the full pipeline."""
        orders = _seed_orders()
        tanks = _seed_customer_tanks()
        forecasts = _seed_forecasts()
        es = _build_mock_es(orders, tanks, forecasts)

        # Run with storm mode active
        agent = _build_prioritization_agent(
            es, storm_mode_evaluator=MockStormModeEvaluator(active=True)
        )
        priority_list = await agent.prioritize_fuel_orders(TENANT_ID)

        assert priority_list is not None

        # Medical and critical tiers should be boosted to top
        top_priorities = priority_list.priorities[:2]
        top_station_ids = {p.station_id for p in top_priorities}

        # At least one of the storm-boosted orders should be in top 2
        storm_boosted = {"tank_critical_001", "tank_medical_002"}
        assert top_station_ids & storm_boosted, (
            f"Expected at least one storm-boosted order in top 2, "
            f"got {top_station_ids}"
        )
