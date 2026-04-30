"""
Unit tests for the Fuel Calculations module.

Tests FuelPriority enum, calculate_refill_quantity, and
calculate_refill_priority functions.

Requirements: 4.3, 4.7
"""
import pytest

from Agents.autonomous.fuel_calculations import (
    FuelPriority,
    calculate_refill_quantity,
    calculate_refill_priority,
)


# ---------------------------------------------------------------------------
# Tests: FuelPriority enum
# ---------------------------------------------------------------------------


class TestFuelPriority:
    """Tests for the FuelPriority enum."""

    def test_has_four_members(self):
        assert len(FuelPriority) == 4

    def test_critical_value(self):
        assert FuelPriority.CRITICAL == "critical"

    def test_high_value(self):
        assert FuelPriority.HIGH == "high"

    def test_medium_value(self):
        assert FuelPriority.MEDIUM == "medium"

    def test_normal_value(self):
        assert FuelPriority.NORMAL == "normal"

    def test_is_str_subclass(self):
        assert isinstance(FuelPriority.CRITICAL, str)

    def test_constructable_from_string(self):
        assert FuelPriority("critical") is FuelPriority.CRITICAL
        assert FuelPriority("high") is FuelPriority.HIGH
        assert FuelPriority("medium") is FuelPriority.MEDIUM
        assert FuelPriority("normal") is FuelPriority.NORMAL


# ---------------------------------------------------------------------------
# Tests: calculate_refill_quantity
# ---------------------------------------------------------------------------


class TestCalculateRefillQuantity:
    """Tests for calculate_refill_quantity.

    Property 9: result == max(0, target_pct * capacity - current_stock)
    """

    def test_basic_refill(self):
        # 80% of 1000 = 800, minus 200 stock = 600
        result = calculate_refill_quantity(1000.0, 200.0)
        assert result == 600.0

    def test_stock_at_target_returns_zero(self):
        # 80% of 1000 = 800, stock is 800 → 0
        result = calculate_refill_quantity(1000.0, 800.0)
        assert result == 0.0

    def test_stock_above_target_returns_zero(self):
        # 80% of 1000 = 800, stock is 900 → max(0, -100) = 0
        result = calculate_refill_quantity(1000.0, 900.0)
        assert result == 0.0

    def test_stock_at_full_capacity_returns_zero(self):
        result = calculate_refill_quantity(1000.0, 1000.0)
        assert result == 0.0

    def test_empty_station(self):
        # 80% of 1000 = 800, stock is 0 → 800
        result = calculate_refill_quantity(1000.0, 0.0)
        assert result == 800.0

    def test_custom_target_pct(self):
        # 90% of 1000 = 900, minus 300 stock = 600
        result = calculate_refill_quantity(1000.0, 300.0, target_pct=0.9)
        assert result == 600.0

    def test_target_pct_one_hundred_percent(self):
        # 100% of 500 = 500, minus 100 stock = 400
        result = calculate_refill_quantity(500.0, 100.0, target_pct=1.0)
        assert result == 400.0

    def test_zero_capacity_returns_zero(self):
        # 80% of 0 = 0, minus 0 stock = 0
        result = calculate_refill_quantity(0.0, 0.0)
        assert result == 0.0

    def test_result_is_always_non_negative(self):
        # Even with stock exceeding capacity
        result = calculate_refill_quantity(100.0, 500.0)
        assert result >= 0.0

    def test_small_fractional_values(self):
        # 80% of 10.5 = 8.4, minus 3.2 = 5.2
        result = calculate_refill_quantity(10.5, 3.2)
        assert result == pytest.approx(5.2)

    def test_default_target_pct_is_0_8(self):
        # Verify default target_pct is 0.8
        result_default = calculate_refill_quantity(1000.0, 0.0)
        result_explicit = calculate_refill_quantity(1000.0, 0.0, target_pct=0.8)
        assert result_default == result_explicit


# ---------------------------------------------------------------------------
# Tests: calculate_refill_priority
# ---------------------------------------------------------------------------


class TestCalculateRefillPriority:
    """Tests for calculate_refill_priority.

    Property 10: critical if <1, high if <3, medium if <5, normal otherwise
    """

    # --- Critical: days_until_empty < 1 ---

    def test_zero_days_is_critical(self):
        assert calculate_refill_priority(0.0) == FuelPriority.CRITICAL

    def test_half_day_is_critical(self):
        assert calculate_refill_priority(0.5) == FuelPriority.CRITICAL

    def test_negative_days_is_critical(self):
        # Already empty
        assert calculate_refill_priority(-1.0) == FuelPriority.CRITICAL

    def test_just_below_one_day_is_critical(self):
        assert calculate_refill_priority(0.99) == FuelPriority.CRITICAL

    # --- High: 1 <= days_until_empty < 3 ---

    def test_exactly_one_day_is_high(self):
        assert calculate_refill_priority(1.0) == FuelPriority.HIGH

    def test_two_days_is_high(self):
        assert calculate_refill_priority(2.0) == FuelPriority.HIGH

    def test_just_below_three_days_is_high(self):
        assert calculate_refill_priority(2.99) == FuelPriority.HIGH

    # --- Medium: 3 <= days_until_empty < 5 ---

    def test_exactly_three_days_is_medium(self):
        assert calculate_refill_priority(3.0) == FuelPriority.MEDIUM

    def test_four_days_is_medium(self):
        assert calculate_refill_priority(4.0) == FuelPriority.MEDIUM

    def test_just_below_five_days_is_medium(self):
        assert calculate_refill_priority(4.99) == FuelPriority.MEDIUM

    # --- Normal: days_until_empty >= 5 ---

    def test_exactly_five_days_is_normal(self):
        assert calculate_refill_priority(5.0) == FuelPriority.NORMAL

    def test_ten_days_is_normal(self):
        assert calculate_refill_priority(10.0) == FuelPriority.NORMAL

    def test_large_value_is_normal(self):
        assert calculate_refill_priority(365.0) == FuelPriority.NORMAL

    # --- Return type ---

    def test_returns_fuel_priority_enum(self):
        result = calculate_refill_priority(2.5)
        assert isinstance(result, FuelPriority)
