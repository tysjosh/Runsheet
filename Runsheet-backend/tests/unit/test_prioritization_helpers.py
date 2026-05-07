"""
Unit tests for ``fuel.services.prioritization_helpers``.

Validates Requirements 3.1.1, 3.1.2, and 3.1.5:
    * 3.1.1 — ``safe_to_delay_days = max(0, floor((hours_to_runout_p90 −
              SLA_buffer_hours) / 24))``.
    * 3.1.2 — bucket mapping: ``none`` (<1d), ``short`` (1–3d),
              ``medium`` (4–7d), ``long`` (>7d).
    * 3.1.5 — default SLA buffer of 6 hours when none is configured.
"""
from __future__ import annotations

import math

import pytest

from fuel.services.prioritization_helpers import (
    DEFAULT_SLA_BUFFER_HOURS,
    classify_safe_to_delay_bucket,
    compute_safe_to_delay,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _ForecastStub:
    """Minimal stand-in for a Pydantic TankForecast (attribute access)."""

    def __init__(self, hours_to_runout_p90: float) -> None:
        self.hours_to_runout_p90 = hours_to_runout_p90


def _forecast(hours_to_runout_p90: float) -> _ForecastStub:
    return _ForecastStub(hours_to_runout_p90=hours_to_runout_p90)


# ---------------------------------------------------------------------------
# Constant sanity checks
# ---------------------------------------------------------------------------


def test_default_sla_buffer_is_six_hours() -> None:
    """Req 3.1.5: default SLA buffer is 6 hours."""
    assert DEFAULT_SLA_BUFFER_HOURS == 6.0


# ---------------------------------------------------------------------------
# classify_safe_to_delay_bucket — bucket boundaries (Req 3.1.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "days,expected_bucket",
    [
        (0, "none"),
        (1, "short"),
        (2, "short"),
        (3, "short"),
        (4, "medium"),
        (5, "medium"),
        (6, "medium"),
        (7, "medium"),
        (8, "long"),
        (30, "long"),
    ],
)
def test_classify_bucket_boundaries(days: int, expected_bucket: str) -> None:
    """Req 3.1.2: bucket thresholds line up with the spec."""
    assert classify_safe_to_delay_bucket(days) == expected_bucket


# ---------------------------------------------------------------------------
# compute_safe_to_delay — formula (Req 3.1.1) and defaults (Req 3.1.5)
# ---------------------------------------------------------------------------


def test_uses_default_sla_buffer_when_none_supplied() -> None:
    """Req 3.1.5: omitting sla_buffer_hours falls back to 6 hours."""
    # hours_to_runout_p90 = 30h, buffer = 6h → (30 - 6) / 24 = 1.0 → floor = 1
    result = compute_safe_to_delay(_forecast(30.0))
    assert result == {"safe_to_delay_days": 1, "safe_to_delay_bucket": "short"}


def test_explicit_sla_buffer_overrides_default() -> None:
    """Passing sla_buffer_hours replaces the default."""
    # hours_to_runout_p90 = 30h, buffer = 24h → (30 - 24) / 24 = 0.25 → floor = 0
    result = compute_safe_to_delay(_forecast(30.0), sla_buffer_hours=24.0)
    assert result == {"safe_to_delay_days": 0, "safe_to_delay_bucket": "none"}


@pytest.mark.parametrize(
    "hours,expected_days,expected_bucket",
    [
        # Below 1 day → "none"
        (0.0, 0, "none"),
        (6.0, 0, "none"),  # Exactly the buffer → 0 hours headroom
        (29.99, 0, "none"),  # (29.99 - 6) / 24 = 0.999… → floor = 0
        # 1–3 day → "short"
        (30.0, 1, "short"),  # floor((30 - 6) / 24) = 1
        (53.99, 1, "short"),  # floor((53.99 - 6) / 24) = 1
        (54.0, 2, "short"),  # floor((54 - 6) / 24) = 2
        (102.0, 4, "medium"),  # floor((102 - 6) / 24) = 4 → medium
        # 4–7 day → "medium"
        (100.0, 3, "short"),  # (100 - 6)/24 = 3.916… → 3 (short)
        (126.0, 5, "medium"),  # (126 - 6)/24 = 5
        (198.0, 8, "long"),  # (198 - 6)/24 = 8 → long
        # >7 day → "long"
        (200.0, 8, "long"),  # (200 - 6)/24 = 8.08 → 8 → long
    ],
)
def test_formula_and_buckets(
    hours: float, expected_days: int, expected_bucket: str
) -> None:
    """Req 3.1.1 + 3.1.2: formula and bucket assignment match the spec."""
    result = compute_safe_to_delay(_forecast(hours))
    assert result["safe_to_delay_days"] == expected_days
    assert result["safe_to_delay_bucket"] == expected_bucket


def test_negative_headroom_clamped_to_zero() -> None:
    """Req 3.1.1: max(0, …) clamp prevents negative tolerances."""
    # 5h runout with 6h buffer → raw = -1/24 days → clamp to 0
    result = compute_safe_to_delay(_forecast(5.0))
    assert result == {"safe_to_delay_days": 0, "safe_to_delay_bucket": "none"}


def test_infinite_runout_returns_long_bucket() -> None:
    """A tank that will not run out gets the maximum delay tolerance."""
    result = compute_safe_to_delay(_forecast(math.inf))
    assert result["safe_to_delay_bucket"] == "long"
    assert result["safe_to_delay_days"] == math.inf


def test_accepts_mapping_forecast() -> None:
    """Dict-shaped forecasts work without wrapping in a model."""
    result = compute_safe_to_delay({"hours_to_runout_p90": 96.0})
    # (96 - 6) / 24 = 3.75 → floor = 3 → short
    assert result == {"safe_to_delay_days": 3, "safe_to_delay_bucket": "short"}


def test_negative_buffer_is_clamped_to_zero() -> None:
    """A negative SLA buffer is treated as zero with a warning."""
    result = compute_safe_to_delay(
        _forecast(48.0), sla_buffer_hours=-10.0
    )
    # Clamped to 0 → (48 - 0) / 24 = 2 days → short bucket
    assert result == {"safe_to_delay_days": 2, "safe_to_delay_bucket": "short"}


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_missing_field_raises_type_error() -> None:
    class Bare:
        pass

    with pytest.raises(TypeError):
        compute_safe_to_delay(Bare())


def test_missing_key_raises_type_error() -> None:
    with pytest.raises(TypeError):
        compute_safe_to_delay({})


def test_none_value_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_safe_to_delay({"hours_to_runout_p90": None})


def test_nan_value_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_safe_to_delay(_forecast(float("nan")))


def test_negative_hours_raises_value_error() -> None:
    with pytest.raises(ValueError):
        compute_safe_to_delay(_forecast(-1.0))


# ---------------------------------------------------------------------------
# compute_priority_clusters — Requirements 3.4.1, 3.4.2, 3.4.3, 3.4.4
# ---------------------------------------------------------------------------

from fuel.services.prioritization_helpers import (
    EARTH_RADIUS_MILES,
    NOISE_CLUSTER_ID,
    PriorityCluster,
    PriorityClusterAssignment,
    compute_priority_clusters,
)


def _cluster_entry(
    *,
    lat: float,
    lon: float,
    priority_bucket: str | None = None,
    fuel_grade: str | None = None,
) -> dict:
    """Build a minimal cluster-entry dict for the helper."""
    entry: dict = {"lat": lat, "lon": lon}
    if priority_bucket is not None:
        entry["priority_bucket"] = priority_bucket
    if fuel_grade is not None:
        entry["fuel_grade"] = fuel_grade
    return entry


# ----- Bucket / noise / empty-set semantics ----------------------------------


def test_compute_priority_clusters_returns_empty_for_empty_input() -> None:
    """An empty entries list produces empty assignments and no clusters."""
    assignments, clusters = compute_priority_clusters([])
    assert assignments == []
    assert clusters == []


def test_compute_priority_clusters_emits_noise_cluster_id_for_isolated_point() -> None:
    """Req 3.4.4: a point without ``min_samples`` neighbours is noise."""
    entries = [_cluster_entry(lat=40.0, lon=-72.0)]
    assignments, clusters = compute_priority_clusters(entries, min_samples=2)
    assert len(assignments) == 1
    assert assignments[0].cluster_id == NOISE_CLUSTER_ID
    assert assignments[0].cluster_size == 1
    assert assignments[0].cluster_centroid == {"lat": 40.0, "lon": -72.0}
    assert clusters == []


# ----- Density: proximity threshold (Req 3.4.1) ------------------------------


def test_points_within_eps_form_a_single_cluster() -> None:
    """Req 3.4.1: pairs within eps_miles cluster together.

    Use ~0.05 miles separation to stay well inside the default 3-mile
    radius regardless of earth-radius rounding.
    """
    entries = [
        _cluster_entry(lat=40.000, lon=-72.000),
        _cluster_entry(lat=40.000, lon=-72.001),
        _cluster_entry(lat=40.001, lon=-72.001),
    ]
    assignments, clusters = compute_priority_clusters(entries)
    assert {a.cluster_id for a in assignments} == {"cluster_0"}
    assert len(clusters) == 1
    assert clusters[0].cluster_id == "cluster_0"
    assert clusters[0].member_count == 3


def test_points_beyond_eps_fall_into_separate_clusters_or_noise() -> None:
    """Req 3.4.1: pairs > eps_miles apart do not share a cluster."""
    # Two tight pairs separated by ~70 miles.
    entries = [
        _cluster_entry(lat=40.000, lon=-72.000),
        _cluster_entry(lat=40.001, lon=-72.001),
        _cluster_entry(lat=41.000, lon=-73.000),
        _cluster_entry(lat=41.001, lon=-73.001),
    ]
    _assignments, clusters = compute_priority_clusters(entries, eps_miles=3.0)
    assert len(clusters) == 2
    # Each dense cluster has two members; no singletons.
    assert all(c.member_count == 2 for c in clusters)


def test_eps_is_interpreted_in_miles_not_radians() -> None:
    """Req 3.4.1: eps is miles — two cities ~12mi apart with eps=3 are noise."""
    # ~12 miles apart along a meridian (1° lat ≈ 69 miles, so ~0.17°).
    entries = [
        _cluster_entry(lat=40.00, lon=-72.0),
        _cluster_entry(lat=40.17, lon=-72.0),
    ]
    assignments, clusters = compute_priority_clusters(
        entries, eps_miles=3.0, min_samples=2
    )
    # Both points must be noise because they are well beyond 3 miles.
    assert all(a.cluster_id == NOISE_CLUSTER_ID for a in assignments)
    assert clusters == []


# ----- Centroid + size on assignments (Req 3.4.2) ----------------------------


def test_assignment_size_and_centroid_match_cluster_membership() -> None:
    """Req 3.4.2: cluster_size / cluster_centroid on every entry match the
    members' own size / mean lat/lon.
    """
    entries = [
        _cluster_entry(lat=40.000, lon=-72.000),
        _cluster_entry(lat=40.002, lon=-72.002),
    ]
    assignments, _ = compute_priority_clusters(entries)
    for a in assignments:
        assert a.cluster_size == 2
        assert a.cluster_centroid == {
            "lat": pytest.approx(40.001, abs=1e-9),
            "lon": pytest.approx(-72.001, abs=1e-9),
        }


# ----- Cluster aggregates (Req 3.4.3) ----------------------------------------


def test_cluster_surfaces_highest_priority_bucket() -> None:
    """Req 3.4.3: cluster row carries the most urgent bucket among members."""
    entries = [
        _cluster_entry(lat=40.0, lon=-72.0, priority_bucket="low"),
        _cluster_entry(lat=40.001, lon=-72.0, priority_bucket="medium"),
        _cluster_entry(lat=40.002, lon=-72.0, priority_bucket="critical"),
    ]
    _, clusters = compute_priority_clusters(entries)
    assert len(clusters) == 1
    assert clusters[0].highest_priority_bucket == "critical"


def test_cluster_handles_unrecognized_bucket_values_gracefully() -> None:
    """An unknown bucket string does not become ``highest_priority_bucket``."""
    entries = [
        _cluster_entry(lat=40.0, lon=-72.0, priority_bucket="bogus"),
        _cluster_entry(lat=40.001, lon=-72.0, priority_bucket="high"),
    ]
    _, clusters = compute_priority_clusters(entries)
    assert clusters[0].highest_priority_bucket == "high"


def test_cluster_collects_all_fuel_grades_present() -> None:
    """Req 3.4.3: cluster row lists every fuel grade represented."""
    entries = [
        _cluster_entry(lat=40.0, lon=-72.0, fuel_grade="DIESEL_2"),
        _cluster_entry(lat=40.001, lon=-72.0, fuel_grade="HEATING_OIL"),
        _cluster_entry(lat=40.002, lon=-72.0, fuel_grade="DIESEL_2"),
    ]
    _, clusters = compute_priority_clusters(entries)
    assert clusters[0].fuel_grades == ["DIESEL_2", "HEATING_OIL"]


def test_cluster_without_buckets_has_none_highest_bucket() -> None:
    """Missing bucket fields produce None (no crash, no fake bucket)."""
    entries = [
        _cluster_entry(lat=40.0, lon=-72.0),
        _cluster_entry(lat=40.001, lon=-72.0),
    ]
    _, clusters = compute_priority_clusters(entries)
    assert clusters[0].highest_priority_bucket is None
    assert clusters[0].fuel_grades == []


# ----- Parameter validation -------------------------------------------------


def test_compute_priority_clusters_rejects_non_positive_eps() -> None:
    with pytest.raises(ValueError):
        compute_priority_clusters(
            [_cluster_entry(lat=40.0, lon=-72.0)], eps_miles=0.0
        )


def test_compute_priority_clusters_rejects_sub_1_min_samples() -> None:
    with pytest.raises(ValueError):
        compute_priority_clusters(
            [_cluster_entry(lat=40.0, lon=-72.0)], min_samples=0
        )


def test_compute_priority_clusters_rejects_missing_coordinates() -> None:
    with pytest.raises(TypeError):
        compute_priority_clusters([{"priority_bucket": "high"}])


def test_compute_priority_clusters_rejects_out_of_range_latitude() -> None:
    with pytest.raises(ValueError):
        compute_priority_clusters([_cluster_entry(lat=91.0, lon=-72.0)])


def test_compute_priority_clusters_rejects_out_of_range_longitude() -> None:
    with pytest.raises(ValueError):
        compute_priority_clusters([_cluster_entry(lat=40.0, lon=181.0)])


# ----- Non-dict inputs ------------------------------------------------------


class _EntryObject:
    """Stand-in for a Pydantic priority model exposing attribute access."""

    def __init__(self, lat, lon, priority_bucket=None, fuel_grade=None):
        self.location_lat = lat
        self.location_lon = lon
        self.priority_bucket = priority_bucket
        self.fuel_grade = fuel_grade


def test_attribute_access_entries_work() -> None:
    """Attribute access and mapping access produce identical clusters."""
    entries = [
        _EntryObject(40.0, -72.0, "critical", "DIESEL_2"),
        _EntryObject(40.001, -72.001, "high", "DIESEL_2"),
    ]
    assignments, clusters = compute_priority_clusters(entries)
    assert len(clusters) == 1
    assert clusters[0].member_count == 2
    assert clusters[0].highest_priority_bucket == "critical"
    assert clusters[0].fuel_grades == ["DIESEL_2"]
    assert assignments[0].cluster_id == "cluster_0"


# ----- Earth-radius constant sanity ----------------------------------------


def test_earth_radius_miles_is_reasonable_value() -> None:
    """Smoke check the constant matches Earth's mean radius in miles."""
    # Earth's mean radius is ~3959 miles; tolerate any value within 10mi.
    assert 3948 <= EARTH_RADIUS_MILES <= 3970
