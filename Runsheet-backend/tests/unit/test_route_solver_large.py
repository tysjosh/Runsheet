"""Unit tests for the large-route solver.

Covers :func:`Agents.support.route_solver.optimize_route_large` and its
helper :func:`Agents.support.route_solver._stitch_clusters_via_depot` which
implement the fuel-ops-hardening scaling path from Task 4.6.

Validates:
    - Requirement 2.3.1 (solver produces a feasible route for up to 100 stops)
    - Requirement 2.3.2 (k-means clustering pre-pass engages above 30 stops;
      per-cluster nearest-neighbor + 2-opt; depot stitching)
    - Requirement 2.3.3 (``overlay.routing_max_stops`` cap, default 100,
      surfaces as ``stop_cap_exceeded``)
    - Requirement 2.3.4 (objective_value, runtime_ms, iterations are recorded
      on every Route_Plan output)
    - Requirement 2.3.5 (output preserves every input stop exactly once and
      begins/ends at the depot)
"""
from __future__ import annotations

import math
import random
from typing import Dict, List

import pytest

from Agents.support.route_solver import (
    CLUSTERING_STOP_THRESHOLD,
    DEFAULT_MAX_STOPS_PER_CLUSTER,
    DEFAULT_ROUTING_MAX_STOPS,
    StopCapExceededError,
    _stitch_clusters_via_depot,
    build_distance_matrix,
    optimize_route_large,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

DEPOT = {"lat": 40.0, "lon": -74.0}


def _grid_stops(n: int, rng: random.Random) -> List[Dict[str, float]]:
    """Build N customer stops within a reasonable US lat/lon box.

    Coordinates are bounded inside (-89, 89) / (-179, 179) so the Haversine
    validation in :func:`compute_distance` never trips.
    """
    stops: List[Dict[str, float]] = []
    for _ in range(n):
        stops.append(
            {
                "lat": rng.uniform(25.0, 48.0),
                "lon": rng.uniform(-123.0, -70.0),
            }
        )
    return stops


def _locations_with_depot(n_customers: int, seed: int = 0) -> List[Dict[str, float]]:
    rng = random.Random(seed)
    return [DEPOT] + _grid_stops(n_customers, rng)


# ---------------------------------------------------------------------------
# Requirement 2.3.3 — stop cap enforcement
# ---------------------------------------------------------------------------

class TestStopCapEnforcement:
    """``optimize_route_large`` must reject requests over the cap."""

    def test_raises_when_customer_stops_exceed_cap(self):
        locations = _locations_with_depot(DEFAULT_ROUTING_MAX_STOPS + 1, seed=1)
        with pytest.raises(StopCapExceededError) as excinfo:
            optimize_route_large(locations, start_index=0)
        assert excinfo.value.stop_count == DEFAULT_ROUTING_MAX_STOPS + 1
        assert excinfo.value.max_stops == DEFAULT_ROUTING_MAX_STOPS
        assert "stop_cap_exceeded" in str(excinfo.value)

    def test_accepts_exactly_cap_count(self):
        locations = _locations_with_depot(DEFAULT_ROUTING_MAX_STOPS, seed=2)
        result = optimize_route_large(locations, start_index=0)
        assert result["order"][0] == 0
        assert result["order"][-1] == 0
        # Customer stops exactly equal cap: solver must succeed.
        assert len(result["order"]) == DEFAULT_ROUTING_MAX_STOPS + 2

    def test_custom_cap_is_honored(self):
        locations = _locations_with_depot(10, seed=3)
        # 10 customer stops, but configured cap is 5 → must raise.
        with pytest.raises(StopCapExceededError) as excinfo:
            optimize_route_large(locations, start_index=0, max_stops=5)
        assert excinfo.value.stop_count == 10
        assert excinfo.value.max_stops == 5

    def test_stop_cap_error_is_valueerror_subclass(self):
        # Callers may handle the error via its base class; ensure that
        # contract holds so the route-planning endpoint can catch both.
        assert issubclass(StopCapExceededError, ValueError)


# ---------------------------------------------------------------------------
# Requirement 2.3.5 — output preservation (every stop exactly once, depot at
# both ends)
# ---------------------------------------------------------------------------

class TestOutputPreservation:
    """The returned order must begin/end at the depot and hit every stop."""

    @pytest.mark.parametrize("n_customers", [1, 5, CLUSTERING_STOP_THRESHOLD, 45, 75, 100])
    def test_order_preserves_every_stop_exactly_once(self, n_customers):
        locations = _locations_with_depot(n_customers, seed=11)
        result = optimize_route_large(locations, start_index=0)

        order = result["order"]
        # Begins and ends at the depot.
        assert order[0] == 0
        assert order[-1] == 0

        # Every customer index appears exactly once between the depot
        # endpoints (inner segment).
        inner = order[1:-1]
        expected_customers = set(range(1, n_customers + 1))
        assert sorted(inner) == sorted(expected_customers)
        # No customer repeats.
        assert len(set(inner)) == len(inner) == n_customers

    def test_zero_customer_stops_returns_only_depot(self):
        # Edge case: tenant requests a plan with no deliveries.
        result = optimize_route_large([DEPOT], start_index=0)
        assert result["order"] == [0]
        assert result["objective_value"] == 0.0
        assert result["iterations"] == 0


# ---------------------------------------------------------------------------
# Requirement 2.3.4 — metrics recorded on every plan
# ---------------------------------------------------------------------------

class TestPlanMetricsRecorded:
    """Every solver output must carry objective_value, runtime_ms, iterations."""

    @pytest.mark.parametrize("n_customers", [5, 20, 40, 80])
    def test_required_metric_fields_present(self, n_customers):
        locations = _locations_with_depot(n_customers, seed=21)
        result = optimize_route_large(locations, start_index=0)

        # Keys every Route_Plan must carry (Req 2.3.4).
        assert "objective_value" in result
        assert "runtime_ms" in result
        assert "iterations" in result
        assert "order" in result
        assert "clusters_used" in result

        # Distance is non-negative (Req 2.3.5 supporting invariant).
        assert result["objective_value"] >= 0.0

        # Runtime and iterations are non-negative integers.
        assert isinstance(result["runtime_ms"], int)
        assert result["runtime_ms"] >= 0
        assert isinstance(result["iterations"], int)
        assert result["iterations"] >= 0

    def test_total_distance_matches_distance_matrix_sum(self):
        # Recompute the tour cost from the matrix and check it equals the
        # reported objective_value (within float tolerance). This guards
        # against silent stitching-vs-reporting drift.
        locations = _locations_with_depot(45, seed=22)
        result = optimize_route_large(locations, start_index=0)
        matrix = build_distance_matrix(locations)

        order = result["order"]
        recomputed = sum(
            matrix[order[i]][order[i + 1]] for i in range(len(order) - 1)
        )
        assert result["objective_value"] == pytest.approx(recomputed, rel=1e-6)


# ---------------------------------------------------------------------------
# Requirement 2.3.2 — clustering pre-pass engages above threshold
# ---------------------------------------------------------------------------

class TestClusteringPrePass:
    """Stops > 30 must trigger the k-means + per-cluster 2-opt pipeline."""

    def test_small_problem_uses_single_cluster(self):
        # At or below the threshold we expect a single-pass solve; the
        # contract is that clusters_used == 1 and we never invoke sklearn.
        n = CLUSTERING_STOP_THRESHOLD  # exactly at threshold → single-pass
        locations = _locations_with_depot(n, seed=31)
        result = optimize_route_large(locations, start_index=0)
        assert result["clusters_used"] == 1

    def test_large_problem_uses_multiple_clusters(self):
        n = 60  # well above threshold
        locations = _locations_with_depot(n, seed=32)
        result = optimize_route_large(
            locations, start_index=0, max_stops_per_cluster=DEFAULT_MAX_STOPS_PER_CLUSTER
        )
        # ceil(60 / 15) == 4
        expected_clusters = math.ceil(n / DEFAULT_MAX_STOPS_PER_CLUSTER)
        assert result["clusters_used"] == expected_clusters

    def test_large_problem_still_preserves_stops(self):
        # Cross-check Req 2.3.5 on the clustering code path specifically.
        n = 75
        locations = _locations_with_depot(n, seed=33)
        result = optimize_route_large(locations, start_index=0)
        inner = result["order"][1:-1]
        assert sorted(inner) == list(range(1, n + 1))
        assert result["order"][0] == 0
        assert result["order"][-1] == 0


# ---------------------------------------------------------------------------
# Direct tests for the stitching helper
# ---------------------------------------------------------------------------

class TestStitchClustersViaDepot:
    """_stitch_clusters_via_depot must concatenate cluster tours through the depot."""

    def test_single_cluster_returns_its_inner_sequence(self):
        # Depot=0, two stops=1,2 — cluster tour already closed at depot.
        locations = [
            {"lat": 40.0, "lon": -74.0},
            {"lat": 41.0, "lon": -74.5},
            {"lat": 41.5, "lon": -74.2},
        ]
        matrix = build_distance_matrix(locations)
        stitched, total = _stitch_clusters_via_depot(
            [[0, 1, 2, 0]], distance_matrix=matrix, depot_index=0
        )
        assert stitched[0] == 0
        assert stitched[-1] == 0
        assert sorted(stitched[1:-1]) == [1, 2]
        assert total == pytest.approx(
            matrix[0][1] + matrix[1][2] + matrix[2][0], rel=1e-6
        )

    def test_empty_cluster_list_returns_depot_only(self):
        locations = [{"lat": 40.0, "lon": -74.0}]
        matrix = build_distance_matrix(locations)
        stitched, total = _stitch_clusters_via_depot(
            [], distance_matrix=matrix, depot_index=0
        )
        assert stitched == [0]
        assert total == 0.0

    def test_multiple_clusters_dedup_depot_between_tours(self):
        # Two clusters that would naively be [0,1,2,0] + [0,3,4,0] = 6 hops
        # with a duplicated depot in the middle. Stitching must drop the
        # duplicate so the tour visits each customer exactly once.
        locations = [
            {"lat": 40.0, "lon": -74.0},   # 0 depot
            {"lat": 40.5, "lon": -74.1},   # 1
            {"lat": 40.6, "lon": -74.2},   # 2
            {"lat": 41.5, "lon": -75.0},   # 3
            {"lat": 41.6, "lon": -75.1},   # 4
        ]
        matrix = build_distance_matrix(locations)
        stitched, _ = _stitch_clusters_via_depot(
            [[0, 1, 2, 0], [0, 3, 4, 0]],
            distance_matrix=matrix,
            depot_index=0,
        )
        # Each customer appears exactly once.
        assert sorted(stitched[1:-1]) == [1, 2, 3, 4]
        # Depot only at endpoints, never in the middle.
        assert stitched.count(0) == 2
        assert stitched[0] == 0 and stitched[-1] == 0


# ---------------------------------------------------------------------------
# Invalid input handling
# ---------------------------------------------------------------------------

class TestInvalidInputs:
    def test_bad_start_index_raises(self):
        with pytest.raises(ValueError):
            optimize_route_large(_locations_with_depot(5, seed=41), start_index=999)
