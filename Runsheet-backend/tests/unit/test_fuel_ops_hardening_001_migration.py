"""
Unit tests for the ``compute_modal_coordinate`` helper used by the
``fuel_ops_hardening_001_region_and_aliases`` migration script.

Covers:
    * Empty input → ``None``.
    * Single-valued input is returned directly.
    * True mode is selected over less-frequent coordinates.
    * Ties are broken by the lexicographically smallest coordinate so
      idempotency holds across re-runs.
    * Invalid coordinates (NaN, out of range, None) are filtered out.
    * GPS-noise-style neighboring coordinates are bucketed together by
      the rounding precision.
"""
from __future__ import annotations

import math

import pytest

from scripts.migrations.fuel_ops_hardening_001_region_and_aliases import (
    compute_modal_coordinate,
)


class TestComputeModalCoordinate:
    def test_empty_input_returns_none(self) -> None:
        assert compute_modal_coordinate([]) is None

    def test_single_coordinate_returned(self) -> None:
        assert compute_modal_coordinate([(40.7128, -74.0060)]) == (40.7128, -74.0060)

    def test_true_mode_wins_over_less_frequent_points(self) -> None:
        coords = [
            (10.0, 20.0),
            (10.0, 20.0),
            (10.0, 20.0),
            (15.0, 25.0),
            (15.0, 25.0),
        ]
        assert compute_modal_coordinate(coords) == (10.0, 20.0)

    def test_tie_is_broken_lexicographically(self) -> None:
        """Two coordinates with equal counts → the smaller tuple wins.

        Required for idempotency: running the migration twice against the
        same station layout MUST produce the same default Depot
        coordinate.
        """

        coords = [(10.0, 20.0), (10.0, 20.0), (5.0, 50.0), (5.0, 50.0)]
        # (5.0, 50.0) < (10.0, 20.0) lexicographically.
        assert compute_modal_coordinate(coords) == (5.0, 50.0)

    def test_invalid_entries_are_ignored(self) -> None:
        coords = [
            (None, 20.0),  # missing component
            (10.0, None),  # missing component
            (float("nan"), 0.0),  # NaN
            (0.0, float("inf")),  # Inf
            (100.0, 20.0),  # latitude out of range
            (0.0, -200.0),  # longitude out of range
            (10.0, 20.0),
            (10.0, 20.0),
        ]
        assert compute_modal_coordinate(coords) == (10.0, 20.0)

    def test_rounding_buckets_gps_noise_together(self) -> None:
        """Coordinates within the rounding precision are clustered together."""

        # Five decimals → coordinates that only differ in the 6th digit
        # all land in the same bucket and push that bucket to the mode.
        coords = [
            (40.712800, -74.006000),
            (40.712801, -74.006001),
            (40.712799, -74.005999),
            (41.000000, -75.000000),
        ]
        lat, lon = compute_modal_coordinate(coords) or (None, None)
        assert lat is not None and lon is not None
        assert math.isclose(lat, 40.71280, abs_tol=1e-6)
        assert math.isclose(lon, -74.00600, abs_tol=1e-6)

    def test_returns_none_when_every_coordinate_is_invalid(self) -> None:
        coords = [(None, None), (float("nan"), float("nan")), (200.0, 400.0)]
        assert compute_modal_coordinate(coords) is None

    def test_handles_string_coordinate_values(self) -> None:
        """Coordinates passed in as strings (from seed data) are coerced."""

        coords = [("10.5", "20.5"), ("10.5", "20.5"), ("9.9", "19.9")]  # type: ignore[list-item]
        assert compute_modal_coordinate(coords) == (10.5, 20.5)
