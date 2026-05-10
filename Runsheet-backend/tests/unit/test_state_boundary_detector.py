"""Unit tests for StateBoundaryDetector — grid-cell caching and boundary detection.

Tests cover:
- BoundaryCrossing model creation with valid data
- Grid cell key computation for various coordinates
- US bounding box rejection for out-of-bounds coordinates
- get_state() returns None when shapefile is not available (graceful degradation)
- get_state() uses grid-cell cache on repeated lookups
- get_state() performs point-in-polygon lookup when cache misses
- detect_boundary_crossing() returns empty list for fewer than 2 points
- detect_boundary_crossing() detects crossings when state changes
- detect_boundary_crossing() skips points where state is None
- detect_boundary_crossing() includes timestamps when provided
- clear_cache() resets the grid-cell cache
- cache_size property reflects current cache state

Validates: Requirement 7.1
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from compliance.services.state_boundary_detector import (
    GRID_CELL_SIZE,
    US_LAT_MAX,
    US_LAT_MIN,
    US_LON_MAX,
    US_LON_MIN,
    BoundaryCrossing,
    StateBoundaryDetector,
    _grid_cell_key,
    _is_within_us_bounds,
)


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_detector_with_mock_polygons(state_map: dict) -> StateBoundaryDetector:
    """Create a StateBoundaryDetector with mocked polygon data.

    Args:
        state_map: Dict mapping (lat_cell, lon_cell) -> state_code.
            The mock will return the state for any point in that grid cell.
    """
    detector = StateBoundaryDetector(shapefile_path="/nonexistent/path.shp")
    # Bypass shapefile loading by directly setting internal state
    detector._shapefile_loaded = True
    detector._load_attempted = True
    detector._state_polygons = []

    # Pre-populate the grid cache with the state map
    for cell_key, state_code in state_map.items():
        detector._grid_cache[cell_key] = state_code

    return detector


def _make_detector_with_polygon_lookup(lookup_fn) -> StateBoundaryDetector:
    """Create a detector with a custom point-in-polygon lookup function.

    Args:
        lookup_fn: Function(lat, lon) -> Optional[str] that replaces
            the real shapefile lookup.
    """
    detector = StateBoundaryDetector(shapefile_path="/nonexistent/path.shp")
    detector._shapefile_loaded = True
    detector._load_attempted = True
    detector._state_polygons = []  # Non-None to pass the check
    detector._point_in_polygon_lookup = lookup_fn  # type: ignore[assignment]
    return detector


# ---------------------------------------------------------------------------
# Tests: BoundaryCrossing Model
# ---------------------------------------------------------------------------


class TestBoundaryCrossingModel:
    """Tests for the BoundaryCrossing Pydantic model."""

    def test_create_with_all_fields(self):
        crossing = BoundaryCrossing(
            from_state="TX",
            to_state="OK",
            lat=33.9,
            lon=-97.1,
            timestamp=_FIXED_NOW,
        )
        assert crossing.from_state == "TX"
        assert crossing.to_state == "OK"
        assert crossing.lat == 33.9
        assert crossing.lon == -97.1
        assert crossing.timestamp == _FIXED_NOW

    def test_create_without_timestamp(self):
        crossing = BoundaryCrossing(
            from_state="CA",
            to_state="NV",
            lat=35.0,
            lon=-115.0,
        )
        assert crossing.timestamp is None

    def test_rejects_extra_fields(self):
        with pytest.raises(Exception):
            BoundaryCrossing(
                from_state="TX",
                to_state="OK",
                lat=33.9,
                lon=-97.1,
                extra_field="not_allowed",
            )


# ---------------------------------------------------------------------------
# Tests: Grid Cell Key Computation
# ---------------------------------------------------------------------------


class TestGridCellKey:
    """Tests for the _grid_cell_key helper function."""

    def test_positive_coordinates(self):
        # 40.5, -73.9 → cell (405, -739)
        key = _grid_cell_key(40.5, -73.9)
        assert key == (405, -739)

    def test_zero_coordinates(self):
        key = _grid_cell_key(0.0, 0.0)
        assert key == (0, 0)

    def test_negative_coordinates(self):
        key = _grid_cell_key(-10.5, -120.3)
        assert key == (-105, -1203)

    def test_boundary_of_cell(self):
        # Points at exact cell boundaries
        key1 = _grid_cell_key(40.0, -74.0)
        key2 = _grid_cell_key(40.09, -73.91)
        assert key1 == key2  # Same cell

    def test_adjacent_cells_differ(self):
        key1 = _grid_cell_key(40.0, -74.0)
        key2 = _grid_cell_key(40.1, -74.0)
        assert key1 != key2  # Different cells


# ---------------------------------------------------------------------------
# Tests: US Bounding Box Check
# ---------------------------------------------------------------------------


class TestIsWithinUSBounds:
    """Tests for the _is_within_us_bounds helper function."""

    def test_point_in_continental_us(self):
        # Dallas, TX
        assert _is_within_us_bounds(32.78, -96.80) is True

    def test_point_in_new_york(self):
        assert _is_within_us_bounds(40.71, -74.01) is True

    def test_point_south_of_us(self):
        # Mexico City
        assert _is_within_us_bounds(19.43, -99.13) is False

    def test_point_north_of_us(self):
        # Toronto, Canada
        assert _is_within_us_bounds(51.5, -79.4) is False

    def test_point_east_of_us(self):
        # Atlantic Ocean
        assert _is_within_us_bounds(40.0, -60.0) is False

    def test_point_west_of_us(self):
        # Pacific Ocean
        assert _is_within_us_bounds(40.0, -130.0) is False

    def test_boundary_values_included(self):
        assert _is_within_us_bounds(US_LAT_MIN, US_LON_MIN) is True
        assert _is_within_us_bounds(US_LAT_MAX, US_LON_MAX) is True


# ---------------------------------------------------------------------------
# Tests: StateBoundaryDetector.get_state()
# ---------------------------------------------------------------------------


class TestGetState:
    """Tests for StateBoundaryDetector.get_state()."""

    def test_returns_none_when_shapefile_not_available(self):
        """Graceful degradation when shapefile is missing."""
        detector = StateBoundaryDetector(shapefile_path="/nonexistent/path.shp")
        result = detector.get_state(40.0, -74.0)
        assert result is None

    def test_returns_none_for_out_of_bounds_coordinates(self):
        """Points outside US bounding box return None without shapefile query."""
        detector = StateBoundaryDetector()
        # Point in Europe
        result = detector.get_state(51.5, 0.0)
        assert result is None

    def test_uses_cache_on_repeated_lookup(self):
        """Second lookup for same grid cell uses cache, not polygon lookup."""
        state_map = {_grid_cell_key(40.0, -74.0): "NJ"}
        detector = _make_detector_with_mock_polygons(state_map)

        result = detector.get_state(40.0, -74.0)
        assert result == "NJ"

    def test_cache_hit_avoids_polygon_lookup(self):
        """Verify that cached results don't trigger polygon queries."""
        call_count = {"n": 0}

        def mock_lookup(lat, lon):
            call_count["n"] += 1
            return "TX"

        detector = _make_detector_with_polygon_lookup(mock_lookup)

        # First call — triggers lookup
        result1 = detector.get_state(32.78, -96.80)
        assert result1 == "TX"
        assert call_count["n"] == 1

        # Second call in same grid cell — uses cache
        result2 = detector.get_state(32.78, -96.80)
        assert result2 == "TX"
        assert call_count["n"] == 1  # No additional lookup

    def test_different_cells_trigger_separate_lookups(self):
        """Points in different grid cells each trigger their own lookup."""
        call_count = {"n": 0}

        def mock_lookup(lat, lon):
            call_count["n"] += 1
            if lat > 33.0:
                return "OK"
            return "TX"

        detector = _make_detector_with_polygon_lookup(mock_lookup)

        detector.get_state(32.5, -97.0)  # TX cell
        detector.get_state(34.0, -97.0)  # OK cell
        assert call_count["n"] == 2

    def test_caches_none_result(self):
        """None results (point not in any state) are also cached."""
        call_count = {"n": 0}

        def mock_lookup(lat, lon):
            call_count["n"] += 1
            return None  # e.g., point in a lake or border area

        detector = _make_detector_with_polygon_lookup(mock_lookup)

        result1 = detector.get_state(40.0, -74.0)
        assert result1 is None
        result2 = detector.get_state(40.0, -74.0)
        assert result2 is None
        assert call_count["n"] == 1  # Only one lookup


# ---------------------------------------------------------------------------
# Tests: StateBoundaryDetector.detect_boundary_crossing()
# ---------------------------------------------------------------------------


class TestDetectBoundaryCrossing:
    """Tests for StateBoundaryDetector.detect_boundary_crossing()."""

    def test_empty_points_returns_empty(self):
        detector = StateBoundaryDetector()
        result = detector.detect_boundary_crossing([])
        assert result == []

    def test_single_point_returns_empty(self):
        detector = StateBoundaryDetector()
        result = detector.detect_boundary_crossing([(40.0, -74.0)])
        assert result == []

    def test_no_crossing_same_state(self):
        """All points in the same state → no crossings."""
        state_map = {
            _grid_cell_key(32.0, -97.0): "TX",
            _grid_cell_key(32.1, -97.0): "TX",
            _grid_cell_key(32.2, -97.0): "TX",
        }
        detector = _make_detector_with_mock_polygons(state_map)

        points = [(32.0, -97.0), (32.1, -97.0), (32.2, -97.0)]
        result = detector.detect_boundary_crossing(points)
        assert result == []

    def test_single_crossing_detected(self):
        """Detects a single TX → OK crossing."""
        state_map = {
            _grid_cell_key(33.8, -97.0): "TX",
            _grid_cell_key(33.9, -97.0): "TX",
            _grid_cell_key(34.0, -97.0): "OK",
        }
        detector = _make_detector_with_mock_polygons(state_map)

        points = [(33.8, -97.0), (33.9, -97.0), (34.0, -97.0)]
        result = detector.detect_boundary_crossing(points)

        assert len(result) == 1
        assert result[0].from_state == "TX"
        assert result[0].to_state == "OK"
        assert result[0].lat == 34.0
        assert result[0].lon == -97.0

    def test_multiple_crossings_detected(self):
        """Detects multiple crossings in a multi-state trip."""
        state_map = {
            _grid_cell_key(32.0, -97.0): "TX",
            _grid_cell_key(34.0, -97.0): "OK",
            _grid_cell_key(37.0, -97.0): "KS",
        }
        detector = _make_detector_with_mock_polygons(state_map)

        points = [(32.0, -97.0), (34.0, -97.0), (37.0, -97.0)]
        result = detector.detect_boundary_crossing(points)

        assert len(result) == 2
        assert result[0].from_state == "TX"
        assert result[0].to_state == "OK"
        assert result[1].from_state == "OK"
        assert result[1].to_state == "KS"

    def test_skips_none_state_points(self):
        """Points where state is None are skipped without breaking detection."""

        def mock_lookup(lat, lon):
            if lat < 33.0:
                return "TX"
            if lat > 34.0:
                return "OK"
            return None  # Border area

        detector = _make_detector_with_polygon_lookup(mock_lookup)

        points = [(32.5, -97.0), (33.5, -97.0), (34.5, -97.0)]
        result = detector.detect_boundary_crossing(points)

        # Should detect TX → OK crossing (skipping the None in between)
        assert len(result) == 1
        assert result[0].from_state == "TX"
        assert result[0].to_state == "OK"

    def test_includes_timestamps_when_provided(self):
        """Timestamps are attached to crossing events."""
        state_map = {
            _grid_cell_key(33.8, -97.0): "TX",
            _grid_cell_key(34.0, -97.0): "OK",
        }
        detector = _make_detector_with_mock_polygons(state_map)

        points = [(33.8, -97.0), (34.0, -97.0)]
        timestamps = [
            datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 6, 1, 10, 30, tzinfo=timezone.utc),
        ]
        result = detector.detect_boundary_crossing(points, timestamps=timestamps)

        assert len(result) == 1
        assert result[0].timestamp == timestamps[1]

    def test_no_timestamps_results_in_none(self):
        """When timestamps are not provided, crossing.timestamp is None."""
        state_map = {
            _grid_cell_key(33.8, -97.0): "TX",
            _grid_cell_key(34.0, -97.0): "OK",
        }
        detector = _make_detector_with_mock_polygons(state_map)

        points = [(33.8, -97.0), (34.0, -97.0)]
        result = detector.detect_boundary_crossing(points)

        assert len(result) == 1
        assert result[0].timestamp is None


# ---------------------------------------------------------------------------
# Tests: Cache Management
# ---------------------------------------------------------------------------


class TestCacheManagement:
    """Tests for cache-related functionality."""

    def test_clear_cache_empties_grid_cache(self):
        state_map = {_grid_cell_key(40.0, -74.0): "NJ"}
        detector = _make_detector_with_mock_polygons(state_map)

        assert detector.cache_size == 1
        detector.clear_cache()
        assert detector.cache_size == 0

    def test_cache_size_reflects_lookups(self):
        def mock_lookup(lat, lon):
            return "TX"

        detector = _make_detector_with_polygon_lookup(mock_lookup)
        assert detector.cache_size == 0

        detector.get_state(32.0, -97.0)
        assert detector.cache_size == 1

        detector.get_state(33.0, -97.0)
        assert detector.cache_size == 2

    def test_is_shapefile_loaded_false_by_default(self):
        detector = StateBoundaryDetector()
        assert detector.is_shapefile_loaded is False

    def test_is_shapefile_loaded_true_after_successful_load(self):
        detector = StateBoundaryDetector()
        detector._shapefile_loaded = True
        assert detector.is_shapefile_loaded is True


# ---------------------------------------------------------------------------
# Tests: Shapefile Loading (Graceful Degradation)
# ---------------------------------------------------------------------------


class TestShapefileLoading:
    """Tests for shapefile loading and graceful degradation."""

    def test_missing_shapefile_logs_warning_and_degrades(self, caplog):
        """When shapefile doesn't exist, logs warning and returns None."""
        import logging

        with caplog.at_level(logging.WARNING):
            detector = StateBoundaryDetector(
                shapefile_path="/definitely/not/a/real/path.shp"
            )
            result = detector.get_state(40.0, -74.0)

        assert result is None
        assert not detector.is_shapefile_loaded
        assert "shapefile not found" in caplog.text

    def test_load_attempted_only_once(self):
        """Shapefile load is only attempted once even on multiple get_state calls."""
        detector = StateBoundaryDetector(
            shapefile_path="/nonexistent/path.shp"
        )

        detector.get_state(40.0, -74.0)
        detector.get_state(41.0, -74.0)

        # _load_attempted should be True after first call
        assert detector._load_attempted is True
        assert not detector.is_shapefile_loaded
