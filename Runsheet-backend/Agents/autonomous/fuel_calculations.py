"""
Fuel Calculations Module.

Pure calculation functions for fuel refill quantity and priority
classification. Used by the Fuel Management Agent to determine how
much fuel to request and at what urgency level.

Requirements: 4.3, 4.7
"""
from enum import Enum


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


def calculate_refill_priority(days_until_empty: float) -> FuelPriority:
    """Classify refill priority based on days until empty.

    Args:
        days_until_empty: Estimated days until station is empty.

    Returns:
        FuelPriority enum value.

    Property 10: critical if <1, high if <3, medium if <5, normal otherwise
    """
    if days_until_empty < 1:
        return FuelPriority.CRITICAL
    elif days_until_empty < 3:
        return FuelPriority.HIGH
    elif days_until_empty < 5:
        return FuelPriority.MEDIUM
    else:
        return FuelPriority.NORMAL
