"""
Unit tests for ``services.unit_conversion``.

Covers the conversion factors, supported unit aliases, idempotence of
canonical-unit passes, round-trip accuracy, and error paths.

Validates: Requirement 6.3.3.
"""
from __future__ import annotations

import math

import pytest

from services.unit_conversion import (
    GAL_TO_L,
    MI_TO_KM,
    UnknownUnitError,
    from_canonical_distance,
    from_canonical_volume,
    to_canonical_distance,
    to_canonical_volume,
)


# ---------------------------------------------------------------------------
# Conversion factors (per NIST Handbook 44)
# ---------------------------------------------------------------------------


class TestConversionConstants:
    def test_gal_to_l_is_exact_nist_value(self) -> None:
        assert GAL_TO_L == 3.785411784

    def test_mi_to_km_is_exact_nist_value(self) -> None:
        assert MI_TO_KM == 1.609344


# ---------------------------------------------------------------------------
# Volume — to_canonical_volume / from_canonical_volume
# ---------------------------------------------------------------------------


class TestToCanonicalVolume:
    def test_gallons_pass_through_unchanged(self) -> None:
        assert to_canonical_volume(10.0, "gal") == 10.0

    def test_liters_convert_to_gallons(self) -> None:
        # 3.785411784 L == 1 gal, exact.
        assert math.isclose(
            to_canonical_volume(3.785411784, "l"), 1.0, rel_tol=1e-12
        )

    @pytest.mark.parametrize(
        "alias",
        ["L", "liter", "liters", "Liter", "LITERS", "  liters  ", "LiTrE", "litres"],
    )
    def test_liter_aliases_all_resolve(self, alias: str) -> None:
        assert math.isclose(
            to_canonical_volume(GAL_TO_L, alias), 1.0, rel_tol=1e-12
        )

    @pytest.mark.parametrize("alias", ["gal", "GAL", "Gallon", "GALLONS", " gal "])
    def test_gallon_aliases_all_resolve(self, alias: str) -> None:
        assert to_canonical_volume(42.0, alias) == 42.0

    def test_zero_passes_through(self) -> None:
        assert to_canonical_volume(0.0, "gal") == 0.0
        assert to_canonical_volume(0.0, "l") == 0.0

    def test_negative_values_are_accepted(self) -> None:
        # The conversion function is purely linear; negativity is a domain
        # concern left to callers.
        assert to_canonical_volume(-GAL_TO_L, "l") == -1.0

    def test_unknown_unit_raises(self) -> None:
        with pytest.raises(UnknownUnitError):
            to_canonical_volume(1.0, "barrels")

    def test_unknown_unit_is_value_error(self) -> None:
        with pytest.raises(ValueError):
            to_canonical_volume(1.0, "barrels")

    def test_non_numeric_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            to_canonical_volume("10", "gal")  # type: ignore[arg-type]

    def test_boolean_input_rejected(self) -> None:
        with pytest.raises(TypeError):
            to_canonical_volume(True, "gal")  # type: ignore[arg-type]

    def test_nan_rejected(self) -> None:
        with pytest.raises(ValueError):
            to_canonical_volume(float("nan"), "gal")

    def test_inf_rejected(self) -> None:
        with pytest.raises(ValueError):
            to_canonical_volume(float("inf"), "l")

    def test_int_input_accepted(self) -> None:
        # Integers should be promoted to float.
        assert to_canonical_volume(5, "gal") == 5.0


class TestFromCanonicalVolume:
    def test_gallons_pass_through_unchanged(self) -> None:
        assert from_canonical_volume(10.0, "gal") == 10.0

    def test_to_liters_uses_exact_factor(self) -> None:
        assert math.isclose(
            from_canonical_volume(1.0, "l"), GAL_TO_L, rel_tol=1e-12
        )

    def test_round_trip_identity_for_gallons(self) -> None:
        for x in [0.0, 1.0, 3.14, 1234.5678]:
            assert from_canonical_volume(to_canonical_volume(x, "gal"), "gal") == x

    @pytest.mark.parametrize(
        "unit", ["gal", "l", "L", "liter", "liters"]
    )
    @pytest.mark.parametrize("value", [0.0, 1.0, 100.0, 12345.6789, 0.0001])
    def test_round_trip_accuracy_within_1e_9(
        self, unit: str, value: float
    ) -> None:
        # Requirement 6.3.4: round-trip accuracy better than 1e-9 relative.
        recovered = from_canonical_volume(
            to_canonical_volume(value, unit), unit
        )
        assert math.isclose(recovered, value, rel_tol=1e-9, abs_tol=1e-12)


class TestVolumeIdempotence:
    """Calling to_canonical_volume twice with the canonical unit is a no-op."""

    @pytest.mark.parametrize("value", [0.0, 1.0, 2.71828, 999_999.0])
    def test_double_canonical_pass_unchanged(self, value: float) -> None:
        once = to_canonical_volume(value, "gal")
        twice = to_canonical_volume(once, "gal")
        assert once == twice == value


# ---------------------------------------------------------------------------
# Distance — to_canonical_distance / from_canonical_distance
# ---------------------------------------------------------------------------


class TestToCanonicalDistance:
    def test_miles_pass_through_unchanged(self) -> None:
        assert to_canonical_distance(25.0, "mi") == 25.0

    def test_kilometers_convert_to_miles(self) -> None:
        # 1.609344 km == 1 mi, exact.
        assert math.isclose(
            to_canonical_distance(1.609344, "km"), 1.0, rel_tol=1e-12
        )

    def test_meters_convert_to_miles(self) -> None:
        # 1609.344 m == 1 mi, exact.
        assert math.isclose(
            to_canonical_distance(1609.344, "m"), 1.0, rel_tol=1e-12
        )

    @pytest.mark.parametrize(
        "alias", ["mi", "MI", "Mile", "miles", "MILES", " mi "]
    )
    def test_mile_aliases_all_resolve(self, alias: str) -> None:
        assert to_canonical_distance(7.0, alias) == 7.0

    @pytest.mark.parametrize("alias", ["km", "KM", "Kilometer", "kilometers"])
    def test_kilometer_aliases_all_resolve(self, alias: str) -> None:
        assert math.isclose(
            to_canonical_distance(MI_TO_KM, alias), 1.0, rel_tol=1e-12
        )

    @pytest.mark.parametrize("alias", ["m", "M", "meter", "meters", " m "])
    def test_meter_aliases_all_resolve(self, alias: str) -> None:
        assert math.isclose(
            to_canonical_distance(MI_TO_KM * 1000.0, alias),
            1.0,
            rel_tol=1e-12,
        )

    def test_unknown_unit_raises(self) -> None:
        with pytest.raises(UnknownUnitError):
            to_canonical_distance(1.0, "furlongs")

    def test_nan_rejected(self) -> None:
        with pytest.raises(ValueError):
            to_canonical_distance(float("nan"), "mi")

    def test_inf_rejected(self) -> None:
        with pytest.raises(ValueError):
            to_canonical_distance(float("-inf"), "km")


class TestFromCanonicalDistance:
    def test_miles_pass_through_unchanged(self) -> None:
        assert from_canonical_distance(25.0, "mi") == 25.0

    def test_to_kilometers_uses_exact_factor(self) -> None:
        assert math.isclose(
            from_canonical_distance(1.0, "km"), MI_TO_KM, rel_tol=1e-12
        )

    def test_to_meters_uses_exact_factor(self) -> None:
        assert math.isclose(
            from_canonical_distance(1.0, "m"), MI_TO_KM * 1000.0, rel_tol=1e-12
        )

    @pytest.mark.parametrize("unit", ["mi", "km", "m"])
    @pytest.mark.parametrize(
        "value", [0.0, 1.0, 0.5, 100.0, 12345.6789, 0.0001]
    )
    def test_round_trip_accuracy_within_1e_9(
        self, unit: str, value: float
    ) -> None:
        # Requirement 6.3.4: round-trip accuracy better than 1e-9 relative.
        recovered = from_canonical_distance(
            to_canonical_distance(value, unit), unit
        )
        assert math.isclose(recovered, value, rel_tol=1e-9, abs_tol=1e-12)


class TestDistanceIdempotence:
    """Calling to_canonical_distance twice with the canonical unit is a no-op."""

    @pytest.mark.parametrize("value", [0.0, 1.0, 3.14159, 750.25])
    def test_double_canonical_pass_unchanged(self, value: float) -> None:
        once = to_canonical_distance(value, "mi")
        twice = to_canonical_distance(once, "mi")
        assert once == twice == value


# ---------------------------------------------------------------------------
# Cross-unit round trips (display → canonical → display)
# ---------------------------------------------------------------------------


class TestCrossUnitRoundTrips:
    """Sanity-check conversions in both directions against textbook values."""

    def test_ten_gallons_is_37_854_liters(self) -> None:
        liters = from_canonical_volume(10.0, "l")
        assert math.isclose(liters, 37.85411784, rel_tol=1e-12)

    def test_one_hundred_miles_is_160_9344_kilometers(self) -> None:
        km = from_canonical_distance(100.0, "km")
        assert math.isclose(km, 160.9344, rel_tol=1e-12)

    def test_one_thousand_meters_is_0_621371_miles(self) -> None:
        miles = to_canonical_distance(1000.0, "m")
        # 1 km = 0.621371192237... mi
        assert math.isclose(miles, 1000.0 / 1609.344, rel_tol=1e-12)
