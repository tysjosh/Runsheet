"""
Fuel Calculations Module.

Pure calculation functions for fuel refill quantity and priority
classification. Used by the Fuel Management Agent to determine how
much fuel to request and at what urgency level.

Requirements: 4.3, 4.7
"""
from enum import Enum
from typing import Optional


class FuelPriority(str, Enum):
    """Priority classification for fuel refill requests.

    Values ordered from most to least urgent.
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    NORMAL = "normal"


def calculate_refill_quantity(
    capacity_liters: float,
    current_stock_liters: float,
    target_pct: float = 0.8,
) -> float:
    """Calculate the refill quantity to restore a station to target capacity.

    Args:
        capacity_liters: Total station capacity in liters.
        current_stock_liters: Current stock level in liters.
        target_pct: Target fill percentage (default 80%).

    Returns:
        Refill quantity in liters. Always >= 0.

    Property 9: For any (C, S), result == max(0, target_pct * C - S)
    """
    target = target_pct * capacity_liters
    quantity = target - current_stock_liters
    return max(0.0, quantity)


# Most urgent first; used to take the max of the days rule and the status floor.
_URGENCY_ORDER = (
    FuelPriority.NORMAL,
    FuelPriority.MEDIUM,
    FuelPriority.HIGH,
    FuelPriority.CRITICAL,
)

# Minimum priority implied by a station's stock status. A station with no
# consumption history reports a huge days_until_empty, so the days rule alone
# rated a critical/empty station "normal" (F12).
_STATUS_FLOOR = {
    "critical": FuelPriority.CRITICAL,
    "empty": FuelPriority.CRITICAL,
    "low": FuelPriority.HIGH,
}


def calculate_refill_priority(
    days_until_empty: float, status: Optional[str] = None
) -> FuelPriority:
    """Classify refill priority based on days until empty and station status.

    Args:
        days_until_empty: Estimated days until station is empty.
        status: Optional station stock status. ``critical`` / ``empty``
            raise the result to CRITICAL, ``low`` to at least HIGH.

    Returns:
        FuelPriority enum value.

    Property 10: critical if <1, high if <3, medium if <5, normal otherwise
    (with ``status`` None; a status floor can only raise the result).
    """
    if days_until_empty < 1:
        by_days = FuelPriority.CRITICAL
    elif days_until_empty < 3:
        by_days = FuelPriority.HIGH
    elif days_until_empty < 5:
        by_days = FuelPriority.MEDIUM
    else:
        by_days = FuelPriority.NORMAL

    floor = _STATUS_FLOOR.get((status or "").strip().lower())
    if floor is None:
        return by_days
    return max(by_days, floor, key=_URGENCY_ORDER.index)
