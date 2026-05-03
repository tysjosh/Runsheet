"""Geo utilities for driver services — distance calculations and coordinate helpers."""

import math


def haversine_distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Compute great-circle distance between two points in meters.

    Uses the Haversine formula to calculate the shortest distance over the
    Earth's surface between two points specified by latitude/longitude.

    Args:
        lat1: Latitude of the first point in decimal degrees.
        lng1: Longitude of the first point in decimal degrees.
        lat2: Latitude of the second point in decimal degrees.
        lng2: Longitude of the second point in decimal degrees.

    Returns:
        Distance between the two points in meters.
    """
    R = 6_371_000  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
