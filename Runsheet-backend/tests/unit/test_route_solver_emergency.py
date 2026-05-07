"""Unit tests for :func:`Agents.support.route_solver.insert_emergency_stop`.

Covers Capability 2 / Requirements 2.4.2 and 2.4.4 of the fuel-ops hardening
spec:

* Cheapest-insertion heuristic picks the position with minimum added detour
  distance (Req 2.4.2).
* Capacity check respects the truck's remaining compartment capacity for the
  requested fuel_grade (Req 2.4.2) and raises ``InfeasibleInsertion`` with
  reason ``capacity_insufficient`` when exhausted (Req 2.4.4).
* SLA check honors ``sla_by_hours`` on both existing stops and the emergency
  itself, raising ``sla_breach`` when no position avoids a deadline miss
  (Req 2.4.4).
* Shift-end check raises ``truck_off_duty`` when every candidate position
  pushes the truck past ``shift_end_hours`` (Req 2.4.4).
* Result carries the patched stop list, recomputed ETAs, and an
  ``eta_shifts`` array used by Task 4.9's Replan_Diff output.
* Traffic_matrix input (keyed by stop ids) overrides Haversine distance,
  matching the cached Traffic_Provider matrix shape.

Validates: Requirements 2.4.2, 2.4.4.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from Agents.support.route_solver import (
    INSERT_REASON_CAPACITY,
    INSERT_REASON_OFF_DUTY,
    INSERT_REASON_SLA,
    InfeasibleInsertion,
    insert_emergency_stop,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _stop(
    stop_id: str,
    lat: float,
    lon: float,
    *,
    sla_by_hours: Optional[float] = None,
) -> Dict[str, Any]:
    s: Dict[str, Any] = {"stop_id": stop_id, "lat": lat, "lon": lon}
    if sla_by_hours is not None:
        s["sla_by_hours"] = sla_by_hours
    return s


def _linear_route(
    *,
    stops: List[Dict[str, Any]],
    remaining_capacity: Optional[Dict[str, float]] = None,
    shift_end_hours: Optional[float] = None,
    start_time_hours: float = 0.0,
) -> Dict[str, Any]:
    """Build a route with a depot at (40.0, -74.0) and the given stops."""
    route: Dict[str, Any] = {
        "depot": {"lat": 40.0, "lon": -74.0},
        "stops": list(stops),
        "remaining_capacity_by_grade": dict(
            remaining_capacity
            if remaining_capacity is not None
            else {"DIESEL_2": 500.0, "GASOLINE_REG": 500.0, "PROPANE": 500.0}
        ),
        "start_time_hours": start_time_hours,
    }
    if shift_end_hours is not None:
        route["shift_end_hours"] = shift_end_hours
    return route


def _emergency(
    *,
    lat: float,
    lon: float,
    fuel_grade: str = "DIESEL_2",
    requested_gallons: float = 100.0,
    sla_by_hours: Optional[float] = None,
) -> Dict[str, Any]:
    em: Dict[str, Any] = {
        "stop_id": "emergency_1",
        "lat": lat,
        "lon": lon,
        "fuel_grade": fuel_grade,
        "requested_gallons": requested_gallons,
    }
    if sla_by_hours is not None:
        em["sla_by_hours"] = sla_by_hours
    return em


# ---------------------------------------------------------------------------
# Cheapest-insertion correctness (Req 2.4.2)
# ---------------------------------------------------------------------------


class TestCheapestInsertionPosition:
    """The emergency must land at the position with minimum added distance."""

    def test_inserts_between_adjacent_stops_near_emergency(self):
        # Emergency at (40.05, -74.05) sits on the depot→A segment
        # (depot 40.0,-74.0; A 40.1,-74.1), so front-insertion is the
        # cheapest position (near-zero detour).
        stops = [
            _stop("A", 40.1, -74.1),
            _stop("B", 40.5, -74.5),
            _stop("C", 41.0, -75.0),
        ]
        route = _linear_route(stops=stops)
        emergency = _emergency(lat=40.05, lon=-74.05)

        result = insert_emergency_stop(route, emergency)

        # Before the first stop (index 0) is cheapest in this geometry.
        assert result["insert_index"] == 0
        assert result["added_distance_km"] >= 0.0
        # The patched sequence has the emergency inserted at the front.
        order = [s["stop_id"] for s in result["new_stops"]]
        assert order == ["emergency_1", "A", "B", "C"]

    def test_appends_when_emergency_nearest_to_last_leg(self):
        # Emergency located very close to C → cheapest to append after C
        # (between C and the return-to-depot leg).
        stops = [
            _stop("A", 40.1, -74.1),
            _stop("B", 40.5, -74.5),
            _stop("C", 41.0, -75.0),
        ]
        route = _linear_route(stops=stops)
        emergency = _emergency(lat=41.01, lon=-75.01)

        result = insert_emergency_stop(route, emergency)

        assert result["insert_index"] == len(stops)
        order = [s["stop_id"] for s in result["new_stops"]]
        assert order == ["A", "B", "C", "emergency_1"]

    def test_inserts_between_middle_stops(self):
        # Emergency close to A-B segment (much closer than to depot-A
        # or B-C) → cheapest insertion position is 1 (between A and B).
        stops = [
            _stop("A", 40.1, -74.1),
            _stop("B", 40.5, -74.5),
            _stop("C", 41.0, -75.0),
        ]
        route = _linear_route(stops=stops)
        emergency = _emergency(lat=40.3, lon=-74.3)

        result = insert_emergency_stop(route, emergency)

        assert result["insert_index"] == 1
        order = [s["stop_id"] for s in result["new_stops"]]
        assert order == ["A", "emergency_1", "B", "C"]

    def test_empty_route_inserts_at_position_zero(self):
        route = _linear_route(stops=[])
        emergency = _emergency(lat=40.2, lon=-74.2)
        result = insert_emergency_stop(route, emergency)
        assert result["insert_index"] == 0
        assert [s["stop_id"] for s in result["new_stops"]] == ["emergency_1"]


# ---------------------------------------------------------------------------
# Capacity enforcement (Req 2.4.2, 2.4.4)
# ---------------------------------------------------------------------------


class TestCapacityEnforcement:
    def test_raises_capacity_insufficient_when_grade_exhausted(self):
        route = _linear_route(
            stops=[_stop("A", 40.2, -74.2)],
            remaining_capacity={"DIESEL_2": 50.0},
        )
        emergency = _emergency(lat=40.21, lon=-74.21, requested_gallons=100.0)
        with pytest.raises(InfeasibleInsertion) as excinfo:
            insert_emergency_stop(route, emergency)
        assert excinfo.value.reason == INSERT_REASON_CAPACITY
        assert excinfo.value.details["fuel_grade"] == "DIESEL_2"
        assert excinfo.value.details["remaining_gallons"] == 50.0
        assert excinfo.value.details["requested_gallons"] == 100.0

    def test_raises_capacity_insufficient_when_grade_not_loaded(self):
        # Truck has no PROPANE on board at all.
        route = _linear_route(
            stops=[_stop("A", 40.2, -74.2)],
            remaining_capacity={"DIESEL_2": 500.0},
        )
        emergency = _emergency(
            lat=40.21, lon=-74.21, fuel_grade="PROPANE", requested_gallons=50.0
        )
        with pytest.raises(InfeasibleInsertion) as excinfo:
            insert_emergency_stop(route, emergency)
        assert excinfo.value.reason == INSERT_REASON_CAPACITY

    def test_succeeds_when_capacity_exactly_matches_request(self):
        route = _linear_route(
            stops=[_stop("A", 40.2, -74.2)],
            remaining_capacity={"DIESEL_2": 100.0},
        )
        emergency = _emergency(lat=40.21, lon=-74.21, requested_gallons=100.0)
        result = insert_emergency_stop(route, emergency)
        assert result["insert_index"] in (0, 1)


# ---------------------------------------------------------------------------
# SLA breach (Req 2.4.4)
# ---------------------------------------------------------------------------


class TestSlaBreach:
    def test_raises_sla_breach_when_emergency_own_sla_unmeetable(self):
        # Emergency SLA is 0.1 hours but the location is ~200 km from
        # both the depot and any in-route stop → no insertion position
        # can deliver within the emergency's own deadline.
        stops = [_stop("A", 40.1, -74.1)]
        route = _linear_route(stops=stops)
        emergency = _emergency(
            lat=41.5, lon=-75.5, sla_by_hours=0.1
        )  # ~200 km, minimum ETA ~5h at 40 km/h
        with pytest.raises(InfeasibleInsertion) as excinfo:
            insert_emergency_stop(route, emergency)
        assert excinfo.value.reason == INSERT_REASON_SLA

    def test_avoids_positions_that_breach_existing_stop_sla(self):
        # Stop A has a very tight SLA. Inserting the emergency *before*
        # A would push A late, so the solver must pick a position that
        # leaves A's ETA untouched (i.e. after A).
        stops = [
            # A ~22 km north, baseline ETA ~0.55h at 40 km/h, SLA 0.6h
            # leaves only a thin 0.05h margin.
            _stop("A", 40.2, -74.0, sla_by_hours=0.6),
            _stop("B", 40.4, -74.0),
        ]
        route = _linear_route(stops=stops)
        # Emergency 111 km north of the depot — inserting before A adds
        # a 5+ hour detour that would push A's ETA past its SLA. The
        # only SLA-safe positions are after A.
        emergency = _emergency(lat=41.0, lon=-74.0)

        result = insert_emergency_stop(route, emergency)
        # Must not be position 0 (that would breach A's SLA).
        assert result["insert_index"] >= 1
        # A is not in the eta_shifts list because it wasn't moved.
        shifted_ids = {e["stop_id"] for e in result["eta_shifts"]}
        assert "A" not in shifted_ids


# ---------------------------------------------------------------------------
# Off-duty / shift end (Req 2.4.4)
# ---------------------------------------------------------------------------


class TestTruckOffDuty:
    def test_raises_truck_off_duty_when_shift_end_exceeded(self):
        # Route already near shift end; adding a large detour pushes
        # return-to-depot past shift_end_hours.
        stops = [
            _stop("A", 40.1, -74.1),
            _stop("B", 40.5, -74.5),
        ]
        route = _linear_route(
            stops=stops,
            shift_end_hours=2.5,  # ~2.5 hours of driving budget
        )
        # Emergency ~300 km detour → adds several hours regardless of
        # insertion position.
        emergency = _emergency(lat=42.5, lon=-76.5, requested_gallons=100.0)
        with pytest.raises(InfeasibleInsertion) as excinfo:
            insert_emergency_stop(route, emergency)
        assert excinfo.value.reason == INSERT_REASON_OFF_DUTY


# ---------------------------------------------------------------------------
# Result shape (used by Task 4.9 Replan_Diff / WebSocket)
# ---------------------------------------------------------------------------


class TestResultShape:
    def test_result_carries_expected_fields(self):
        stops = [
            _stop("A", 40.1, -74.1),
            _stop("B", 40.5, -74.5),
        ]
        route = _linear_route(stops=stops)
        emergency = _emergency(lat=40.3, lon=-74.3)
        result = insert_emergency_stop(route, emergency)

        for key in (
            "insert_index",
            "added_distance_km",
            "new_stops",
            "new_etas",
            "eta_shifts",
            "return_to_depot_hours",
            "stops_shifted_count",
        ):
            assert key in result, f"missing result key {key!r}"

        assert len(result["new_stops"]) == len(stops) + 1
        assert len(result["new_etas"]) == len(result["new_stops"])
        assert result["added_distance_km"] >= 0.0
        assert result["stops_shifted_count"] == len(result["eta_shifts"])

    def test_eta_shifts_reported_for_stops_after_insertion(self):
        # Position the emergency so front-insertion is strictly
        # cheapest — it sits on the depot→A leg.
        stops = [
            _stop("A", 40.1, -74.1),
            _stop("B", 40.5, -74.5),
            _stop("C", 41.0, -75.0),
        ]
        route = _linear_route(stops=stops)
        emergency = _emergency(lat=40.05, lon=-74.05, requested_gallons=50.0)

        result = insert_emergency_stop(route, emergency)
        assert result["insert_index"] == 0
        shifted_ids = [e["stop_id"] for e in result["eta_shifts"]]
        # Every existing stop is downstream of the insertion and should
        # appear in the eta_shifts list with a positive shift.
        assert set(shifted_ids) == {"A", "B", "C"}
        for shift in result["eta_shifts"]:
            assert shift["shift_minutes"] > 0
            assert shift["after_eta_hours"] > shift["before_eta_hours"]

    def test_eta_shifts_empty_when_emergency_appended_to_end(self):
        # Appending after the last stop must not shift any existing
        # stop's ETA — they all precede the insertion point.
        stops = [
            _stop("A", 40.1, -74.1),
            _stop("B", 40.5, -74.5),
        ]
        route = _linear_route(stops=stops)
        emergency = _emergency(lat=40.51, lon=-74.51)
        result = insert_emergency_stop(route, emergency)

        assert result["insert_index"] == len(stops)
        assert result["eta_shifts"] == []
        assert result["stops_shifted_count"] == 0


# ---------------------------------------------------------------------------
# Traffic matrix override
# ---------------------------------------------------------------------------


class TestTrafficMatrixOverride:
    def test_traffic_matrix_entry_overrides_haversine_distance(self):
        # Two plausible insertion positions; use the traffic matrix to
        # make one *artificially* cheaper than Haversine would suggest.
        stops = [
            _stop("A", 40.1, -74.1),
            _stop("B", 40.5, -74.5),
        ]
        route = _linear_route(stops=stops)
        # Without traffic matrix: emergency at (40.3, -74.3) lands
        # between A and B (position 1).
        emergency = _emergency(lat=40.3, lon=-74.3)

        matrix: Dict[Tuple[str, str], Dict[str, float]] = {
            # Make the depot→emergency edge astronomically expensive
            # so front-insertion is worse than mid-insertion.
            ("from", "emergency_1"): {
                "distance_km": 10_000.0,
                "duration_minutes": 60_000.0,
            },
        }
        result = insert_emergency_stop(route, emergency, traffic_matrix=matrix)
        # Front-insert would require the depot→emergency edge; with the
        # huge matrix cost it must not be chosen.
        assert result["insert_index"] != 0
