"""Unit tests for the Haversine distance utility function."""

import pytest

from driver.services.geo_utils import haversine_distance_meters


class TestHaversineDistanceMeters:
    """Tests for haversine_distance_meters."""

    def test_same_point_returns_zero(self):
        """Distance from a point to itself should be zero."""
        assert haversine_distance_meters(0.0, 0.0, 0.0, 0.0) == 0.0
        assert haversine_distance_meters(51.5074, -0.1278, 51.5074, -0.1278) == 0.0

    def test_known_distance_london_to_paris(self):
        """London (51.5074, -0.1278) to Paris (48.8566, 2.3522) ≈ 343.5 km."""
        distance = haversine_distance_meters(51.5074, -0.1278, 48.8566, 2.3522)
        assert 340_000 < distance < 347_000  # ~343.5 km with reasonable tolerance

    def test_known_distance_new_york_to_los_angeles(self):
        """New York (40.7128, -74.0060) to LA (34.0522, -118.2437) ≈ 3,944 km."""
        distance = haversine_distance_meters(40.7128, -74.0060, 34.0522, -118.2437)
        assert 3_930_000 < distance < 3_960_000

    def test_short_distance_within_pod_radius(self):
        """Two points ~100m apart should return a distance under 500m (default POD radius)."""
        # Approximately 100m offset in latitude at the equator
        lat1, lng1 = 0.0, 0.0
        lat2, lng2 = 0.0009, 0.0  # ~100m north
        distance = haversine_distance_meters(lat1, lng1, lat2, lng2)
        assert 90 < distance < 110

    def test_short_distance_outside_pod_radius(self):
        """Two points ~600m apart should exceed the 500m default POD radius."""
        lat1, lng1 = 0.0, 0.0
        lat2, lng2 = 0.0054, 0.0  # ~600m north
        distance = haversine_distance_meters(lat1, lng1, lat2, lng2)
        assert distance > 500

    def test_antipodal_points(self):
        """Distance between antipodal points should be approximately half Earth's circumference."""
        # North pole to south pole
        distance = haversine_distance_meters(90.0, 0.0, -90.0, 0.0)
        half_circumference = 6_371_000 * 3.14159265358979
        assert abs(distance - half_circumference) < 100  # within 100m tolerance

    def test_symmetry(self):
        """Distance from A to B should equal distance from B to A."""
        d1 = haversine_distance_meters(51.5074, -0.1278, 48.8566, 2.3522)
        d2 = haversine_distance_meters(48.8566, 2.3522, 51.5074, -0.1278)
        assert d1 == pytest.approx(d2)

    def test_returns_float(self):
        """Function should always return a float."""
        result = haversine_distance_meters(0.0, 0.0, 1.0, 1.0)
        assert isinstance(result, float)

    def test_non_negative(self):
        """Distance should always be non-negative."""
        result = haversine_distance_meters(10.0, 20.0, -10.0, -20.0)
        assert result >= 0.0
