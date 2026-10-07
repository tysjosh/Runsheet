"""Dispatch validation helpers shared by the route planning agent and the Dispatch Board.

Dispatch-board design K3 (task 6). These were private methods on
``Agents.overlay.route_planning_agent.RoutePlanningAgent``; they are module
functions here so the board's ``DispatchValidationService`` (task 10) and the
agent compute route requirements, route-hour estimates and stop locations the
same way. ``RoutePlanningAgent`` calls these; its behaviour is unchanged, which
the golden tests in ``tests/unit/test_dispatch_validation_helpers.py`` pin.

Pure functions, no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from fuel.services.fuel_product_catalog import UnknownFuelProductError, canonicalize

#: Product categories that require a HAZMAT endorsement per DOT/FMCSA
#: regulations when transported in bulk. All petroleum fuels and LPG are
#: Class 3 (flammable liquids) or Class 2.1 (flammable gas).
HAZMAT_CATEGORIES: Tuple[str, ...] = (
    "diesel",
    "gasoline",
    "propane",
    "kerosene",
    "heating_oil",
    "off_road",
    "ethanol",
)

#: Average speed in mph used to estimate drive hours from route distance. Fuel
#: delivery trucks in urban/suburban areas average roughly 25 mph including
#: stops and traffic.
AVERAGE_SPEED_MPH: float = 25.0

#: Average time per delivery stop in hours (loading, unloading, paperwork,
#: safety checks). Used to estimate total on-duty hours.
HOURS_PER_STOP: float = 0.5

#: Average distance between stops in miles (used when actual route distance is
#: not yet computed). Conservative estimate for fuel delivery routes.
AVG_MILES_BETWEEN_STOPS: float = 15.0


def lookup_product(product_code: str) -> Optional[Any]:
    """Look up a FuelProduct from the catalog by product_code."""
    from fuel.services.fuel_product_catalog import FUEL_PRODUCT_CATALOG

    for product in FUEL_PRODUCT_CATALOG:
        if product.product_code == product_code:
            return product
    return None


def build_route_requirements(
    assignments: List[Dict[str, Any]],
    *,
    hazmat_categories: Sequence[str] = HAZMAT_CATEGORIES,
) -> Dict[str, Any]:
    """Derive route requirements from loading plan assignments.

    Inspects the fuel grades/product codes in the assignments to determine:

    - ``requires_hazmat``: True if any assignment carries a HAZMAT-classified
      product (all bulk petroleum fuels). An unidentifiable grade counts as
      HAZMAT (conservative).
    - ``requires_tanker``: True if any assignment is present (all fuel
      deliveries use cargo tank vehicles).
    - ``min_cdl_class``: "A" for cargo tank vehicles (standard for fuel tanker
      trucks in the US).
    """
    requires_hazmat = False
    requires_tanker = bool(assignments)  # All fuel deliveries use tankers

    for assignment in assignments:
        fuel_grade = assignment.get("fuel_grade", "")
        if not fuel_grade:
            continue
        try:
            product_code = canonicalize(fuel_grade)
            product = lookup_product(product_code)
            if product and product.category in hazmat_categories:
                requires_hazmat = True
                break
        except (UnknownFuelProductError, Exception):
            # If we can't identify the product, assume HAZMAT for safety
            # (conservative approach for unknown fuels).
            requires_hazmat = True
            break

    return {
        "requires_hazmat": requires_hazmat,
        "requires_tanker": requires_tanker,
        "min_cdl_class": "A" if requires_tanker else None,
    }


def estimate_route_hours(
    assignments: List[Dict[str, Any]],
    *,
    average_speed_mph: float = AVERAGE_SPEED_MPH,
    hours_per_stop: float = HOURS_PER_STOP,
    avg_miles_between_stops: float = AVG_MILES_BETWEEN_STOPS,
) -> Tuple[float, float]:
    """Estimate drive hours and total on-duty hours for a route.

    - Drive hours = (num_stops + 1) * avg_miles_between_stops / avg_speed_mph
      (depot → stops → depot)
    - Total hours = drive_hours + num_stops * hours_per_stop

    Returns ``(estimated_drive_hours, estimated_total_hours)``.
    """
    num_stops = len(assignments)
    if num_stops == 0:
        return (0.0, 0.0)

    total_miles = (num_stops + 1) * avg_miles_between_stops
    estimated_drive_hours = total_miles / average_speed_mph
    estimated_total_hours = estimated_drive_hours + (num_stops * hours_per_stop)
    return (estimated_drive_hours, estimated_total_hours)


def resolve_stop_locations(
    *,
    station_ids: List[str],
    station_locations: Dict[str, Dict[str, float]],
    order_ids_by_station: Dict[str, List[str]],
    order_stop_locations: Dict[str, Dict[str, float]],
) -> Dict[str, Dict[str, float]]:
    """Merge order-derived and station-derived stop coordinates.

    Order coordinates win: they come from the order's own ship-to
    (``ship_to_lat``/``ship_to_lon``, or a geocoded ``ship_to_address``) and
    therefore work whether the demand behind the stop is a retail
    ``fuel_stations`` document or a ``customer_tanks`` row. ``fuel_stations``
    remains the fallback so legacy retail tenants — whose orders may carry no
    coordinates at all — keep routing.
    """
    resolved: Dict[str, Dict[str, float]] = {}
    for station_id in station_ids:
        for order_id in order_ids_by_station.get(station_id, []):
            order_location = order_stop_locations.get(order_id)
            if order_location:
                resolved[station_id] = dict(order_location)
                break
        if station_id in resolved:
            continue
        station_location = station_locations.get(station_id)
        if station_location:
            resolved[station_id] = dict(station_location)
    return resolved
