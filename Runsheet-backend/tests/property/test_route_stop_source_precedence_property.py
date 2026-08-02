"""
Property-based test for Route_Planning_Agent stop-source precedence.

# Feature: fuel-ops-hardening, Property: Stop-Source Precedence

**Validates: Requirements 4.3, 5.2.2**

``RoutePlanningAgent._resolve_stop_locations`` merges order-derived stop
coordinates (from ``fuel_orders_current.ship_to_*``) over station-derived
ones (from ``fuel_stations``). For any combination of resolvable orders
and station documents the merge must satisfy:

1. **Order precedence** — if any of a stop's joined orders has a
   coordinate, the resolved coordinate is one of those order coordinates.
2. **Station fallback** — if none of the stop's orders resolves but a
   station document does, the resolved coordinate is the station's.
3. **No fabrication** — a stop with neither source is absent from the
   result, and no resolved coordinate is invented.
"""
from typing import Dict, List
from unittest.mock import AsyncMock, MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from Agents.overlay.route_planning_agent import RoutePlanningAgent


# ---------------------------------------------------------------------------
# Strategies — constrained to the real input space: stop ids come from a
# small alphabet so orders and stations actually collide, and coordinates
# are valid WGS84 degrees.
# ---------------------------------------------------------------------------

_stop_ids = st.sampled_from(["s1", "s2", "s3", "s4"])
_order_ids = st.sampled_from(["o1", "o2", "o3", "o4", "o5"])
_coords = st.fixed_dictionaries(
    {
        "lat": st.floats(-90.0, 90.0, allow_nan=False, allow_infinity=False),
        "lon": st.floats(-180.0, 180.0, allow_nan=False, allow_infinity=False),
    }
)


def _agent() -> RoutePlanningAgent:
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    return RoutePlanningAgent(
        signal_bus=signal_bus,
        es_service=es,
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
        autonomy_config_service=MagicMock(),
        feature_flag_service=MagicMock(),
    )


class TestStopSourcePrecedence:
    """**Validates: Requirements 4.3, 5.2.2**"""

    @given(
        station_ids=st.lists(_stop_ids, min_size=1, max_size=4, unique=True),
        station_locations=st.dictionaries(_stop_ids, _coords, max_size=4),
        order_ids_by_station=st.dictionaries(
            _stop_ids,
            st.lists(_order_ids, max_size=3, unique=True),
            max_size=4,
        ),
        order_stop_locations=st.dictionaries(_order_ids, _coords, max_size=5),
    )
    @settings(max_examples=300)
    def test_order_wins_station_falls_back_nothing_is_invented(
        self,
        station_ids: List[str],
        station_locations: Dict[str, Dict[str, float]],
        order_ids_by_station: Dict[str, List[str]],
        order_stop_locations: Dict[str, Dict[str, float]],
    ):
        """**Validates: Requirements 4.3, 5.2.2**"""
        resolved = RoutePlanningAgent._resolve_stop_locations(
            station_ids=station_ids,
            station_locations=station_locations,
            order_ids_by_station=order_ids_by_station,
            order_stop_locations=order_stop_locations,
        )

        # No stop outside the requested set is ever produced.
        assert set(resolved) <= set(station_ids)

        for station_id in station_ids:
            order_coords = [
                order_stop_locations[oid]
                for oid in order_ids_by_station.get(station_id, [])
                if oid in order_stop_locations
            ]
            station_coord = station_locations.get(station_id)

            if order_coords:
                # 1. Order precedence.
                assert resolved[station_id] in order_coords
            elif station_coord is not None:
                # 2. Station fallback.
                assert resolved[station_id] == station_coord
            else:
                # 3. No fabrication.
                assert station_id not in resolved
