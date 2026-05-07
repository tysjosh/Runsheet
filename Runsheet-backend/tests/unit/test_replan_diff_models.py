"""
Unit tests for :mod:`Agents.support.replan_diff_models`.

Covers Capability 2 / Requirement 2.5.1 of the fuel-ops hardening spec:

* :class:`ReplanDiff` Pydantic contract — required ``original_route_id`` /
  ``patched_route_id``, typed nested arrays, tz-aware ``generated_at``,
  and a lossless JSON round-trip (Req 2.5.1 & 2.5.5).
* Nested submodels (:class:`StopRef`, :class:`ReorderedStop`,
  :class:`ReassignedStop`, :class:`QuantityChange`, :class:`EtaShift`)
  reject unknown fields and enforce bounds on gallons and indices.
* :func:`compute_replan_diff` extracts added / removed / reordered /
  quantity / ETA-shift changes from Pydantic ``RoutePlan``s, plain
  dicts, and attribute objects; marks reassignments only when trucks
  differ; handles malformed stops gracefully.

Validates: Requirements 2.5.1.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

from Agents.support.fuel_distribution_models import (
    FuelGrade,
    RoutePlan,
    RouteStop,
)
from Agents.support.replan_diff_models import (
    EtaShift,
    QuantityChange,
    ReassignedStop,
    ReorderedStop,
    ReplanDiff,
    StopRef,
    compute_replan_diff,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mvp_route(
    route_id: str,
    truck_id: str,
    stops: List[Dict[str, Any]],
    *,
    tenant_id: str = "tenant_a",
) -> RoutePlan:
    """Build an MVP :class:`RoutePlan` from plain stop dicts."""

    built_stops = [
        RouteStop(
            station_id=stop["station_id"],
            eta=stop["eta"],
            drop=stop["drop"],
            sequence=stop.get("sequence", idx),
        )
        for idx, stop in enumerate(stops)
    ]
    return RoutePlan(
        route_id=route_id,
        truck_id=truck_id,
        plan_id="plan_1",
        stops=built_stops,
        distance_km=12.3,
        eta_confidence=0.9,
        tenant_id=tenant_id,
    )


def _stop(
    station_id: str,
    eta: str,
    drop: Dict[str, float],
    sequence: int,
) -> Dict[str, Any]:
    return {
        "station_id": station_id,
        "eta": eta,
        "drop": drop,
        "sequence": sequence,
    }


# ---------------------------------------------------------------------------
# Nested submodel validation
# ---------------------------------------------------------------------------


class TestStopRef:
    def test_minimum_fields_accept(self) -> None:
        ref = StopRef(stop_id="s1", index=0)
        assert ref.stop_id == "s1"
        assert ref.index == 0
        assert ref.gallons is None
        assert ref.product_code is None
        assert ref.eta is None

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            StopRef(stop_id="s1", index=0, extra="nope")  # type: ignore[call-arg]

    def test_rejects_negative_index(self) -> None:
        with pytest.raises(ValidationError):
            StopRef(stop_id="s1", index=-1)

    def test_rejects_negative_gallons(self) -> None:
        with pytest.raises(ValidationError):
            StopRef(stop_id="s1", index=0, gallons=-0.1)

    def test_rejects_blank_stop_id(self) -> None:
        with pytest.raises(ValidationError):
            StopRef(stop_id="", index=0)


class TestReorderedStop:
    def test_accepts_valid(self) -> None:
        row = ReorderedStop(stop_id="s1", before_index=1, after_index=3)
        assert row.after_index == 3

    def test_rejects_negative_index(self) -> None:
        with pytest.raises(ValidationError):
            ReorderedStop(stop_id="s1", before_index=-1, after_index=2)


class TestReassignedStop:
    def test_accepts_valid(self) -> None:
        row = ReassignedStop(
            stop_id="s1", from_truck_id="t1", to_truck_id="t2"
        )
        assert row.from_truck_id == "t1"

    def test_rejects_blank_truck(self) -> None:
        with pytest.raises(ValidationError):
            ReassignedStop(stop_id="s1", from_truck_id="", to_truck_id="t2")


class TestQuantityChange:
    def test_accepts_valid(self) -> None:
        row = QuantityChange(
            stop_id="s1",
            before_gallons=100.0,
            after_gallons=150.5,
            product_code="DIESEL_2",
        )
        assert row.product_code == "DIESEL_2"

    def test_rejects_negative_gallons(self) -> None:
        with pytest.raises(ValidationError):
            QuantityChange(
                stop_id="s1", before_gallons=-1.0, after_gallons=10.0
            )


class TestEtaShift:
    def test_accepts_valid(self) -> None:
        row = EtaShift(
            stop_id="s1",
            before_eta="2025-01-01T08:00:00+00:00",
            after_eta="2025-01-01T09:15:00+00:00",
            shift_minutes=75.0,
        )
        assert row.shift_minutes == 75.0

    def test_allows_negative_shift(self) -> None:
        # Stops moving earlier in the day is a legitimate scenario.
        row = EtaShift(
            stop_id="s1",
            before_eta="2025-01-01T09:00:00+00:00",
            after_eta="2025-01-01T08:30:00+00:00",
            shift_minutes=-30.0,
        )
        assert row.shift_minutes == -30.0


# ---------------------------------------------------------------------------
# ReplanDiff top-level model
# ---------------------------------------------------------------------------


class TestReplanDiffModel:
    def test_requires_route_ids(self) -> None:
        with pytest.raises(ValidationError):
            ReplanDiff(original_route_id="", patched_route_id="r2")

    def test_defaults_diff_id_and_generated_at(self) -> None:
        diff = ReplanDiff(original_route_id="r1", patched_route_id="r2")
        assert diff.diff_id
        assert diff.generated_at.tzinfo is not None

    def test_rejects_naive_generated_at(self) -> None:
        with pytest.raises(ValidationError):
            ReplanDiff(
                original_route_id="r1",
                patched_route_id="r2",
                generated_at=datetime(2025, 1, 1, 12, 0, 0),
            )

    def test_summary_counts_match_arrays(self) -> None:
        diff = ReplanDiff(
            original_route_id="r1",
            patched_route_id="r2",
            added_stops=[StopRef(stop_id="s1", index=0)],
            removed_stops=[
                StopRef(stop_id="s2", index=0),
                StopRef(stop_id="s3", index=1),
            ],
            reordered_stops=[
                ReorderedStop(stop_id="s4", before_index=0, after_index=2)
            ],
            reassigned_stops=[],
            quantity_changes=[
                QuantityChange(
                    stop_id="s5", before_gallons=100.0, after_gallons=120.0
                )
            ],
            eta_shifts=[],
        )
        assert diff.summary_counts() == {
            "added": 1,
            "removed": 2,
            "reordered": 1,
            "reassigned": 0,
            "quantity_changes": 1,
            "eta_shifts": 0,
        }

    # -- Round-trip property (Req 2.5.5 at the unit-test level) ----------

    def test_json_round_trip_preserves_fields(self) -> None:
        original = ReplanDiff(
            diff_id="diff_123",
            original_route_id="r1",
            patched_route_id="r2",
            added_stops=[
                StopRef(
                    stop_id="s_new",
                    index=4,
                    gallons=250.0,
                    product_code="DIESEL_2",
                    eta="2025-01-01T10:00:00+00:00",
                )
            ],
            removed_stops=[StopRef(stop_id="s_gone", index=2)],
            reordered_stops=[
                ReorderedStop(stop_id="s_move", before_index=0, after_index=3)
            ],
            reassigned_stops=[
                ReassignedStop(
                    stop_id="s_move2",
                    from_truck_id="t1",
                    to_truck_id="t2",
                )
            ],
            quantity_changes=[
                QuantityChange(
                    stop_id="s_q",
                    before_gallons=100.0,
                    after_gallons=150.0,
                    product_code="PROPANE",
                )
            ],
            eta_shifts=[
                EtaShift(
                    stop_id="s_eta",
                    before_eta="2025-01-01T08:00:00+00:00",
                    after_eta="2025-01-01T09:15:00+00:00",
                    shift_minutes=75.0,
                )
            ],
            generated_at=datetime(
                2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc
            ),
        )

        payload = original.model_dump_json()
        # Also sanity-check that the payload is valid JSON.
        json.loads(payload)

        restored = ReplanDiff.model_validate_json(payload)
        assert restored == original
        # Round-trip again to confirm determinism.
        assert ReplanDiff.model_validate_json(restored.model_dump_json()) == original

    def test_empty_diff_round_trips(self) -> None:
        diff = ReplanDiff(original_route_id="r1", patched_route_id="r2")
        restored = ReplanDiff.model_validate_json(diff.model_dump_json())
        assert restored == diff


# ---------------------------------------------------------------------------
# compute_replan_diff behaviour
# ---------------------------------------------------------------------------


class TestComputeReplanDiff:
    def test_identical_routes_produce_empty_diff(self) -> None:
        stops = [
            _stop("S1", "2025-01-01T08:00:00+00:00", {"AGO": 100.0}, 0),
            _stop("S2", "2025-01-01T09:00:00+00:00", {"AGO": 50.0}, 1),
        ]
        route_a = _mvp_route("r1", "t1", stops)
        route_b = _mvp_route("r1", "t1", stops)

        diff = compute_replan_diff(route_a, route_b)

        assert diff.added_stops == []
        assert diff.removed_stops == []
        assert diff.reordered_stops == []
        assert diff.reassigned_stops == []
        assert diff.quantity_changes == []
        assert diff.eta_shifts == []

    def test_detects_added_and_removed_stops(self) -> None:
        original = _mvp_route(
            "r1",
            "t1",
            [
                _stop("S1", "2025-01-01T08:00:00+00:00", {"AGO": 100.0}, 0),
                _stop("S2", "2025-01-01T09:00:00+00:00", {"AGO": 50.0}, 1),
            ],
        )
        patched = _mvp_route(
            "r2",
            "t1",
            [
                _stop("S1", "2025-01-01T08:00:00+00:00", {"AGO": 100.0}, 0),
                _stop("S3", "2025-01-01T09:30:00+00:00", {"PMS": 200.0}, 1),
            ],
        )

        diff = compute_replan_diff(original, patched)

        assert [s.stop_id for s in diff.added_stops] == ["S3"]
        assert diff.added_stops[0].gallons == 200.0
        assert [s.stop_id for s in diff.removed_stops] == ["S2"]
        assert diff.removed_stops[0].gallons == 50.0

    def test_detects_reordering_same_truck(self) -> None:
        original = _mvp_route(
            "r1",
            "t1",
            [
                _stop("S1", "2025-01-01T08:00:00+00:00", {"AGO": 100.0}, 0),
                _stop("S2", "2025-01-01T09:00:00+00:00", {"AGO": 50.0}, 1),
                _stop("S3", "2025-01-01T10:00:00+00:00", {"AGO": 75.0}, 2),
            ],
        )
        patched = _mvp_route(
            "r2",
            "t1",
            [
                _stop("S3", "2025-01-01T08:30:00+00:00", {"AGO": 75.0}, 0),
                _stop("S1", "2025-01-01T09:00:00+00:00", {"AGO": 100.0}, 1),
                _stop("S2", "2025-01-01T10:00:00+00:00", {"AGO": 50.0}, 2),
            ],
        )

        diff = compute_replan_diff(original, patched)

        reordered_ids = {
            (r.stop_id, r.before_index, r.after_index)
            for r in diff.reordered_stops
        }
        assert reordered_ids == {("S1", 0, 1), ("S2", 1, 2), ("S3", 2, 0)}
        # Same truck → no reassignments.
        assert diff.reassigned_stops == []

    def test_detects_truck_reassignment(self) -> None:
        original = _mvp_route(
            "r1",
            "t1",
            [
                _stop("S1", "2025-01-01T08:00:00+00:00", {"AGO": 100.0}, 0),
                _stop("S2", "2025-01-01T09:00:00+00:00", {"AGO": 50.0}, 1),
            ],
        )
        patched = _mvp_route(
            "r2",
            "t2",
            [
                _stop("S1", "2025-01-01T08:00:00+00:00", {"AGO": 100.0}, 0),
                _stop("S2", "2025-01-01T09:00:00+00:00", {"AGO": 50.0}, 1),
            ],
        )

        diff = compute_replan_diff(original, patched)

        assigned = {
            (r.stop_id, r.from_truck_id, r.to_truck_id)
            for r in diff.reassigned_stops
        }
        assert assigned == {("S1", "t1", "t2"), ("S2", "t1", "t2")}

    def test_detects_quantity_change(self) -> None:
        original = _mvp_route(
            "r1",
            "t1",
            [
                _stop("S1", "2025-01-01T08:00:00+00:00", {"AGO": 100.0}, 0),
            ],
        )
        patched = _mvp_route(
            "r2",
            "t1",
            [
                _stop("S1", "2025-01-01T08:00:00+00:00", {"AGO": 175.0}, 0),
            ],
        )

        diff = compute_replan_diff(original, patched)

        assert len(diff.quantity_changes) == 1
        change = diff.quantity_changes[0]
        assert change.stop_id == "S1"
        assert change.before_gallons == 100.0
        assert change.after_gallons == 175.0
        # Single-grade drop → product_code inferred from the key.
        assert change.product_code == "AGO"

    def test_detects_eta_shift_in_minutes(self) -> None:
        original = _mvp_route(
            "r1",
            "t1",
            [
                _stop("S1", "2025-01-01T08:00:00+00:00", {"AGO": 100.0}, 0),
            ],
        )
        patched = _mvp_route(
            "r2",
            "t1",
            [
                _stop("S1", "2025-01-01T09:30:00+00:00", {"AGO": 100.0}, 0),
            ],
        )

        diff = compute_replan_diff(original, patched)

        assert len(diff.eta_shifts) == 1
        shift = diff.eta_shifts[0]
        assert shift.stop_id == "S1"
        assert shift.before_eta == "2025-01-01T08:00:00+00:00"
        assert shift.after_eta == "2025-01-01T09:30:00+00:00"
        assert shift.shift_minutes == pytest.approx(90.0)

    def test_accepts_plain_dict_inputs(self) -> None:
        original = {
            "route_id": "r1",
            "truck_id": "t1",
            "stops": [
                {
                    "stop_id": "S1",
                    "planned_gallons": 200.0,
                    "eta": "2025-01-01T08:00:00+00:00",
                    "product_code": "DIESEL_2",
                }
            ],
        }
        patched = {
            "route_id": "r2",
            "truck_id": "t1",
            "stops": [
                {
                    "stop_id": "S1",
                    "planned_gallons": 250.0,
                    "eta": "2025-01-01T08:00:00+00:00",
                    "product_code": "DIESEL_2",
                }
            ],
        }

        diff = compute_replan_diff(original, patched)

        assert len(diff.quantity_changes) == 1
        assert diff.quantity_changes[0].product_code == "DIESEL_2"
        assert diff.added_stops == []
        assert diff.removed_stops == []

    def test_accepts_attribute_objects(self) -> None:
        original = SimpleNamespace(
            route_id="r1",
            truck_id="t1",
            stops=[
                SimpleNamespace(
                    stop_id="S1",
                    planned_gallons=100.0,
                    eta="2025-01-01T08:00:00+00:00",
                    product_code="PROPANE",
                )
            ],
        )
        patched = SimpleNamespace(
            route_id="r2",
            truck_id="t1",
            stops=[
                SimpleNamespace(
                    stop_id="S1",
                    planned_gallons=100.0,
                    eta="2025-01-01T08:45:00+00:00",
                    product_code="PROPANE",
                )
            ],
        )

        diff = compute_replan_diff(original, patched)

        assert len(diff.eta_shifts) == 1
        assert diff.eta_shifts[0].shift_minutes == pytest.approx(45.0)

    def test_raises_without_route_id(self) -> None:
        original = {"route_id": "", "truck_id": "t1", "stops": []}
        patched = {"route_id": "r2", "truck_id": "t1", "stops": []}
        with pytest.raises(ValueError):
            compute_replan_diff(original, patched)

    def test_skips_malformed_stops_without_identifier(self) -> None:
        original = {
            "route_id": "r1",
            "truck_id": "t1",
            "stops": [{"eta": "2025-01-01T08:00:00+00:00"}],
        }
        patched = {
            "route_id": "r2",
            "truck_id": "t1",
            "stops": [{"eta": "2025-01-01T08:00:00+00:00"}],
        }

        # Neither stop has an identifier — diff is empty, not a crash.
        diff = compute_replan_diff(original, patched)
        assert diff.added_stops == []
        assert diff.removed_stops == []

    def test_result_round_trips_through_json(self) -> None:
        original = _mvp_route(
            "r1",
            "t1",
            [
                _stop("S1", "2025-01-01T08:00:00+00:00", {"AGO": 100.0}, 0),
                _stop("S2", "2025-01-01T09:00:00+00:00", {"AGO": 50.0}, 1),
            ],
        )
        patched = _mvp_route(
            "r2",
            "t2",
            [
                _stop("S2", "2025-01-01T09:00:00+00:00", {"AGO": 80.0}, 0),
                _stop("S3", "2025-01-01T10:00:00+00:00", {"PMS": 200.0}, 1),
            ],
        )

        diff = compute_replan_diff(
            original,
            patched,
            generated_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            diff_id="diff_compute_round_trip",
        )

        restored = ReplanDiff.model_validate_json(diff.model_dump_json())
        assert restored == diff
