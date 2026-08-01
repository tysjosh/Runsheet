"""Per-grade volume conversion — the single named gallons/litres boundary.

US gallons is canonical on every driver-facing contract; litres stay canonical
inside ``mvp_plan_executions``. That split is forced by the planned-side models
(``RouteStop.drop`` and ``CompartmentAssignment.quantity_liters`` are litres),
so a conversion has to happen somewhere. This module is that somewhere, and it
is the only place it happens on the check-in path:

* ``us_gallons_to_liters`` is called **exactly once**, from the driver check-in
  request handler in ``Agents/support/mvp_endpoints.py``, before
  ``PlanExecutionService.record_checkin`` (Requirement 6.18).
* ``liters_to_us_gallons`` is called at the response boundary only
  (Requirement 6.23).
* ``Agents/support/plan_execution_service.py`` imports neither function and
  performs no conversion, so the storage layer cannot convert even by accident
  (Requirement 6.19). Every value it writes to ``planned_quantities`` and
  ``actual_quantities`` is litres, and every variance is computed with both
  operands in litres.

Both functions map per-grade quantity dictionaries (fuel grade → quantity), the
shape ``CheckinRequest.actual_quantities_gallons`` and the persisted
``actual_quantities`` field both use.

Relationship to ``services/unit_conversion.py``: that module converts *scalar*
values between a tenant's display units and the platform's canonical units,
where canonical volume is gallons. This module converts *per-grade mappings*
across the one storage boundary where litres are canonical. The two are distinct
concerns, so the numeric definition is shared rather than duplicated —
``LITERS_PER_US_GALLON`` is bound to ``unit_conversion.GAL_TO_L``, which means
there is exactly one gallon/litre constant in the backend and it cannot drift.

Validates: Requirements 6.18, 6.19.
"""
from __future__ import annotations

from typing import Dict, Final, Mapping

from services.unit_conversion import GAL_TO_L

#: Exact US-liquid-gallon definition, 3.785411784 litres (NIST Handbook 44).
#: Bound to ``services.unit_conversion.GAL_TO_L`` so the backend holds a single
#: gallon/litre constant. The value matches ``LITERS_PER_GALLON`` in
#: ``runsheet/src/services/fuelApi.ts:39`` exactly, so the web and mobile
#: volume boundaries cannot drift apart.
LITERS_PER_US_GALLON: Final[float] = GAL_TO_L


def us_gallons_to_liters(
    quantities_gallons: Mapping[str, float],
) -> Dict[str, float]:
    """Convert a per-grade US-gallon mapping to litres (R6.18).

    This is the ONLY gallons→litres conversion on the check-in request path. It
    is called exactly once, from the driver check-in request handler in
    ``Agents/support/mvp_endpoints.py``, before
    ``PlanExecutionService.record_checkin``.

    Args:
        quantities_gallons: Fuel grade → quantity in US gallons.

    Returns:
        A new dict with the same grade keys and litre values. Grade keys are
        preserved verbatim; the input mapping is not mutated.
    """
    return {
        grade: float(gallons) * LITERS_PER_US_GALLON
        for grade, gallons in quantities_gallons.items()
    }


def liters_to_us_gallons(
    quantities_liters: Mapping[str, float],
) -> Dict[str, float]:
    """Convert a per-grade litre mapping to US gallons for the response
    boundary (R6.23).

    The inverse of :func:`us_gallons_to_liters`. Round-tripping a value through
    both functions returns the original within floating-point tolerance.

    Args:
        quantities_liters: Fuel grade → quantity in litres, as persisted on an
            ``mvp_plan_executions`` stop record.

    Returns:
        A new dict with the same grade keys and US-gallon values. Grade keys
        are preserved verbatim; the input mapping is not mutated.
    """
    return {
        grade: float(liters) / LITERS_PER_US_GALLON
        for grade, liters in quantities_liters.items()
    }


__all__ = [
    "LITERS_PER_US_GALLON",
    "us_gallons_to_liters",
    "liters_to_us_gallons",
]
