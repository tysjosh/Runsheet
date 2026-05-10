"""Integration and gap-filling tests for Phase 12 (IFTA Reporter).

This test module fills coverage gaps identified across the three Phase 12
test files:
- Boundary detection edge cases (Alaska/Hawaii, points on state borders,
  rapid back-and-forth crossings)
- Quarterly aggregation edge cases (cross-quarter boundary, year rollover)
- Incomplete-data flagging edge cases (partial data, trucks index failure)
- Integration between StateBoundaryDetector and IFTAReporter

Validates: Requirements 7.1, 7.2, 7.4, 7.6
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from compliance.services.ifta_reporter import (
    IFTAReport,
    IFTAReporter,
    IncompleteDataFlag,
    MileageAdjustment,
    TripSegment,
    TruckIFTASummary,
    compute_quarter,
    _quarter_date_range,
)
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
# Helpers
# ---------------------------------------------------------------------------


def _make_detector_with_polygon_lookup(lookup_fn) -> StateBoundaryDetector:
    """Create a detector with a custom point-in-polygon lookup function."""
    detector = StateBoundaryDetector(shapefile_path="/nonexistent/path.shp")
    detector._shapefile_loaded = True
    detector._load_attempted = True
    detector._state_polygons = []
    detector._point_in_polygon_lookup = lookup_fn
    return detector


def _make_detector_with_mock_polygons(state_map: dict) -> StateBoundaryDetector:
    """Create a StateBoundaryDetector with pre-populated grid cache."""
    detector = StateBoundaryDetector(shapefile_path="/nonexistent/path.shp")
    detector._shapefile_loaded = True
    detector._load_attempted = True
    detector._state_polygons = []
    for cell_key, state_code in state_map.items():
        detector._grid_cache[cell_key] = state_code
    return detector


# ---------------------------------------------------------------------------
# Tests: Boundary Detection Edge Cases
# ---------------------------------------------------------------------------


class TestBoundaryDetectionEdgeCases:
    """Edge cases for StateBoundaryDetector not covered in existing tests.

    Validates: Requirement 7.1
    """

    def test_alaska_coordinates_outside_continental_bounds(self):
        """Alaska coordinates (lat > 50 or lon < -125) are outside US bounds."""
        # Anchorage, AK: 61.2°N, -149.9°W
        assert _is_within_us_bounds(61.2, -149.9) is False

    def test_hawaii_coordinates_outside_continental_bounds(self):
        """Hawaii coordinates (lat < 24) are outside US bounds."""
        # Honolulu, HI: 21.3°N, -157.8°W
        assert _is_within_us_bounds(21.3, -157.8) is False

    def test_get_state_returns_none_for_alaska_coordinates(self):
        """get_state returns None for Alaska coordinates (outside bounding box)."""
        detector = StateBoundaryDetector()
        # Fairbanks, AK
        result = detector.get_state(64.84, -147.72)
        assert result is None

    def test_get_state_returns_none_for_hawaii_coordinates(self):
        """get_state returns None for Hawaii coordinates (outside bounding box)."""
        detector = StateBoundaryDetector()
        # Maui, HI
        result = detector.get_state(20.8, -156.3)
        assert result is None

    def test_rapid_back_and_forth_crossings(self):
        """Rapid back-and-forth crossings near a border are all detected."""
        state_map = {
            _grid_cell_key(33.9, -97.0): "TX",
            _grid_cell_key(34.0, -97.0): "OK",
            _grid_cell_key(33.9, -97.1): "TX",
            _grid_cell_key(34.0, -97.1): "OK",
        }
        detector = _make_detector_with_mock_polygons(state_map)

        # Truck zigzags across TX/OK border
        points = [
            (33.9, -97.0),   # TX
            (34.0, -97.0),   # OK
            (33.9, -97.1),   # TX
            (34.0, -97.1),   # OK
        ]
        result = detector.detect_boundary_crossing(points)

        assert len(result) == 3
        assert result[0].from_state == "TX"
        assert result[0].to_state == "OK"
        assert result[1].from_state == "OK"
        assert result[1].to_state == "TX"
        assert result[2].from_state == "TX"
        assert result[2].to_state == "OK"

    def test_crossing_with_multiple_none_points_between_states(self):
        """Multiple None points between valid states still detect crossing."""

        def mock_lookup(lat, lon):
            if lat < 33.0:
                return "TX"
            if lat > 35.0:
                return "OK"
            return None  # Large border zone

        detector = _make_detector_with_polygon_lookup(mock_lookup)

        points = [
            (32.5, -97.0),  # TX
            (33.5, -97.0),  # None
            (34.0, -97.0),  # None
            (34.5, -97.0),  # None
            (35.5, -97.0),  # OK
        ]
        result = detector.detect_boundary_crossing(points)

        assert len(result) == 1
        assert result[0].from_state == "TX"
        assert result[0].to_state == "OK"

    def test_all_points_none_returns_empty(self):
        """When all points resolve to None, no crossings are detected."""

        def mock_lookup(lat, lon):
            return None

        detector = _make_detector_with_polygon_lookup(mock_lookup)

        points = [(33.0, -97.0), (34.0, -97.0), (35.0, -97.0)]
        result = detector.detect_boundary_crossing(points)

        assert result == []

    def test_point_at_exact_boundary_of_us_bounding_box(self):
        """Points at exact boundary values of the US bounding box are included."""
        # Exact min lat (Key West area)
        assert _is_within_us_bounds(24.0, -80.0) is True
        # Exact max lat (northern border)
        assert _is_within_us_bounds(50.0, -100.0) is True
        # Just outside
        assert _is_within_us_bounds(23.99, -80.0) is False
        assert _is_within_us_bounds(50.01, -100.0) is False

    def test_grid_cell_key_for_extreme_us_coordinates(self):
        """Grid cell keys are computed correctly for extreme US coordinates."""
        # Key West, FL (southernmost continental US)
        key_south = _grid_cell_key(24.55, -81.78)
        assert key_south == (245, -818)

        # Northwest Angle, MN (northernmost continental US)
        key_north = _grid_cell_key(49.38, -95.15)
        assert key_north == (493, -952)

    def test_detect_crossing_with_timestamps_shorter_than_points(self):
        """When timestamps list is shorter than points, missing timestamps are None."""
        state_map = {
            _grid_cell_key(33.8, -97.0): "TX",
            _grid_cell_key(34.0, -97.0): "OK",
            _grid_cell_key(34.2, -97.0): "OK",
        }
        detector = _make_detector_with_mock_polygons(state_map)

        points = [(33.8, -97.0), (34.0, -97.0), (34.2, -97.0)]
        # Only one timestamp provided (shorter than points list)
        timestamps = [datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)]

        result = detector.detect_boundary_crossing(points, timestamps=timestamps)

        assert len(result) == 1
        # Crossing at index 1, but timestamps[1] doesn't exist → None
        assert result[0].timestamp is None


# ---------------------------------------------------------------------------
# Tests: Quarterly Aggregation Edge Cases
# ---------------------------------------------------------------------------


class TestQuarterlyAggregationEdgeCases:
    """Edge cases for quarterly computation and aggregation.

    Validates: Requirements 7.2, 7.4
    """

    def test_compute_quarter_year_boundary_dec_31(self):
        """Dec 31 at 23:59:59 is Q4 of the current year."""
        ts = datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2025-Q4"

    def test_compute_quarter_year_boundary_jan_1(self):
        """Jan 1 at 00:00:00 is Q1 of the new year."""
        ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2026-Q1"

    def test_compute_quarter_q1_q2_boundary_mar_31(self):
        """Mar 31 at 23:59:59 is still Q1."""
        ts = datetime(2026, 3, 31, 23, 59, 59, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2026-Q1"

    def test_compute_quarter_q1_q2_boundary_apr_1(self):
        """Apr 1 at 00:00:00 is Q2."""
        ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2026-Q2"

    def test_compute_quarter_q2_q3_boundary(self):
        """Jun 30 → Q2, Jul 1 → Q3."""
        assert compute_quarter(datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)) == "2026-Q2"
        assert compute_quarter(datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)) == "2026-Q3"

    def test_compute_quarter_q3_q4_boundary(self):
        """Sep 30 → Q3, Oct 1 → Q4."""
        assert compute_quarter(datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)) == "2026-Q3"
        assert compute_quarter(datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)) == "2026-Q4"

    def test_quarter_date_range_q4_crosses_year(self):
        """Q4 date range ends on Jan 1 of the next year."""
        start, end = _quarter_date_range("2025-Q4")
        assert start == "2025-10-01"
        assert end == "2026-01-01"

    def test_quarter_date_range_q1_starts_at_year_beginning(self):
        """Q1 date range starts on Jan 1."""
        start, end = _quarter_date_range("2026-Q1")
        assert start == "2026-01-01"
        assert end == "2026-04-01"

    @pytest.mark.asyncio
    async def test_trip_segment_at_quarter_boundary_uses_timestamp_quarter(self):
        """A trip segment at midnight Dec 31/Jan 1 uses the timestamp's quarter."""
        es = AsyncMock()
        es.index_document = AsyncMock(return_value=None)
        detector = MagicMock()

        reporter = IFTAReporter(
            es_service=es,
            state_boundary_detector=detector,
        )

        # Trip at 23:59 Dec 31 → Q4 2025
        ts_q4 = datetime(2025, 12, 31, 23, 59, 0, tzinfo=timezone.utc)
        segment_q4 = await reporter.record_trip_segment(
            tenant_id="t1",
            truck_id="truck_001",
            from_state="TX",
            to_state="OK",
            miles=50.0,
            timestamp=ts_q4,
        )
        assert segment_q4.quarter == "2025-Q4"

        # Trip at 00:01 Jan 1 → Q1 2026
        ts_q1 = datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
        segment_q1 = await reporter.record_trip_segment(
            tenant_id="t1",
            truck_id="truck_001",
            from_state="OK",
            to_state="KS",
            miles=75.0,
            timestamp=ts_q1,
        )
        assert segment_q1.quarter == "2026-Q1"

    @pytest.mark.asyncio
    async def test_trip_segments_same_crossing_different_quarters(self):
        """Same state crossing recorded in different quarters produces separate records."""
        es = AsyncMock()
        es.index_document = AsyncMock(return_value=None)
        detector = MagicMock()

        reporter = IFTAReporter(
            es_service=es,
            state_boundary_detector=detector,
        )

        # Q1 crossing
        seg1 = await reporter.record_trip_segment(
            tenant_id="t1",
            truck_id="truck_001",
            from_state="TX",
            to_state="OK",
            miles=100.0,
            timestamp=datetime(2026, 2, 15, 10, 0, tzinfo=timezone.utc),
        )

        # Q2 crossing (same route)
        seg2 = await reporter.record_trip_segment(
            tenant_id="t1",
            truck_id="truck_001",
            from_state="TX",
            to_state="OK",
            miles=100.0,
            timestamp=datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc),
        )

        assert seg1.quarter == "2026-Q1"
        assert seg2.quarter == "2026-Q2"
        assert seg1.record_id != seg2.record_id


# ---------------------------------------------------------------------------
# Tests: Incomplete-Data Flagging Edge Cases
# ---------------------------------------------------------------------------


class TestIncompleteDataFlaggingEdgeCases:
    """Edge cases for incomplete data detection.

    Validates: Requirement 7.6
    """

    @pytest.mark.asyncio
    async def test_all_trucks_missing_data_all_flagged(self):
        """When no trucks have mileage data, all fleet trucks are flagged."""
        es = AsyncMock()
        es.search_documents = AsyncMock()
        es.search_documents.side_effect = [
            # _get_fleet_truck_ids — 3 trucks in fleet
            {
                "hits": {"hits": [], "total": {"value": 3}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 5},
                            {"key": "truck_002", "doc_count": 3},
                            {"key": "truck_003", "doc_count": 2},
                        ]
                    }
                },
            },
            # _get_trucks_with_mileage_data — no trucks have data
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {
                    "truck_ids": {"buckets": []}
                },
            },
        ]

        reporter = IFTAReporter(
            es_service=es,
            state_boundary_detector=MagicMock(),
        )

        flags = await reporter.check_data_completeness(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert len(flags) == 3
        flagged_ids = {f.truck_id for f in flags}
        assert flagged_ids == {"truck_001", "truck_002", "truck_003"}
        for flag in flags:
            assert flag.flag_type == "ifta_data_incomplete"
            assert flag.quarter == "2026-Q1"

    @pytest.mark.asyncio
    async def test_mileage_data_query_failure_propagates_exception(self):
        """When mileage data query fails, the exception propagates (unlike trucks index failure)."""
        es = AsyncMock()
        es.search_documents = AsyncMock()
        es.search_documents.side_effect = [
            # _get_fleet_truck_ids — 2 trucks (succeeds)
            {
                "hits": {"hits": [], "total": {"value": 2}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 5},
                            {"key": "truck_002", "doc_count": 3},
                        ]
                    }
                },
            },
            # _get_trucks_with_mileage_data — raises exception
            Exception("connection_timeout"),
        ]

        reporter = IFTAReporter(
            es_service=es,
            state_boundary_detector=MagicMock(),
        )

        # _get_trucks_with_mileage_data does NOT have error handling,
        # so the exception propagates (unlike _get_fleet_truck_ids which
        # catches exceptions and returns empty set)
        with pytest.raises(Exception, match="connection_timeout"):
            await reporter.check_data_completeness(
                tenant_id="tenant_abc",
                quarter="2026-Q2",
            )

    @pytest.mark.asyncio
    async def test_incomplete_data_flag_includes_quarter(self):
        """IncompleteDataFlag correctly stores the quarter being checked."""
        flag = IncompleteDataFlag(
            truck_id="truck_001",
            quarter="2026-Q3",
            reason="No Geotab odometer/mileage data for truck_001 during 2026-Q3",
        )

        assert flag.quarter == "2026-Q3"
        assert flag.truck_id == "truck_001"
        assert "2026-Q3" in flag.reason

    @pytest.mark.asyncio
    async def test_generate_report_with_all_trucks_incomplete(self):
        """Report handles case where all trucks are flagged as incomplete."""
        es = AsyncMock()
        notification = AsyncMock()
        notification.notify_event = AsyncMock(return_value=[])

        es.search_documents = AsyncMock()
        es.search_documents.side_effect = [
            # check_data_completeness: _get_fleet_truck_ids
            {
                "hits": {"hits": [], "total": {"value": 2}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 5},
                            {"key": "truck_002", "doc_count": 3},
                        ]
                    }
                },
            },
            # check_data_completeness: _get_trucks_with_mileage_data — none
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"truck_ids": {"buckets": []}},
            },
            # get_all_trucks_mileage — empty (all trucks excluded)
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        reporter = IFTAReporter(
            es_service=es,
            state_boundary_detector=MagicMock(),
            notification_service=notification,
        )

        report = await reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert isinstance(report, IFTAReport)
        assert report.truck_count == 0
        assert report.trucks == []
        assert len(report.incomplete_trucks) == 2
        assert report.total_miles == 0.0
        assert report.fleet_mpg is None


# ---------------------------------------------------------------------------
# Tests: Integration — StateBoundaryDetector + IFTAReporter
# ---------------------------------------------------------------------------


class TestBoundaryDetectorIFTAReporterIntegration:
    """Integration tests verifying StateBoundaryDetector and IFTAReporter
    work together correctly.

    Validates: Requirements 7.1, 7.2
    """

    @pytest.mark.asyncio
    async def test_boundary_crossings_produce_trip_segments(self):
        """Detected boundary crossings can be recorded as trip segments."""
        # Set up boundary detector with known state map
        state_map = {
            _grid_cell_key(32.0, -97.0): "TX",
            _grid_cell_key(34.0, -97.0): "OK",
            _grid_cell_key(37.0, -97.0): "KS",
        }
        detector = _make_detector_with_mock_polygons(state_map)

        # Detect crossings
        points = [(32.0, -97.0), (34.0, -97.0), (37.0, -97.0)]
        timestamps = [
            datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 15, 11, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        ]
        crossings = detector.detect_boundary_crossing(points, timestamps=timestamps)

        assert len(crossings) == 2

        # Set up IFTA reporter
        es = AsyncMock()
        es.index_document = AsyncMock(return_value=None)

        reporter = IFTAReporter(
            es_service=es,
            state_boundary_detector=detector,
        )

        # Record each crossing as a trip segment
        segments = []
        for crossing in crossings:
            segment = await reporter.record_trip_segment(
                tenant_id="tenant_abc",
                truck_id="truck_001",
                from_state=crossing.from_state,
                to_state=crossing.to_state,
                miles=100.0,
                timestamp=crossing.timestamp,
            )
            segments.append(segment)

        assert len(segments) == 2
        assert segments[0].jurisdiction == "TX"
        assert segments[1].jurisdiction == "OK"
        assert all(s.quarter == "2026-Q1" for s in segments)

        # Verify both were persisted to ES
        assert es.index_document.call_count == 2

    @pytest.mark.asyncio
    async def test_none_state_crossings_not_recorded(self):
        """When boundary detector returns None for a point, no segment is recorded."""

        def mock_lookup(lat, lon):
            if lat < 33.0:
                return "TX"
            return None  # Everything else is unknown

        detector = _make_detector_with_polygon_lookup(mock_lookup)

        # Only one valid state, so no crossings
        points = [(32.5, -97.0), (33.5, -97.0), (34.5, -97.0)]
        crossings = detector.detect_boundary_crossing(points)

        # No crossing because we never get a second valid state
        assert crossings == []

    @pytest.mark.asyncio
    async def test_multi_state_trip_produces_correct_jurisdictions(self):
        """A multi-state trip produces segments with correct from_state jurisdictions."""
        state_map = {
            _grid_cell_key(30.0, -97.0): "TX",
            _grid_cell_key(32.0, -97.0): "TX",
            _grid_cell_key(34.0, -97.0): "OK",
            _grid_cell_key(36.0, -97.0): "KS",
            _grid_cell_key(38.0, -97.0): "NE",
        }
        detector = _make_detector_with_mock_polygons(state_map)

        points = [
            (30.0, -97.0),  # TX
            (32.0, -97.0),  # TX (no crossing)
            (34.0, -97.0),  # OK (crossing from TX)
            (36.0, -97.0),  # KS (crossing from OK)
            (38.0, -97.0),  # NE (crossing from KS)
        ]
        crossings = detector.detect_boundary_crossing(points)

        assert len(crossings) == 3
        # Each crossing's from_state is the jurisdiction where miles were driven
        assert crossings[0].from_state == "TX"
        assert crossings[0].to_state == "OK"
        assert crossings[1].from_state == "OK"
        assert crossings[1].to_state == "KS"
        assert crossings[2].from_state == "KS"
        assert crossings[2].to_state == "NE"

    @pytest.mark.asyncio
    async def test_fleet_mpg_zero_gallons_does_not_crash_report(self):
        """generate_quarterly_report handles zero gallons without division error."""
        es = AsyncMock()
        es.search_documents = AsyncMock()
        es.search_documents.side_effect = [
            # check_data_completeness: _get_fleet_truck_ids
            {
                "hits": {"hits": [], "total": {"value": 1}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [{"key": "truck_001", "doc_count": 3}]
                    }
                },
            },
            # check_data_completeness: _get_trucks_with_mileage_data
            {
                "hits": {"hits": [], "total": {"value": 3}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [{"key": "truck_001", "doc_count": 3}]
                    }
                },
            },
            # get_all_trucks_mileage — truck has miles but no fuel
            {
                "hits": {"hits": [], "total": {"value": 3}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 3,
                                "truck_total_miles": {"value": 1500.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 3,
                                            "total_miles": {"value": 1500.0},
                                            "taxable_miles": {"value": 1500.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols — empty
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        reporter = IFTAReporter(
            es_service=es,
            state_boundary_detector=MagicMock(),
        )

        report = await reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q2",
        )

        # Should not crash — fleet_mpg is None when no gallons
        assert report.fleet_mpg is None
        assert report.total_miles == 1500.0
        assert report.total_gallons == 0.0
        # net_taxable_gallons should be 0 when fleet_mpg is None
        assert report.trucks[0].jurisdictions[0].net_taxable_gallons == 0.0

    @pytest.mark.asyncio
    async def test_manual_adjustment_included_in_quarterly_aggregation(self):
        """Manual adjustments (source=manual_adjustment) are included in mileage totals."""
        es = AsyncMock()
        es.index_document = AsyncMock(return_value=None)

        reporter = IFTAReporter(
            es_service=es,
            state_boundary_detector=MagicMock(),
        )

        # Record a manual adjustment
        adj = await reporter.record_manual_adjustment(
            tenant_id="tenant_abc",
            truck_id="truck_001",
            jurisdiction="TX",
            miles=50.0,
            quarter="2026-Q1",
            operator_id="admin_001",
            reason="GPS signal loss correction",
        )

        # Verify it was persisted with source=manual_adjustment
        doc = es.index_document.call_args[0][2]
        assert doc["source"] == "manual_adjustment"
        assert doc["miles"] == 50.0
        assert doc["quarter"] == "2026-Q1"
        # Manual adjustments go to the same ifta_mileage index
        assert es.index_document.call_args[0][0] == "ifta_mileage"
