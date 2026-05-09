"""
Unit tests for Route_Planning_Agent fuel-order stop building (Task 11.2).

Validates: Requirements 5.2.1, 5.2.2, 5.2.3
- Builds stops from fuel_orders_current WHERE status IN {confirmed, scheduled}
- Uses ship_to_lat/ship_to_lon as stop coordinate, falls back to geocoding
- Treats delivery_window_start/delivery_window_end as hard routing constraints
- Surfaces windows that cannot be satisfied as window_miss entries
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Agents.overlay.route_planning_agent import (
    FUEL_ORDERS_CURRENT_INDEX,
    RoutePlanningAgent,
)
from Agents.support.fuel_distribution_models import WindowMissEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_agent(es_service=None) -> RoutePlanningAgent:
    """Build a minimal RoutePlanningAgent with mocked dependencies."""
    signal_bus = MagicMock()
    signal_bus.subscribe = MagicMock()
    es = es_service or AsyncMock()
    activity_log = MagicMock()
    ws_manager = MagicMock()
    confirmation_protocol = MagicMock()
    autonomy_config = MagicMock()
    feature_flags = MagicMock()

    agent = RoutePlanningAgent(
        signal_bus=signal_bus,
        es_service=es,
        activity_log_service=activity_log,
        ws_manager=ws_manager,
        confirmation_protocol=confirmation_protocol,
        autonomy_config_service=autonomy_config,
        feature_flag_service=feature_flags,
    )
    return agent


def _make_order(
    order_id: str = "ord_abc123",
    tenant_id: str = "tenant-1",
    status: str = "confirmed",
    ship_to_lat: Optional[float] = 32.7767,
    ship_to_lon: Optional[float] = -96.7970,
    ship_to_address: str = "123 Main St, Dallas, TX",
    delivery_window_start: Optional[str] = None,
    delivery_window_end: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Build a minimal fuel order document."""
    doc: Dict[str, Any] = {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "status": status,
        "ship_to_lat": ship_to_lat,
        "ship_to_lon": ship_to_lon,
        "ship_to_address": ship_to_address,
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
    }
    if delivery_window_start is not None:
        doc["delivery_window_start"] = delivery_window_start
    if delivery_window_end is not None:
        doc["delivery_window_end"] = delivery_window_end
    doc.update(kwargs)
    return doc


def _es_response(orders: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wrap orders in an ES search response shape."""
    return {
        "hits": {
            "hits": [{"_source": order} for order in orders],
        }
    }


# ---------------------------------------------------------------------------
# Tests: _fetch_routable_orders (Req 5.2.1)
# ---------------------------------------------------------------------------


class TestFetchRoutableOrders:
    """Verify the ES query targets fuel_orders_current with correct filters."""

    @pytest.mark.asyncio
    async def test_queries_confirmed_and_scheduled_statuses(self):
        """Req 5.2.1: reads WHERE status IN {confirmed, scheduled}."""
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response([]))
        agent = _make_agent(es_service=es)

        await agent._fetch_routable_orders("tenant-1")

        es.search_documents.assert_called_once()
        call_args = es.search_documents.call_args
        index = call_args[0][0]
        query = call_args[0][1]

        assert index == FUEL_ORDERS_CURRENT_INDEX
        # Verify the query filters by tenant_id and status
        filters = query["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "tenant-1"}} in filters
        assert {"terms": {"status": ["confirmed", "scheduled"]}} in filters

    @pytest.mark.asyncio
    async def test_returns_source_docs(self):
        """Returns the _source from each hit."""
        orders = [
            _make_order(order_id="ord_1"),
            _make_order(order_id="ord_2", status="scheduled"),
        ]
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response(orders))
        agent = _make_agent(es_service=es)

        result = await agent._fetch_routable_orders("tenant-1")

        assert len(result) == 2
        assert result[0]["order_id"] == "ord_1"
        assert result[1]["order_id"] == "ord_2"

    @pytest.mark.asyncio
    async def test_returns_empty_on_es_error(self):
        """Gracefully returns empty list on ES failure."""
        es = AsyncMock()
        es.search_documents = AsyncMock(side_effect=RuntimeError("ES down"))
        agent = _make_agent(es_service=es)

        result = await agent._fetch_routable_orders("tenant-1")

        assert result == []


# ---------------------------------------------------------------------------
# Tests: build_stops_from_fuel_orders — coordinate resolution (Req 5.2.2)
# ---------------------------------------------------------------------------


class TestBuildStopsCoordinates:
    """Verify ship_to_lat/lon usage and geocoding fallback."""

    @pytest.mark.asyncio
    async def test_uses_ship_to_lat_lon_as_stop_coordinate(self):
        """Req 5.2.2: uses ship_to_lat/ship_to_lon as the stop coordinate."""
        orders = [_make_order(ship_to_lat=33.0, ship_to_lon=-97.0)]
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response(orders))
        agent = _make_agent(es_service=es)

        _, stop_locations, _ = await agent.build_stops_from_fuel_orders(
            "tenant-1"
        )

        assert "ord_abc123" in stop_locations
        assert stop_locations["ord_abc123"] == {"lat": 33.0, "lon": -97.0}

    @pytest.mark.asyncio
    async def test_falls_back_to_geocoding_when_lat_lon_null(self):
        """Req 5.2.2: falls back to geocoding ship_to_address when null."""
        orders = [
            _make_order(
                ship_to_lat=None,
                ship_to_lon=None,
                ship_to_address="456 Oak Ave, Houston, TX",
            )
        ]
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response(orders))
        # Mock the geocoding hook
        es.geocode_address = AsyncMock(
            return_value={"lat": 29.7604, "lon": -95.3698}
        )
        agent = _make_agent(es_service=es)

        _, stop_locations, _ = await agent.build_stops_from_fuel_orders(
            "tenant-1"
        )

        assert "ord_abc123" in stop_locations
        assert stop_locations["ord_abc123"] == {"lat": 29.7604, "lon": -95.3698}
        es.geocode_address.assert_called_once_with("456 Oak Ave, Houston, TX")

    @pytest.mark.asyncio
    async def test_falls_back_to_geocoding_when_lat_lon_zero(self):
        """Zero coordinates trigger geocoding fallback."""
        orders = [_make_order(ship_to_lat=0.0, ship_to_lon=0.0)]
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response(orders))
        es.geocode_address = AsyncMock(
            return_value={"lat": 30.0, "lon": -90.0}
        )
        agent = _make_agent(es_service=es)

        _, stop_locations, _ = await agent.build_stops_from_fuel_orders(
            "tenant-1"
        )

        assert "ord_abc123" in stop_locations
        assert stop_locations["ord_abc123"] == {"lat": 30.0, "lon": -90.0}

    @pytest.mark.asyncio
    async def test_no_location_when_geocoding_unavailable(self):
        """Order excluded from stop_locations when geocoding is unavailable."""
        orders = [_make_order(ship_to_lat=None, ship_to_lon=None)]
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response(orders))
        # No geocode_address attribute on ES service
        agent = _make_agent(es_service=es)

        _, stop_locations, _ = await agent.build_stops_from_fuel_orders(
            "tenant-1"
        )

        assert "ord_abc123" not in stop_locations

    @pytest.mark.asyncio
    async def test_geocoding_failure_excludes_order(self):
        """Order excluded from stop_locations when geocoding raises."""
        orders = [_make_order(ship_to_lat=None, ship_to_lon=None)]
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response(orders))
        es.geocode_address = AsyncMock(side_effect=RuntimeError("geocode fail"))
        agent = _make_agent(es_service=es)

        _, stop_locations, _ = await agent.build_stops_from_fuel_orders(
            "tenant-1"
        )

        assert "ord_abc123" not in stop_locations


# ---------------------------------------------------------------------------
# Tests: build_stops_from_fuel_orders — window constraints (Req 5.2.3)
# ---------------------------------------------------------------------------


class TestBuildStopsWindowConstraints:
    """Verify delivery windows are treated as hard constraints."""

    @pytest.mark.asyncio
    async def test_past_window_surfaces_as_window_miss(self):
        """Req 5.2.3: window_end in the past → window_miss entry."""
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        past_start = (
            datetime.now(timezone.utc) - timedelta(hours=4)
        ).isoformat()
        orders = [
            _make_order(
                delivery_window_start=past_start,
                delivery_window_end=past,
            )
        ]
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response(orders))
        agent = _make_agent(es_service=es)

        _, _, window_misses = await agent.build_stops_from_fuel_orders(
            "tenant-1"
        )

        assert len(window_misses) == 1
        wm = window_misses[0]
        assert isinstance(wm, WindowMissEntry)
        assert wm.order_id == "ord_abc123"
        assert wm.reason == "window_miss"
        assert "past" in wm.detail

    @pytest.mark.asyncio
    async def test_narrow_window_surfaces_as_window_miss(self):
        """Req 5.2.3: window ending in < 30 min → window_miss entry."""
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(minutes=10)).isoformat()  # Only 10 min left
        orders = [
            _make_order(
                delivery_window_start=start,
                delivery_window_end=end,
            )
        ]
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response(orders))
        agent = _make_agent(es_service=es)

        _, _, window_misses = await agent.build_stops_from_fuel_orders(
            "tenant-1"
        )

        assert len(window_misses) == 1
        wm = window_misses[0]
        assert wm.order_id == "ord_abc123"
        assert wm.reason == "window_miss"
        assert "30 minutes" in wm.detail

    @pytest.mark.asyncio
    async def test_future_window_no_miss(self):
        """Orders with satisfiable future windows produce no window_miss."""
        now = datetime.now(timezone.utc)
        start = (now + timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=4)).isoformat()
        orders = [
            _make_order(
                delivery_window_start=start,
                delivery_window_end=end,
            )
        ]
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response(orders))
        agent = _make_agent(es_service=es)

        _, _, window_misses = await agent.build_stops_from_fuel_orders(
            "tenant-1"
        )

        assert len(window_misses) == 0

    @pytest.mark.asyncio
    async def test_no_window_no_miss(self):
        """Orders without delivery windows produce no window_miss."""
        orders = [_make_order()]  # No window fields
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response(orders))
        agent = _make_agent(es_service=es)

        _, _, window_misses = await agent.build_stops_from_fuel_orders(
            "tenant-1"
        )

        assert len(window_misses) == 0

    @pytest.mark.asyncio
    async def test_multiple_orders_mixed_windows(self):
        """Mix of satisfiable and unsatisfiable windows."""
        now = datetime.now(timezone.utc)
        past_end = (now - timedelta(hours=1)).isoformat()
        past_start = (now - timedelta(hours=3)).isoformat()
        future_start = (now + timedelta(hours=1)).isoformat()
        future_end = (now + timedelta(hours=4)).isoformat()

        orders = [
            _make_order(
                order_id="ord_past",
                delivery_window_start=past_start,
                delivery_window_end=past_end,
            ),
            _make_order(
                order_id="ord_future",
                delivery_window_start=future_start,
                delivery_window_end=future_end,
            ),
            _make_order(order_id="ord_no_window"),
        ]
        es = AsyncMock()
        es.search_documents = AsyncMock(return_value=_es_response(orders))
        agent = _make_agent(es_service=es)

        _, _, window_misses = await agent.build_stops_from_fuel_orders(
            "tenant-1"
        )

        assert len(window_misses) == 1
        assert window_misses[0].order_id == "ord_past"


# ---------------------------------------------------------------------------
# Tests: WindowMissEntry model
# ---------------------------------------------------------------------------


class TestWindowMissEntryModel:
    """Verify the WindowMissEntry model structure."""

    def test_default_reason(self):
        """Default reason is 'window_miss'."""
        entry = WindowMissEntry(order_id="ord_1")
        assert entry.reason == "window_miss"

    def test_full_construction(self):
        """All fields can be populated."""
        entry = WindowMissEntry(
            order_id="ord_1",
            reason="window_miss",
            delivery_window_start="2025-01-01T08:00:00Z",
            delivery_window_end="2025-01-01T12:00:00Z",
            detail="delivery_window_end is in the past",
        )
        assert entry.order_id == "ord_1"
        assert entry.delivery_window_start == "2025-01-01T08:00:00Z"
        assert entry.delivery_window_end == "2025-01-01T12:00:00Z"
        assert entry.detail == "delivery_window_end is in the past"

    def test_serialization_roundtrip(self):
        """model_dump → model_validate round-trip."""
        entry = WindowMissEntry(
            order_id="ord_1",
            delivery_window_start="2025-01-01T08:00:00Z",
            delivery_window_end="2025-01-01T12:00:00Z",
            detail="test detail",
        )
        dumped = entry.model_dump(mode="json")
        restored = WindowMissEntry.model_validate(dumped)
        assert restored == entry


# ---------------------------------------------------------------------------
# Tests: RoutePlan.window_misses field
# ---------------------------------------------------------------------------


class TestRoutePlanWindowMisses:
    """Verify RoutePlan carries window_misses."""

    def test_default_empty(self):
        """RoutePlan.window_misses defaults to empty list."""
        from Agents.support.fuel_distribution_models import RoutePlan, RouteStop

        plan = RoutePlan(
            truck_id="truck-1",
            plan_id="plan-1",
            stops=[RouteStop(station_id="s1", eta="2025-01-01T10:00:00Z", drop={}, sequence=0)],
            distance_km=10.0,
            eta_confidence=0.8,
            tenant_id="tenant-1",
        )
        assert plan.window_misses == []

    def test_with_window_misses(self):
        """RoutePlan can carry window_miss entries."""
        from Agents.support.fuel_distribution_models import RoutePlan, RouteStop

        wm = WindowMissEntry(
            order_id="ord_1",
            delivery_window_start="2025-01-01T08:00:00Z",
            delivery_window_end="2025-01-01T12:00:00Z",
            detail="window expired",
        )
        plan = RoutePlan(
            truck_id="truck-1",
            plan_id="plan-1",
            stops=[RouteStop(station_id="s1", eta="2025-01-01T10:00:00Z", drop={}, sequence=0)],
            distance_km=10.0,
            eta_confidence=0.8,
            tenant_id="tenant-1",
            window_misses=[wm],
        )
        assert len(plan.window_misses) == 1
        assert plan.window_misses[0].order_id == "ord_1"


# ---------------------------------------------------------------------------
# Tests: ReplanDiff.window_misses field
# ---------------------------------------------------------------------------


class TestReplanDiffWindowMisses:
    """Verify ReplanDiff carries window_misses."""

    def test_default_empty(self):
        """ReplanDiff.window_misses defaults to empty list."""
        from Agents.support.fuel_distribution_models import ReplanDiff

        diff = ReplanDiff()
        assert diff.window_misses == []

    def test_with_window_misses(self):
        """ReplanDiff can carry window_miss entries."""
        from Agents.support.fuel_distribution_models import ReplanDiff

        wm = WindowMissEntry(
            order_id="ord_1",
            detail="window expired",
        )
        diff = ReplanDiff(window_misses=[wm])
        assert len(diff.window_misses) == 1
        assert diff.window_misses[0].order_id == "ord_1"
