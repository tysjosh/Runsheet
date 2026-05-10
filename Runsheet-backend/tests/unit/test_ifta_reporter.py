"""Unit tests for compliance/services/ifta_reporter.py.

Tests cover:
- Service initialization with required dependencies
- record_trip_segment persistence to the ifta_mileage ES index
- Quarter computation from timestamps
- get_mileage_by_jurisdiction aggregation query (per-truck)
- get_all_trucks_mileage nested aggregation (all trucks in fleet)

Validates: Requirement 7.1, 7.2
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from compliance.services.ifta_reporter import (
    IFTAReport,
    IFTAReporter,
    JurisdictionFuelGallons,
    JurisdictionIFTAEntry,
    JurisdictionMileage,
    MileageAdjustment,
    TripSegment,
    TruckFuelGallons,
    TruckIFTASummary,
    TruckJurisdictionMileage,
    compute_quarter,
)
from compliance.services.state_boundary_detector import StateBoundaryDetector


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_es_service():
    """Create a mock ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {},
    })
    return es


@pytest.fixture
def mock_boundary_detector():
    """Create a mock StateBoundaryDetector."""
    detector = MagicMock(spec=StateBoundaryDetector)
    detector.get_state = MagicMock(return_value="TX")
    return detector


@pytest.fixture
def mock_notification_service():
    """Create a mock notification service."""
    return AsyncMock()


@pytest.fixture
def ifta_reporter(mock_es_service, mock_boundary_detector, mock_notification_service):
    """Create an IFTAReporter instance with mocked dependencies."""
    return IFTAReporter(
        es_service=mock_es_service,
        state_boundary_detector=mock_boundary_detector,
        notification_service=mock_notification_service,
    )


@pytest.fixture
def ifta_reporter_no_notifications(mock_es_service, mock_boundary_detector):
    """Create an IFTAReporter without notification service."""
    return IFTAReporter(
        es_service=mock_es_service,
        state_boundary_detector=mock_boundary_detector,
    )


# ---------------------------------------------------------------------------
# Tests: Initialization
# ---------------------------------------------------------------------------


class TestIFTAReporterInit:
    """Tests for IFTAReporter initialization."""

    def test_init_with_all_dependencies(
        self, mock_es_service, mock_boundary_detector, mock_notification_service
    ):
        """Service initializes with es_service, boundary detector, and notification service."""
        reporter = IFTAReporter(
            es_service=mock_es_service,
            state_boundary_detector=mock_boundary_detector,
            notification_service=mock_notification_service,
        )
        assert reporter._es is mock_es_service
        assert reporter._boundary_detector is mock_boundary_detector
        assert reporter._notification_service is mock_notification_service

    def test_init_without_notification_service(
        self, mock_es_service, mock_boundary_detector
    ):
        """Service initializes with notification_service as None (optional)."""
        reporter = IFTAReporter(
            es_service=mock_es_service,
            state_boundary_detector=mock_boundary_detector,
        )
        assert reporter._es is mock_es_service
        assert reporter._boundary_detector is mock_boundary_detector
        assert reporter._notification_service is None


# ---------------------------------------------------------------------------
# Tests: compute_quarter helper
# ---------------------------------------------------------------------------


class TestComputeQuarter:
    """Tests for the compute_quarter helper function."""

    def test_q1_january(self):
        ts = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2026-Q1"

    def test_q1_march(self):
        ts = datetime(2026, 3, 31, 23, 59, 59, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2026-Q1"

    def test_q2_april(self):
        ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2026-Q2"

    def test_q2_june(self):
        ts = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2026-Q2"

    def test_q3_july(self):
        ts = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2026-Q3"

    def test_q3_september(self):
        ts = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2026-Q3"

    def test_q4_october(self):
        ts = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2026-Q4"

    def test_q4_december(self):
        ts = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2026-Q4"

    def test_different_year(self):
        ts = datetime(2025, 5, 15, 8, 30, 0, tzinfo=timezone.utc)
        assert compute_quarter(ts) == "2025-Q2"


# ---------------------------------------------------------------------------
# Tests: record_trip_segment
# ---------------------------------------------------------------------------


class TestRecordTripSegment:
    """Tests for IFTAReporter.record_trip_segment."""

    @pytest.mark.asyncio
    async def test_records_segment_to_es(self, ifta_reporter, mock_es_service):
        """record_trip_segment persists a document to the ifta_mileage index."""
        timestamp = datetime(2026, 2, 15, 14, 30, 0, tzinfo=timezone.utc)

        segment = await ifta_reporter.record_trip_segment(
            tenant_id="tenant_abc",
            truck_id="truck_001",
            from_state="TX",
            to_state="OK",
            miles=125.5,
            timestamp=timestamp,
        )

        # Verify ES index_document was called
        mock_es_service.index_document.assert_called_once()
        call_args = mock_es_service.index_document.call_args

        # Check index name
        assert call_args[0][0] == "ifta_mileage"

        # Check document ID matches the segment record_id
        assert call_args[0][1] == segment.record_id

        # Check document content
        doc = call_args[0][2]
        assert doc["tenant_id"] == "tenant_abc"
        assert doc["truck_id"] == "truck_001"
        assert doc["jurisdiction"] == "TX"
        assert doc["miles"] == 125.5
        assert doc["quarter"] == "2026-Q1"
        assert doc["source"] == "geotab"

    @pytest.mark.asyncio
    async def test_returns_trip_segment_model(self, ifta_reporter):
        """record_trip_segment returns a TripSegment with correct fields."""
        timestamp = datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc)

        segment = await ifta_reporter.record_trip_segment(
            tenant_id="tenant_xyz",
            truck_id="truck_002",
            from_state="CA",
            to_state="NV",
            miles=87.3,
            timestamp=timestamp,
        )

        assert isinstance(segment, TripSegment)
        assert segment.tenant_id == "tenant_xyz"
        assert segment.truck_id == "truck_002"
        assert segment.jurisdiction == "CA"
        assert segment.miles == 87.3
        assert segment.quarter == "2026-Q2"
        assert segment.timestamp == timestamp
        assert segment.source == "geotab"
        assert segment.record_id.startswith("ifta_")

    @pytest.mark.asyncio
    async def test_quarter_computed_from_timestamp(self, ifta_reporter):
        """Quarter is automatically computed from the provided timestamp."""
        # Q3 timestamp
        timestamp = datetime(2026, 8, 20, 16, 45, 0, tzinfo=timezone.utc)

        segment = await ifta_reporter.record_trip_segment(
            tenant_id="tenant_abc",
            truck_id="truck_003",
            from_state="IL",
            to_state="IN",
            miles=50.0,
            timestamp=timestamp,
        )

        assert segment.quarter == "2026-Q3"

    @pytest.mark.asyncio
    async def test_from_state_uppercased(self, ifta_reporter):
        """from_state is stored as uppercase regardless of input case."""
        timestamp = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)

        segment = await ifta_reporter.record_trip_segment(
            tenant_id="tenant_abc",
            truck_id="truck_004",
            from_state="tx",
            to_state="ok",
            miles=100.0,
            timestamp=timestamp,
        )

        assert segment.jurisdiction == "TX"

    @pytest.mark.asyncio
    async def test_custom_source(self, ifta_reporter, mock_es_service):
        """record_trip_segment accepts a custom source parameter."""
        timestamp = datetime(2026, 3, 1, 8, 0, 0, tzinfo=timezone.utc)

        segment = await ifta_reporter.record_trip_segment(
            tenant_id="tenant_abc",
            truck_id="truck_005",
            from_state="FL",
            to_state="GA",
            miles=200.0,
            timestamp=timestamp,
            source="manual_adjustment",
        )

        assert segment.source == "manual_adjustment"

        # Verify source is persisted to ES
        doc = mock_es_service.index_document.call_args[0][2]
        assert doc["source"] == "manual_adjustment"

    @pytest.mark.asyncio
    async def test_document_includes_created_at(self, ifta_reporter, mock_es_service):
        """Persisted document includes created_at timestamp."""
        timestamp = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)

        await ifta_reporter.record_trip_segment(
            tenant_id="tenant_abc",
            truck_id="truck_006",
            from_state="PA",
            to_state="NJ",
            miles=30.0,
            timestamp=timestamp,
        )

        doc = mock_es_service.index_document.call_args[0][2]
        assert "created_at" in doc
        assert "updated_at" in doc


# ---------------------------------------------------------------------------
# Tests: get_mileage_by_jurisdiction
# ---------------------------------------------------------------------------


class TestGetMileageByJurisdiction:
    """Tests for IFTAReporter.get_mileage_by_jurisdiction."""

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_data(
        self, ifta_reporter, mock_es_service
    ):
        """Returns empty list when no mileage data exists."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_jurisdiction": {"buckets": []},
            },
        }

        result = await ifta_reporter.get_mileage_by_jurisdiction(
            tenant_id="tenant_abc",
            truck_id="truck_001",
            quarter="2026-Q1",
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_jurisdiction_mileage_list(
        self, ifta_reporter, mock_es_service
    ):
        """Returns JurisdictionMileage records from ES aggregation."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 5}},
            "aggregations": {
                "by_jurisdiction": {
                    "buckets": [
                        {
                            "key": "TX",
                            "doc_count": 3,
                            "total_miles": {"value": 450.5},
                            "taxable_miles": {"value": 450.5},
                        },
                        {
                            "key": "OK",
                            "doc_count": 2,
                            "total_miles": {"value": 200.0},
                            "taxable_miles": {"value": 200.0},
                        },
                    ]
                }
            },
        }

        result = await ifta_reporter.get_mileage_by_jurisdiction(
            tenant_id="tenant_abc",
            truck_id="truck_001",
            quarter="2026-Q1",
        )

        assert len(result) == 2
        assert isinstance(result[0], JurisdictionMileage)
        assert result[0].jurisdiction == "TX"
        assert result[0].total_miles == 450.5
        assert result[0].segment_count == 3
        assert result[1].jurisdiction == "OK"
        assert result[1].total_miles == 200.0
        assert result[1].segment_count == 2

    @pytest.mark.asyncio
    async def test_query_includes_tenant_filter(
        self, ifta_reporter, mock_es_service
    ):
        """Query is scoped to the tenant via inject_tenant_filter."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_jurisdiction": {"buckets": []},
            },
        }

        await ifta_reporter.get_mileage_by_jurisdiction(
            tenant_id="tenant_abc",
            truck_id="truck_001",
            quarter="2026-Q1",
        )

        # Verify search was called
        mock_es_service.search_documents.assert_called_once()
        call_args = mock_es_service.search_documents.call_args

        # Check index name
        assert call_args[0][0] == "ifta_mileage"

        # The query should contain tenant_id filter (injected by inject_tenant_filter)
        query = call_args[0][1]
        # inject_tenant_filter adds a term filter for tenant_id
        query_str = str(query)
        assert "tenant_id" in query_str


# ---------------------------------------------------------------------------
# Tests: generate_quarterly_report
# ---------------------------------------------------------------------------


class TestGenerateQuarterlyReport:
    """Tests for IFTAReporter.generate_quarterly_report.

    Validates: Requirement 7.4 — per-truck IFTA summary showing
    jurisdiction, total_miles, taxable_miles, tax_paid_gallons,
    net_taxable_gallons, tax_rate, and tax_due for each state.
    """

    @pytest.mark.asyncio
    async def test_returns_ifta_report_with_empty_data(
        self, ifta_reporter, mock_es_service
    ):
        """generate_quarterly_report returns an IFTAReport with empty trucks when no data."""
        # get_all_trucks_mileage returns empty
        # get_fuel_gallons_by_jurisdiction returns empty
        mock_es_service.search_documents.side_effect = [
            # check_data_completeness: _get_fleet_truck_ids (no trucks)
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"truck_ids": {"buckets": []}},
            },
            # get_all_trucks_mileage response
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # get_fuel_gallons_by_jurisdiction: fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        report = await ifta_reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert isinstance(report, IFTAReport)
        assert report.tenant_id == "tenant_abc"
        assert report.quarter == "2026-Q1"
        assert report.trucks == []
        assert report.jurisdictions == []
        assert report.total_miles == 0.0
        assert report.total_gallons == 0.0
        assert report.truck_count == 0
        assert report.fleet_mpg is None

    @pytest.mark.asyncio
    async def test_report_with_single_truck_single_jurisdiction(
        self, ifta_reporter, mock_es_service
    ):
        """Report produces per-truck summary for a single truck in one state."""
        mock_es_service.search_documents.side_effect = [
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
            # get_all_trucks_mileage response
            {
                "hits": {"hits": [], "total": {"value": 3}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 3,
                                "truck_total_miles": {"value": 1000.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 3,
                                            "total_miles": {"value": 1000.0},
                                            "taxable_miles": {"value": 1000.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols
            {
                "hits": {"hits": [], "total": {"value": 2}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 2,
                                "truck_total_gallons": {"value": 200.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 200.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        report = await ifta_reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert report.truck_count == 1
        assert report.total_miles == 1000.0
        assert report.total_gallons == 200.0
        # fleet_mpg = 1000 / 200 = 5.0
        assert report.fleet_mpg == pytest.approx(5.0)

        # Per-truck summary
        assert len(report.trucks) == 1
        truck = report.trucks[0]
        assert isinstance(truck, TruckIFTASummary)
        assert truck.truck_id == "truck_001"
        assert truck.total_miles == 1000.0
        assert truck.total_gallons == 200.0

        # Per-jurisdiction entry
        assert len(truck.jurisdictions) == 1
        jur = truck.jurisdictions[0]
        assert isinstance(jur, JurisdictionIFTAEntry)
        assert jur.jurisdiction == "TX"
        assert jur.total_miles == 1000.0
        assert jur.taxable_miles == 1000.0
        assert jur.tax_paid_gallons == 200.0
        # net_taxable = (1000 / 5.0) - 200 = 200 - 200 = 0
        assert jur.net_taxable_gallons == pytest.approx(0.0)
        assert jur.tax_rate == 0.0
        assert jur.tax_due == 0.0

    @pytest.mark.asyncio
    async def test_report_with_multiple_trucks_multiple_jurisdictions(
        self, ifta_reporter, mock_es_service
    ):
        """Report produces per-truck summaries for multiple trucks across states."""
        mock_es_service.search_documents.side_effect = [
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
            # check_data_completeness: _get_trucks_with_mileage_data
            {
                "hits": {"hits": [], "total": {"value": 8}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 5},
                            {"key": "truck_002", "doc_count": 3},
                        ]
                    }
                },
            },
            # get_all_trucks_mileage response
            {
                "hits": {"hits": [], "total": {"value": 8}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 5,
                                "truck_total_miles": {"value": 800.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 3,
                                            "total_miles": {"value": 500.0},
                                            "taxable_miles": {"value": 500.0},
                                        },
                                        {
                                            "key": "OK",
                                            "doc_count": 2,
                                            "total_miles": {"value": 300.0},
                                            "taxable_miles": {"value": 300.0},
                                        },
                                    ]
                                },
                            },
                            {
                                "key": "truck_002",
                                "doc_count": 3,
                                "truck_total_miles": {"value": 400.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 2,
                                            "total_miles": {"value": 250.0},
                                            "taxable_miles": {"value": 250.0},
                                        },
                                        {
                                            "key": "NM",
                                            "doc_count": 1,
                                            "total_miles": {"value": 150.0},
                                            "taxable_miles": {"value": 150.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols
            {
                "hits": {"hits": [], "total": {"value": 4}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 2,
                                "truck_total_gallons": {"value": 150.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 150.0},
                                        },
                                    ]
                                },
                            },
                            {
                                "key": "truck_002",
                                "doc_count": 2,
                                "truck_total_gallons": {"value": 100.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "NM",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 100.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        report = await ifta_reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q2",
        )

        assert report.truck_count == 2
        assert report.total_miles == 1200.0  # 800 + 400
        assert report.total_gallons == 250.0  # 150 + 100
        # fleet_mpg = 1200 / 250 = 4.8
        assert report.fleet_mpg == pytest.approx(4.8)

        # Truck 1
        truck1 = report.trucks[0]
        assert truck1.truck_id == "truck_001"
        assert truck1.total_miles == 800.0
        assert truck1.total_gallons == 150.0
        assert len(truck1.jurisdictions) == 2

        # Truck 1 - TX jurisdiction
        tx_entry = next(j for j in truck1.jurisdictions if j.jurisdiction == "TX")
        assert tx_entry.total_miles == 500.0
        assert tx_entry.taxable_miles == 500.0
        assert tx_entry.tax_paid_gallons == 150.0
        # net_taxable = (500 / 4.8) - 150 = 104.167 - 150 = -45.833
        expected_net = (500.0 / 4.8) - 150.0
        assert tx_entry.net_taxable_gallons == pytest.approx(expected_net, rel=1e-3)

        # Truck 1 - OK jurisdiction (no fuel purchased there)
        ok_entry = next(j for j in truck1.jurisdictions if j.jurisdiction == "OK")
        assert ok_entry.total_miles == 300.0
        assert ok_entry.tax_paid_gallons == 0.0
        # net_taxable = (300 / 4.8) - 0 = 62.5
        expected_net_ok = 300.0 / 4.8
        assert ok_entry.net_taxable_gallons == pytest.approx(expected_net_ok, rel=1e-3)

        # Truck 2
        truck2 = report.trucks[1]
        assert truck2.truck_id == "truck_002"
        assert truck2.total_miles == 400.0
        assert truck2.total_gallons == 100.0

    @pytest.mark.asyncio
    async def test_report_fleet_jurisdictions_aggregated(
        self, ifta_reporter, mock_es_service
    ):
        """Report includes fleet-level jurisdiction totals aggregated across trucks."""
        mock_es_service.search_documents.side_effect = [
            # check_data_completeness: _get_fleet_truck_ids
            {
                "hits": {"hits": [], "total": {"value": 2}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 2},
                            {"key": "truck_002", "doc_count": 2},
                        ]
                    }
                },
            },
            # check_data_completeness: _get_trucks_with_mileage_data
            {
                "hits": {"hits": [], "total": {"value": 4}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 2},
                            {"key": "truck_002", "doc_count": 2},
                        ]
                    }
                },
            },
            # get_all_trucks_mileage response
            {
                "hits": {"hits": [], "total": {"value": 4}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 2,
                                "truck_total_miles": {"value": 600.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 2,
                                            "total_miles": {"value": 600.0},
                                            "taxable_miles": {"value": 600.0},
                                        },
                                    ]
                                },
                            },
                            {
                                "key": "truck_002",
                                "doc_count": 2,
                                "truck_total_miles": {"value": 400.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 2,
                                            "total_miles": {"value": 400.0},
                                            "taxable_miles": {"value": 400.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        report = await ifta_reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        # Fleet-level jurisdictions should aggregate TX from both trucks
        assert len(report.jurisdictions) == 1
        assert report.jurisdictions[0].jurisdiction == "TX"
        assert report.jurisdictions[0].total_miles == 1000.0  # 600 + 400
        assert report.jurisdictions[0].taxable_miles == 1000.0

    @pytest.mark.asyncio
    async def test_report_no_fuel_data_fleet_mpg_is_none(
        self, ifta_reporter, mock_es_service
    ):
        """Fleet MPG is None when no fuel gallons data exists."""
        mock_es_service.search_documents.side_effect = [
            # check_data_completeness: _get_fleet_truck_ids
            {
                "hits": {"hits": [], "total": {"value": 1}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [{"key": "truck_001", "doc_count": 2}]
                    }
                },
            },
            # check_data_completeness: _get_trucks_with_mileage_data
            {
                "hits": {"hits": [], "total": {"value": 2}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [{"key": "truck_001", "doc_count": 2}]
                    }
                },
            },
            # get_all_trucks_mileage response
            {
                "hits": {"hits": [], "total": {"value": 2}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 2,
                                "truck_total_miles": {"value": 500.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "FL",
                                            "doc_count": 2,
                                            "total_miles": {"value": 500.0},
                                            "taxable_miles": {"value": 500.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols (empty)
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        report = await ifta_reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q3",
        )

        assert report.fleet_mpg is None
        assert report.total_gallons == 0.0
        # net_taxable_gallons should be 0 when fleet_mpg is None
        assert report.trucks[0].jurisdictions[0].net_taxable_gallons == 0.0

    @pytest.mark.asyncio
    async def test_report_net_taxable_gallons_computation(
        self, ifta_reporter, mock_es_service
    ):
        """Net taxable gallons = (taxable_miles / fleet_mpg) - tax_paid_gallons."""
        mock_es_service.search_documents.side_effect = [
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
            # get_all_trucks_mileage: truck with 600 miles in TX
            {
                "hits": {"hits": [], "total": {"value": 3}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 3,
                                "truck_total_miles": {"value": 600.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 3,
                                            "total_miles": {"value": 600.0},
                                            "taxable_miles": {"value": 600.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: 100 gallons in TX
            {
                "hits": {"hits": [], "total": {"value": 1}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 1,
                                "truck_total_gallons": {"value": 100.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 1,
                                            "total_gallons": {"value": 100.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        report = await ifta_reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        # fleet_mpg = 600 / 100 = 6.0
        assert report.fleet_mpg == pytest.approx(6.0)

        jur = report.trucks[0].jurisdictions[0]
        # net_taxable = (600 / 6.0) - 100 = 100 - 100 = 0
        assert jur.net_taxable_gallons == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_report_tax_rate_and_tax_due_are_zero(
        self, ifta_reporter, mock_es_service
    ):
        """Tax rate and tax due are set to 0.0 (placeholder until rate table)."""
        mock_es_service.search_documents.side_effect = [
            # check_data_completeness: _get_fleet_truck_ids
            {
                "hits": {"hits": [], "total": {"value": 1}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [{"key": "truck_001", "doc_count": 1}]
                    }
                },
            },
            # check_data_completeness: _get_trucks_with_mileage_data
            {
                "hits": {"hits": [], "total": {"value": 1}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [{"key": "truck_001", "doc_count": 1}]
                    }
                },
            },
            # get_all_trucks_mileage
            {
                "hits": {"hits": [], "total": {"value": 1}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 1,
                                "truck_total_miles": {"value": 300.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "CA",
                                            "doc_count": 1,
                                            "total_miles": {"value": 300.0},
                                            "taxable_miles": {"value": 300.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # terminal_bols
            {
                "hits": {"hits": [], "total": {"value": 1}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 1,
                                "truck_total_gallons": {"value": 50.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "CA",
                                            "doc_count": 1,
                                            "total_gallons": {"value": 50.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        report = await ifta_reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q4",
        )

        jur = report.trucks[0].jurisdictions[0]
        assert jur.tax_rate == 0.0
        assert jur.tax_due == 0.0


# ---------------------------------------------------------------------------
# Tests: compute_fleet_mpg
# ---------------------------------------------------------------------------


class TestComputeFleetMpg:
    """Tests for IFTAReporter.compute_fleet_mpg.

    Validates: Requirement 7.5 — THE IFTA_Reporter SHALL compute the IFTA
    fleet average MPG as total_miles / total_gallons across all qualified
    vehicles for the quarter.
    """

    @pytest.mark.asyncio
    async def test_normal_computation(self, ifta_reporter, mock_es_service):
        """compute_fleet_mpg returns total_miles / total_gallons for normal data."""
        mock_es_service.search_documents.side_effect = [
            # _get_total_miles response (ifta_mileage aggregation)
            {
                "hits": {"hits": [], "total": {"value": 10}},
                "aggregations": {
                    "total_miles": {"value": 5000.0},
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols
            {
                "hits": {"hits": [], "total": {"value": 5}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 3,
                                "truck_total_gallons": {"value": 600.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 3,
                                            "total_gallons": {"value": 600.0},
                                        },
                                    ]
                                },
                            },
                            {
                                "key": "truck_002",
                                "doc_count": 2,
                                "truck_total_gallons": {"value": 400.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "OK",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 400.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        result = await ifta_reporter.compute_fleet_mpg(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        # fleet_mpg = 5000 / (600 + 400) = 5000 / 1000 = 5.0
        assert result == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_zero_gallons_returns_zero(self, ifta_reporter, mock_es_service):
        """compute_fleet_mpg returns 0.0 when total_gallons is zero (avoids division by zero)."""
        mock_es_service.search_documents.side_effect = [
            # _get_total_miles response — has miles but no fuel
            {
                "hits": {"hits": [], "total": {"value": 5}},
                "aggregations": {
                    "total_miles": {"value": 3000.0},
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols (empty)
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # get_fuel_gallons_by_jurisdiction: fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        result = await ifta_reporter.compute_fleet_mpg(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert result == 0.0

    @pytest.mark.asyncio
    async def test_correct_aggregation_from_multiple_sources(
        self, ifta_reporter, mock_es_service
    ):
        """compute_fleet_mpg correctly aggregates miles from ifta_mileage and gallons from both BOLs and fuel cards."""
        mock_es_service.search_documents.side_effect = [
            # _get_total_miles response
            {
                "hits": {"hits": [], "total": {"value": 20}},
                "aggregations": {
                    "total_miles": {"value": 12000.0},
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols
            {
                "hits": {"hits": [], "total": {"value": 4}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 2,
                                "truck_total_gallons": {"value": 800.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 800.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: fuel_card_transactions
            {
                "hits": {"hits": [], "total": {"value": 3}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 1,
                                "truck_total_gallons": {"value": 200.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "OK",
                                            "doc_count": 1,
                                            "total_gallons": {"value": 200.0},
                                        },
                                    ]
                                },
                            },
                            {
                                "key": "truck_002",
                                "doc_count": 2,
                                "truck_total_gallons": {"value": 500.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "NM",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 500.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
        ]

        result = await ifta_reporter.compute_fleet_mpg(
            tenant_id="tenant_abc",
            quarter="2026-Q2",
        )

        # total_gallons = 800 (BOL truck_001) + 200 (fuel_card truck_001) + 500 (fuel_card truck_002) = 1500
        # fleet_mpg = 12000 / 1500 = 8.0
        assert result == pytest.approx(8.0)

    @pytest.mark.asyncio
    async def test_no_mileage_data_returns_zero(
        self, ifta_reporter, mock_es_service
    ):
        """compute_fleet_mpg returns 0.0 when no mileage data exists (0 miles / any gallons = 0)."""
        mock_es_service.search_documents.side_effect = [
            # _get_total_miles response — no miles
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {
                    "total_miles": {"value": 0.0},
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols with some fuel
            {
                "hits": {"hits": [], "total": {"value": 2}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 2,
                                "truck_total_gallons": {"value": 500.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 500.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        result = await ifta_reporter.compute_fleet_mpg(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        # 0 miles / 500 gallons = 0.0
        assert result == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_no_data_at_all_returns_zero(
        self, ifta_reporter, mock_es_service
    ):
        """compute_fleet_mpg returns 0.0 when neither miles nor gallons data exists."""
        mock_es_service.search_documents.side_effect = [
            # _get_total_miles response — no miles
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {
                    "total_miles": {"value": 0.0},
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols (empty)
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        result = await ifta_reporter.compute_fleet_mpg(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert result == 0.0


# ---------------------------------------------------------------------------
# Tests: TripSegment model
# ---------------------------------------------------------------------------


class TestTripSegmentModel:
    """Tests for the TripSegment Pydantic model."""

    def test_creates_with_required_fields(self):
        """TripSegment can be created with all required fields."""
        ts = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        segment = TripSegment(
            tenant_id="tenant_abc",
            truck_id="truck_001",
            jurisdiction="TX",
            miles=100.0,
            quarter="2026-Q1",
            timestamp=ts,
        )
        assert segment.tenant_id == "tenant_abc"
        assert segment.truck_id == "truck_001"
        assert segment.jurisdiction == "TX"
        assert segment.miles == 100.0
        assert segment.quarter == "2026-Q1"
        assert segment.record_id.startswith("ifta_")

    def test_auto_generates_record_id(self):
        """TripSegment auto-generates a unique record_id."""
        ts = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        s1 = TripSegment(
            tenant_id="t", truck_id="tr", jurisdiction="TX",
            miles=10.0, quarter="2026-Q1", timestamp=ts,
        )
        s2 = TripSegment(
            tenant_id="t", truck_id="tr", jurisdiction="TX",
            miles=10.0, quarter="2026-Q1", timestamp=ts,
        )
        assert s1.record_id != s2.record_id

    def test_rejects_negative_miles(self):
        """TripSegment rejects negative miles."""
        ts = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(Exception):
            TripSegment(
                tenant_id="t", truck_id="tr", jurisdiction="TX",
                miles=-5.0, quarter="2026-Q1", timestamp=ts,
            )

    def test_default_source_is_geotab(self):
        """TripSegment defaults source to 'geotab'."""
        ts = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        segment = TripSegment(
            tenant_id="t", truck_id="tr", jurisdiction="TX",
            miles=10.0, quarter="2026-Q1", timestamp=ts,
        )
        assert segment.source == "geotab"


# ---------------------------------------------------------------------------
# Tests: get_all_trucks_mileage (Task 12.4, Req 7.2)
# ---------------------------------------------------------------------------


class TestGetAllTrucksMileage:
    """Tests for IFTAReporter.get_all_trucks_mileage.

    Validates: Requirement 7.2 — aggregate total miles per jurisdiction
    per truck per quarter.
    """

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_data(
        self, ifta_reporter, mock_es_service
    ):
        """Returns empty list when no trucks have mileage data."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_truck": {"buckets": []},
            },
        }

        result = await ifta_reporter.get_all_trucks_mileage(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_per_truck_jurisdiction_breakdown(
        self, ifta_reporter, mock_es_service
    ):
        """Returns TruckJurisdictionMileage for each truck with per-state breakdown."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 10}},
            "aggregations": {
                "by_truck": {
                    "buckets": [
                        {
                            "key": "truck_001",
                            "doc_count": 5,
                            "truck_total_miles": {"value": 650.5},
                            "by_jurisdiction": {
                                "buckets": [
                                    {
                                        "key": "TX",
                                        "doc_count": 3,
                                        "total_miles": {"value": 450.5},
                                        "taxable_miles": {"value": 450.5},
                                    },
                                    {
                                        "key": "OK",
                                        "doc_count": 2,
                                        "total_miles": {"value": 200.0},
                                        "taxable_miles": {"value": 200.0},
                                    },
                                ]
                            },
                        },
                        {
                            "key": "truck_002",
                            "doc_count": 3,
                            "truck_total_miles": {"value": 300.0},
                            "by_jurisdiction": {
                                "buckets": [
                                    {
                                        "key": "CA",
                                        "doc_count": 2,
                                        "total_miles": {"value": 200.0},
                                        "taxable_miles": {"value": 200.0},
                                    },
                                    {
                                        "key": "NV",
                                        "doc_count": 1,
                                        "total_miles": {"value": 100.0},
                                        "taxable_miles": {"value": 100.0},
                                    },
                                ]
                            },
                        },
                    ]
                }
            },
        }

        result = await ifta_reporter.get_all_trucks_mileage(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert len(result) == 2

        # First truck
        assert isinstance(result[0], TruckJurisdictionMileage)
        assert result[0].truck_id == "truck_001"
        assert result[0].total_miles == 650.5
        assert len(result[0].jurisdictions) == 2
        assert result[0].jurisdictions[0].jurisdiction == "TX"
        assert result[0].jurisdictions[0].total_miles == 450.5
        assert result[0].jurisdictions[0].segment_count == 3
        assert result[0].jurisdictions[1].jurisdiction == "OK"
        assert result[0].jurisdictions[1].total_miles == 200.0

        # Second truck
        assert result[1].truck_id == "truck_002"
        assert result[1].total_miles == 300.0
        assert len(result[1].jurisdictions) == 2
        assert result[1].jurisdictions[0].jurisdiction == "CA"
        assert result[1].jurisdictions[0].total_miles == 200.0
        assert result[1].jurisdictions[1].jurisdiction == "NV"
        assert result[1].jurisdictions[1].total_miles == 100.0

    @pytest.mark.asyncio
    async def test_single_truck_single_jurisdiction(
        self, ifta_reporter, mock_es_service
    ):
        """Handles a single truck with mileage in only one state."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 2}},
            "aggregations": {
                "by_truck": {
                    "buckets": [
                        {
                            "key": "truck_solo",
                            "doc_count": 2,
                            "truck_total_miles": {"value": 500.0},
                            "by_jurisdiction": {
                                "buckets": [
                                    {
                                        "key": "FL",
                                        "doc_count": 2,
                                        "total_miles": {"value": 500.0},
                                        "taxable_miles": {"value": 500.0},
                                    },
                                ]
                            },
                        },
                    ]
                }
            },
        }

        result = await ifta_reporter.get_all_trucks_mileage(
            tenant_id="tenant_abc",
            quarter="2026-Q2",
        )

        assert len(result) == 1
        assert result[0].truck_id == "truck_solo"
        assert result[0].total_miles == 500.0
        assert len(result[0].jurisdictions) == 1
        assert result[0].jurisdictions[0].jurisdiction == "FL"
        assert result[0].jurisdictions[0].total_miles == 500.0

    @pytest.mark.asyncio
    async def test_query_includes_tenant_filter(
        self, ifta_reporter, mock_es_service
    ):
        """Query is scoped to the tenant via inject_tenant_filter."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_truck": {"buckets": []},
            },
        }

        await ifta_reporter.get_all_trucks_mileage(
            tenant_id="tenant_xyz",
            quarter="2026-Q3",
        )

        # Verify search was called
        mock_es_service.search_documents.assert_called_once()
        call_args = mock_es_service.search_documents.call_args

        # Check index name
        assert call_args[0][0] == "ifta_mileage"

        # The query should contain tenant_id filter
        query = call_args[0][1]
        query_str = str(query)
        assert "tenant_id" in query_str
        assert "tenant_xyz" in query_str

    @pytest.mark.asyncio
    async def test_query_filters_by_quarter(
        self, ifta_reporter, mock_es_service
    ):
        """Query filters by the specified quarter."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_truck": {"buckets": []},
            },
        }

        await ifta_reporter.get_all_trucks_mileage(
            tenant_id="tenant_abc",
            quarter="2026-Q4",
        )

        call_args = mock_es_service.search_documents.call_args
        query = call_args[0][1]
        query_str = str(query)
        assert "2026-Q4" in query_str

    @pytest.mark.asyncio
    async def test_query_uses_nested_aggregation_structure(
        self, ifta_reporter, mock_es_service
    ):
        """Query uses nested aggs: by_truck → by_jurisdiction."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_truck": {"buckets": []},
            },
        }

        await ifta_reporter.get_all_trucks_mileage(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        call_args = mock_es_service.search_documents.call_args
        query = call_args[0][1]

        # Verify nested aggregation structure
        assert "aggs" in query
        assert "by_truck" in query["aggs"]
        truck_agg = query["aggs"]["by_truck"]
        assert truck_agg["terms"]["field"] == "truck_id"
        assert "aggs" in truck_agg
        assert "by_jurisdiction" in truck_agg["aggs"]
        jur_agg = truck_agg["aggs"]["by_jurisdiction"]
        assert jur_agg["terms"]["field"] == "jurisdiction"
        assert "total_miles" in jur_agg["aggs"]
        assert "taxable_miles" in jur_agg["aggs"]

    @pytest.mark.asyncio
    async def test_taxable_miles_tracked_per_jurisdiction(
        self, ifta_reporter, mock_es_service
    ):
        """Taxable miles are correctly parsed from the aggregation response."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 4}},
            "aggregations": {
                "by_truck": {
                    "buckets": [
                        {
                            "key": "truck_003",
                            "doc_count": 4,
                            "truck_total_miles": {"value": 800.0},
                            "by_jurisdiction": {
                                "buckets": [
                                    {
                                        "key": "IL",
                                        "doc_count": 2,
                                        "total_miles": {"value": 400.0},
                                        "taxable_miles": {"value": 380.0},
                                    },
                                    {
                                        "key": "IN",
                                        "doc_count": 2,
                                        "total_miles": {"value": 400.0},
                                        "taxable_miles": {"value": 400.0},
                                    },
                                ]
                            },
                        },
                    ]
                }
            },
        }

        result = await ifta_reporter.get_all_trucks_mileage(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert result[0].jurisdictions[0].taxable_miles == 380.0
        assert result[0].jurisdictions[1].taxable_miles == 400.0


# ---------------------------------------------------------------------------
# Tests: TruckJurisdictionMileage model
# ---------------------------------------------------------------------------


class TestTruckJurisdictionMileageModel:
    """Tests for the TruckJurisdictionMileage Pydantic model."""

    def test_creates_with_required_fields(self):
        """TruckJurisdictionMileage can be created with truck_id."""
        entry = TruckJurisdictionMileage(
            truck_id="truck_001",
            jurisdictions=[
                JurisdictionMileage(
                    jurisdiction="TX",
                    total_miles=100.0,
                    taxable_miles=100.0,
                    segment_count=2,
                )
            ],
            total_miles=100.0,
        )
        assert entry.truck_id == "truck_001"
        assert entry.total_miles == 100.0
        assert len(entry.jurisdictions) == 1

    def test_defaults_to_empty_jurisdictions(self):
        """TruckJurisdictionMileage defaults to empty jurisdictions list."""
        entry = TruckJurisdictionMileage(truck_id="truck_empty")
        assert entry.jurisdictions == []
        assert entry.total_miles == 0.0


# ---------------------------------------------------------------------------
# Tests: _quarter_date_range helper
# ---------------------------------------------------------------------------


class TestQuarterDateRange:
    """Tests for the _quarter_date_range helper function."""

    def test_q1_date_range(self):
        from compliance.services.ifta_reporter import _quarter_date_range
        start, end = _quarter_date_range("2026-Q1")
        assert start == "2026-01-01"
        assert end == "2026-04-01"

    def test_q2_date_range(self):
        from compliance.services.ifta_reporter import _quarter_date_range
        start, end = _quarter_date_range("2026-Q2")
        assert start == "2026-04-01"
        assert end == "2026-07-01"

    def test_q3_date_range(self):
        from compliance.services.ifta_reporter import _quarter_date_range
        start, end = _quarter_date_range("2026-Q3")
        assert start == "2026-07-01"
        assert end == "2026-10-01"

    def test_q4_date_range(self):
        from compliance.services.ifta_reporter import _quarter_date_range
        start, end = _quarter_date_range("2026-Q4")
        assert start == "2026-10-01"
        assert end == "2027-01-01"

    def test_different_year(self):
        from compliance.services.ifta_reporter import _quarter_date_range
        start, end = _quarter_date_range("2025-Q2")
        assert start == "2025-04-01"
        assert end == "2025-07-01"


# ---------------------------------------------------------------------------
# Tests: JurisdictionFuelGallons model
# ---------------------------------------------------------------------------


class TestJurisdictionFuelGallonsModel:
    """Tests for the JurisdictionFuelGallons Pydantic model."""

    def test_creates_with_required_fields(self):
        """JurisdictionFuelGallons can be created with required fields."""
        from compliance.services.ifta_reporter import JurisdictionFuelGallons
        entry = JurisdictionFuelGallons(
            jurisdiction="TX",
            total_gallons=1500.0,
            source="terminal_bol",
        )
        assert entry.jurisdiction == "TX"
        assert entry.total_gallons == 1500.0
        assert entry.source == "terminal_bol"
        assert entry.truck_id is None
        assert entry.transaction_count == 0

    def test_creates_with_all_fields(self):
        """JurisdictionFuelGallons can be created with all fields."""
        from compliance.services.ifta_reporter import JurisdictionFuelGallons
        entry = JurisdictionFuelGallons(
            jurisdiction="OK",
            total_gallons=800.5,
            source="fuel_card",
            truck_id="truck_001",
            transaction_count=5,
        )
        assert entry.jurisdiction == "OK"
        assert entry.total_gallons == 800.5
        assert entry.source == "fuel_card"
        assert entry.truck_id == "truck_001"
        assert entry.transaction_count == 5

    def test_defaults_total_gallons_to_zero(self):
        """JurisdictionFuelGallons defaults total_gallons to 0.0."""
        from compliance.services.ifta_reporter import JurisdictionFuelGallons
        entry = JurisdictionFuelGallons(
            jurisdiction="CA",
            source="terminal_bol",
        )
        assert entry.total_gallons == 0.0


# ---------------------------------------------------------------------------
# Tests: TruckFuelGallons model
# ---------------------------------------------------------------------------


class TestTruckFuelGallonsModel:
    """Tests for the TruckFuelGallons Pydantic model."""

    def test_creates_with_required_fields(self):
        """TruckFuelGallons can be created with truck_id."""
        from compliance.services.ifta_reporter import TruckFuelGallons
        entry = TruckFuelGallons(truck_id="truck_001")
        assert entry.truck_id == "truck_001"
        assert entry.jurisdictions == []
        assert entry.total_gallons == 0.0

    def test_creates_with_jurisdictions(self):
        """TruckFuelGallons can be created with jurisdiction entries."""
        from compliance.services.ifta_reporter import (
            JurisdictionFuelGallons,
            TruckFuelGallons,
        )
        entry = TruckFuelGallons(
            truck_id="truck_002",
            jurisdictions=[
                JurisdictionFuelGallons(
                    jurisdiction="TX",
                    total_gallons=1000.0,
                    source="terminal_bol",
                    truck_id="truck_002",
                    transaction_count=3,
                ),
            ],
            total_gallons=1000.0,
        )
        assert entry.truck_id == "truck_002"
        assert len(entry.jurisdictions) == 1
        assert entry.total_gallons == 1000.0


# ---------------------------------------------------------------------------
# Tests: get_fuel_gallons_by_jurisdiction (Task 12.5, Req 7.3)
# ---------------------------------------------------------------------------


class TestGetFuelGallonsByJurisdiction:
    """Tests for IFTAReporter.get_fuel_gallons_by_jurisdiction.

    Validates: Requirement 7.3 — aggregate total fuel gallons purchased
    per jurisdiction per truck per quarter from terminal BOL ingestion
    records and fuel card transactions.
    """

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_data(
        self, ifta_reporter, mock_es_service
    ):
        """Returns empty list when no fuel purchase data exists."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_truck": {"buckets": []},
            },
        }

        result = await ifta_reporter.get_fuel_gallons_by_jurisdiction(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_terminal_bol_gallons_per_truck_per_jurisdiction(
        self, ifta_reporter, mock_es_service
    ):
        """Returns per-truck, per-jurisdiction fuel gallons from terminal BOLs."""
        # First call is for terminal_bols, second for fuel_card_transactions
        mock_es_service.search_documents.side_effect = [
            # terminal_bols response
            {
                "hits": {"hits": [], "total": {"value": 5}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 3,
                                "truck_total_gallons": {"value": 4500.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 3000.0},
                                        },
                                        {
                                            "key": "OK",
                                            "doc_count": 1,
                                            "total_gallons": {"value": 1500.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # fuel_card_transactions — index doesn't exist, raises exception
            Exception("index_not_found_exception"),
        ]

        result = await ifta_reporter.get_fuel_gallons_by_jurisdiction(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert len(result) == 1
        assert result[0].truck_id == "truck_001"
        assert result[0].total_gallons == 4500.0
        assert len(result[0].jurisdictions) == 2
        assert result[0].jurisdictions[0].jurisdiction == "TX"
        assert result[0].jurisdictions[0].total_gallons == 3000.0
        assert result[0].jurisdictions[0].source == "terminal_bol"
        assert result[0].jurisdictions[0].transaction_count == 2
        assert result[0].jurisdictions[1].jurisdiction == "OK"
        assert result[0].jurisdictions[1].total_gallons == 1500.0
        assert result[0].jurisdictions[1].source == "terminal_bol"

    @pytest.mark.asyncio
    async def test_merges_bol_and_fuel_card_data_for_same_truck(
        self, ifta_reporter, mock_es_service
    ):
        """Merges fuel gallons from both sources when same truck appears in both."""
        mock_es_service.search_documents.side_effect = [
            # terminal_bols response
            {
                "hits": {"hits": [], "total": {"value": 2}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 2,
                                "truck_total_gallons": {"value": 2000.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 2000.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # fuel_card_transactions response
            {
                "hits": {"hits": [], "total": {"value": 3}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 3,
                                "truck_total_gallons": {"value": 500.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "OK",
                                            "doc_count": 3,
                                            "total_gallons": {"value": 500.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
        ]

        result = await ifta_reporter.get_fuel_gallons_by_jurisdiction(
            tenant_id="tenant_abc",
            quarter="2026-Q2",
        )

        assert len(result) == 1
        assert result[0].truck_id == "truck_001"
        assert result[0].total_gallons == 2500.0  # 2000 + 500
        assert len(result[0].jurisdictions) == 2
        # BOL entry
        assert result[0].jurisdictions[0].jurisdiction == "TX"
        assert result[0].jurisdictions[0].source == "terminal_bol"
        assert result[0].jurisdictions[0].total_gallons == 2000.0
        # Fuel card entry
        assert result[0].jurisdictions[1].jurisdiction == "OK"
        assert result[0].jurisdictions[1].source == "fuel_card"
        assert result[0].jurisdictions[1].total_gallons == 500.0

    @pytest.mark.asyncio
    async def test_handles_multiple_trucks(
        self, ifta_reporter, mock_es_service
    ):
        """Returns data for multiple trucks from terminal BOLs."""
        mock_es_service.search_documents.side_effect = [
            # terminal_bols response with multiple trucks
            {
                "hits": {"hits": [], "total": {"value": 6}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 3,
                                "truck_total_gallons": {"value": 3000.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 3,
                                            "total_gallons": {"value": 3000.0},
                                        },
                                    ]
                                },
                            },
                            {
                                "key": "truck_002",
                                "doc_count": 3,
                                "truck_total_gallons": {"value": 2500.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "CA",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 1500.0},
                                        },
                                        {
                                            "key": "NV",
                                            "doc_count": 1,
                                            "total_gallons": {"value": 1000.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # fuel_card_transactions — exception (index doesn't exist)
            Exception("index_not_found_exception"),
        ]

        result = await ifta_reporter.get_fuel_gallons_by_jurisdiction(
            tenant_id="tenant_abc",
            quarter="2026-Q3",
        )

        assert len(result) == 2
        assert result[0].truck_id == "truck_001"
        assert result[0].total_gallons == 3000.0
        assert result[1].truck_id == "truck_002"
        assert result[1].total_gallons == 2500.0
        assert len(result[1].jurisdictions) == 2

    @pytest.mark.asyncio
    async def test_gracefully_handles_fuel_card_index_missing(
        self, ifta_reporter, mock_es_service
    ):
        """Gracefully returns BOL-only data when fuel_card_transactions index doesn't exist."""
        mock_es_service.search_documents.side_effect = [
            # terminal_bols response
            {
                "hits": {"hits": [], "total": {"value": 1}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 1,
                                "truck_total_gallons": {"value": 1000.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 1,
                                            "total_gallons": {"value": 1000.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # fuel_card_transactions raises exception
            Exception("index_not_found_exception"),
        ]

        result = await ifta_reporter.get_fuel_gallons_by_jurisdiction(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        # Should still return BOL data
        assert len(result) == 1
        assert result[0].truck_id == "truck_001"
        assert result[0].total_gallons == 1000.0

    @pytest.mark.asyncio
    async def test_queries_terminal_bols_with_date_range(
        self, ifta_reporter, mock_es_service
    ):
        """Query filters terminal_bols by issued_at date range for the quarter."""
        mock_es_service.search_documents.side_effect = [
            # terminal_bols response
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # fuel_card_transactions — exception
            Exception("index_not_found_exception"),
        ]

        await ifta_reporter.get_fuel_gallons_by_jurisdiction(
            tenant_id="tenant_abc",
            quarter="2026-Q2",
        )

        # First call should be to terminal_bols
        first_call = mock_es_service.search_documents.call_args_list[0]
        assert first_call[0][0] == "terminal_bols"

        query = first_call[0][1]
        query_str = str(query)
        # Should filter by issued_at date range for Q2 (Apr-Jun)
        assert "issued_at" in query_str
        assert "2026-04-01" in query_str
        assert "2026-07-01" in query_str

    @pytest.mark.asyncio
    async def test_excludes_rejected_bols(
        self, ifta_reporter, mock_es_service
    ):
        """Query excludes terminal BOLs with status 'rejected'."""
        mock_es_service.search_documents.side_effect = [
            # terminal_bols response
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # fuel_card_transactions — exception
            Exception("index_not_found_exception"),
        ]

        await ifta_reporter.get_fuel_gallons_by_jurisdiction(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        first_call = mock_es_service.search_documents.call_args_list[0]
        query = first_call[0][1]
        query_str = str(query)
        assert "rejected" in query_str
        assert "must_not" in query_str

    @pytest.mark.asyncio
    async def test_query_includes_tenant_filter(
        self, ifta_reporter, mock_es_service
    ):
        """Query is scoped to the tenant via inject_tenant_filter."""
        mock_es_service.search_documents.side_effect = [
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            Exception("index_not_found_exception"),
        ]

        await ifta_reporter.get_fuel_gallons_by_jurisdiction(
            tenant_id="tenant_xyz",
            quarter="2026-Q1",
        )

        first_call = mock_es_service.search_documents.call_args_list[0]
        query = first_call[0][1]
        query_str = str(query)
        assert "tenant_id" in query_str
        assert "tenant_xyz" in query_str

    @pytest.mark.asyncio
    async def test_different_trucks_from_different_sources(
        self, ifta_reporter, mock_es_service
    ):
        """Trucks appearing only in fuel cards are included in results."""
        mock_es_service.search_documents.side_effect = [
            # terminal_bols — truck_001 only
            {
                "hits": {"hits": [], "total": {"value": 1}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 1,
                                "truck_total_gallons": {"value": 1000.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 1,
                                            "total_gallons": {"value": 1000.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # fuel_card_transactions — truck_002 only
            {
                "hits": {"hits": [], "total": {"value": 2}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_002",
                                "doc_count": 2,
                                "truck_total_gallons": {"value": 750.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "FL",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 750.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
        ]

        result = await ifta_reporter.get_fuel_gallons_by_jurisdiction(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert len(result) == 2
        truck_ids = {r.truck_id for r in result}
        assert "truck_001" in truck_ids
        assert "truck_002" in truck_ids

        # Find each truck's data
        truck_001 = next(r for r in result if r.truck_id == "truck_001")
        truck_002 = next(r for r in result if r.truck_id == "truck_002")

        assert truck_001.total_gallons == 1000.0
        assert truck_001.jurisdictions[0].source == "terminal_bol"
        assert truck_002.total_gallons == 750.0
        assert truck_002.jurisdictions[0].source == "fuel_card"


# ---------------------------------------------------------------------------
# Tests: _merge_fuel_gallons static method
# ---------------------------------------------------------------------------


class TestMergeFuelGallons:
    """Tests for IFTAReporter._merge_fuel_gallons static method."""

    def test_merge_empty_lists(self):
        """Merging two empty lists returns empty list."""
        result = IFTAReporter._merge_fuel_gallons([], [])
        assert result == []

    def test_merge_bol_only(self):
        """Merging BOL data with empty fuel card returns BOL data."""
        from compliance.services.ifta_reporter import (
            JurisdictionFuelGallons,
            TruckFuelGallons,
        )
        bol_data = [
            TruckFuelGallons(
                truck_id="truck_001",
                jurisdictions=[
                    JurisdictionFuelGallons(
                        jurisdiction="TX",
                        total_gallons=1000.0,
                        source="terminal_bol",
                    )
                ],
                total_gallons=1000.0,
            )
        ]

        result = IFTAReporter._merge_fuel_gallons(bol_data, [])
        assert len(result) == 1
        assert result[0].truck_id == "truck_001"
        assert result[0].total_gallons == 1000.0

    def test_merge_fuel_card_only(self):
        """Merging empty BOL with fuel card data returns fuel card data."""
        from compliance.services.ifta_reporter import (
            JurisdictionFuelGallons,
            TruckFuelGallons,
        )
        fc_data = [
            TruckFuelGallons(
                truck_id="truck_002",
                jurisdictions=[
                    JurisdictionFuelGallons(
                        jurisdiction="OK",
                        total_gallons=500.0,
                        source="fuel_card",
                    )
                ],
                total_gallons=500.0,
            )
        ]

        result = IFTAReporter._merge_fuel_gallons([], fc_data)
        assert len(result) == 1
        assert result[0].truck_id == "truck_002"
        assert result[0].total_gallons == 500.0

    def test_merge_same_truck_combines_gallons(self):
        """Same truck in both sources gets combined total_gallons."""
        from compliance.services.ifta_reporter import (
            JurisdictionFuelGallons,
            TruckFuelGallons,
        )
        bol_data = [
            TruckFuelGallons(
                truck_id="truck_001",
                jurisdictions=[
                    JurisdictionFuelGallons(
                        jurisdiction="TX",
                        total_gallons=2000.0,
                        source="terminal_bol",
                    )
                ],
                total_gallons=2000.0,
            )
        ]
        fc_data = [
            TruckFuelGallons(
                truck_id="truck_001",
                jurisdictions=[
                    JurisdictionFuelGallons(
                        jurisdiction="OK",
                        total_gallons=800.0,
                        source="fuel_card",
                    )
                ],
                total_gallons=800.0,
            )
        ]

        result = IFTAReporter._merge_fuel_gallons(bol_data, fc_data)
        assert len(result) == 1
        assert result[0].truck_id == "truck_001"
        assert result[0].total_gallons == 2800.0
        assert len(result[0].jurisdictions) == 2


# ---------------------------------------------------------------------------
# Tests: JurisdictionIFTAEntry model (Task 12.6, Req 7.4)
# ---------------------------------------------------------------------------


class TestJurisdictionIFTAEntryModel:
    """Tests for the JurisdictionIFTAEntry Pydantic model.

    Validates: Requirement 7.4
    """

    def test_creates_with_required_fields(self):
        """JurisdictionIFTAEntry can be created with jurisdiction."""
        entry = JurisdictionIFTAEntry(jurisdiction="TX")
        assert entry.jurisdiction == "TX"
        assert entry.total_miles == 0.0
        assert entry.taxable_miles == 0.0
        assert entry.tax_paid_gallons == 0.0
        assert entry.net_taxable_gallons == 0.0
        assert entry.tax_rate == 0.0
        assert entry.tax_due == 0.0

    def test_creates_with_all_fields(self):
        """JurisdictionIFTAEntry can be created with all fields."""
        entry = JurisdictionIFTAEntry(
            jurisdiction="OK",
            total_miles=500.0,
            taxable_miles=480.0,
            tax_paid_gallons=100.0,
            net_taxable_gallons=-4.0,
            tax_rate=0.0,
            tax_due=0.0,
        )
        assert entry.jurisdiction == "OK"
        assert entry.total_miles == 500.0
        assert entry.taxable_miles == 480.0
        assert entry.tax_paid_gallons == 100.0
        assert entry.net_taxable_gallons == -4.0
        assert entry.tax_rate == 0.0
        assert entry.tax_due == 0.0

    def test_allows_negative_net_taxable_gallons(self):
        """Net taxable gallons can be negative (credit owed to carrier)."""
        entry = JurisdictionIFTAEntry(
            jurisdiction="CA",
            net_taxable_gallons=-50.0,
        )
        assert entry.net_taxable_gallons == -50.0


# ---------------------------------------------------------------------------
# Tests: TruckIFTASummary model (Task 12.6, Req 7.4)
# ---------------------------------------------------------------------------


class TestTruckIFTASummaryModel:
    """Tests for the TruckIFTASummary Pydantic model.

    Validates: Requirement 7.4
    """

    def test_creates_with_required_fields(self):
        """TruckIFTASummary can be created with truck_id."""
        summary = TruckIFTASummary(truck_id="truck_001")
        assert summary.truck_id == "truck_001"
        assert summary.jurisdictions == []
        assert summary.total_miles == 0.0
        assert summary.total_gallons == 0.0

    def test_creates_with_jurisdiction_entries(self):
        """TruckIFTASummary can be created with jurisdiction entries."""
        summary = TruckIFTASummary(
            truck_id="truck_002",
            jurisdictions=[
                JurisdictionIFTAEntry(
                    jurisdiction="TX",
                    total_miles=500.0,
                    taxable_miles=500.0,
                    tax_paid_gallons=80.0,
                    net_taxable_gallons=20.0,
                ),
                JurisdictionIFTAEntry(
                    jurisdiction="OK",
                    total_miles=300.0,
                    taxable_miles=300.0,
                    tax_paid_gallons=0.0,
                    net_taxable_gallons=60.0,
                ),
            ],
            total_miles=800.0,
            total_gallons=80.0,
        )
        assert summary.truck_id == "truck_002"
        assert len(summary.jurisdictions) == 2
        assert summary.total_miles == 800.0
        assert summary.total_gallons == 80.0


# ---------------------------------------------------------------------------
# Tests: IFTAReport model with trucks field (Task 12.6, Req 7.4)
# ---------------------------------------------------------------------------


class TestIFTAReportModelWithTrucks:
    """Tests for the updated IFTAReport model with trucks field.

    Validates: Requirement 7.4
    """

    def test_report_includes_trucks_field(self):
        """IFTAReport has a trucks field for per-truck summaries."""
        report = IFTAReport(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
            trucks=[
                TruckIFTASummary(
                    truck_id="truck_001",
                    total_miles=1000.0,
                    total_gallons=200.0,
                )
            ],
            total_miles=1000.0,
            total_gallons=200.0,
            truck_count=1,
        )
        assert len(report.trucks) == 1
        assert report.trucks[0].truck_id == "truck_001"

    def test_report_includes_total_gallons(self):
        """IFTAReport has a total_gallons field."""
        report = IFTAReport(
            tenant_id="tenant_abc",
            quarter="2026-Q2",
            total_gallons=500.0,
        )
        assert report.total_gallons == 500.0

    def test_report_defaults(self):
        """IFTAReport defaults trucks to empty list and total_gallons to 0."""
        report = IFTAReport(
            tenant_id="tenant_abc",
            quarter="2026-Q3",
        )
        assert report.trucks == []
        assert report.total_gallons == 0.0
        assert report.fleet_mpg is None


# ---------------------------------------------------------------------------
# Tests: check_data_completeness (Task 12.8, Req 7.6)
# ---------------------------------------------------------------------------


class TestCheckDataCompleteness:
    """Tests for IFTAReporter.check_data_completeness.

    Validates: Requirement 7.6 — IF Geotab odometer data is unavailable
    for a truck during a reporting period, THEN THE IFTA_Reporter SHALL
    flag that truck as `ifta_data_incomplete` and exclude it from the
    automated return while alerting the fleet manager.
    """

    @pytest.mark.asyncio
    async def test_all_trucks_have_data_returns_empty(
        self, ifta_reporter, mock_es_service
    ):
        """Returns empty list when all fleet trucks have mileage data."""
        # First call: _get_fleet_truck_ids (trucks index)
        # Second call: _get_trucks_with_mileage_data (ifta_mileage index)
        mock_es_service.search_documents.side_effect = [
            # _get_fleet_truck_ids response
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
            # _get_trucks_with_mileage_data response
            {
                "hits": {"hits": [], "total": {"value": 10}},
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
        ]

        flags = await ifta_reporter.check_data_completeness(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert flags == []

    @pytest.mark.asyncio
    async def test_trucks_without_data_are_flagged(
        self, ifta_reporter, mock_es_service
    ):
        """Trucks with no mileage data are flagged as ifta_data_incomplete."""
        mock_es_service.search_documents.side_effect = [
            # _get_fleet_truck_ids response — 3 trucks in fleet
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
            # _get_trucks_with_mileage_data response — only truck_001 has data
            {
                "hits": {"hits": [], "total": {"value": 5}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 5},
                        ]
                    }
                },
            },
        ]

        from compliance.services.ifta_reporter import IncompleteDataFlag

        flags = await ifta_reporter.check_data_completeness(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert len(flags) == 2
        assert all(isinstance(f, IncompleteDataFlag) for f in flags)

        # Flags should be sorted by truck_id
        assert flags[0].truck_id == "truck_002"
        assert flags[1].truck_id == "truck_003"

        # Verify flag fields
        for flag in flags:
            assert flag.flag_type == "ifta_data_incomplete"
            assert flag.quarter == "2026-Q1"
            assert "No Geotab odometer/mileage data" in flag.reason
            assert flag.flagged_at is not None

    @pytest.mark.asyncio
    async def test_alert_sent_when_notification_service_configured(
        self, ifta_reporter, mock_es_service, mock_notification_service
    ):
        """Alert is sent to fleet manager when trucks are flagged and notification service exists."""
        mock_es_service.search_documents.side_effect = [
            # _get_fleet_truck_ids response
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
            # _get_trucks_with_mileage_data response — only truck_001 has data
            {
                "hits": {"hits": [], "total": {"value": 5}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 5},
                        ]
                    }
                },
            },
        ]

        mock_notification_service.notify_event = AsyncMock(return_value=[])

        flags = await ifta_reporter.check_data_completeness(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert len(flags) == 1
        assert flags[0].truck_id == "truck_002"

        # Verify notification was sent
        mock_notification_service.notify_event.assert_called_once()
        call_kwargs = mock_notification_service.notify_event.call_args[1]
        assert call_kwargs["event_type"] == "ifta_data_incomplete"
        assert call_kwargs["tenant_id"] == "tenant_abc"
        assert call_kwargs["event_data"]["quarter"] == "2026-Q1"
        assert "truck_002" in call_kwargs["event_data"]["flagged_truck_ids"]
        assert call_kwargs["event_data"]["flagged_count"] == 1

    @pytest.mark.asyncio
    async def test_alert_skipped_when_no_notification_service(
        self, ifta_reporter_no_notifications, mock_es_service
    ):
        """No alert is sent when notification service is not configured."""
        mock_es_service.search_documents.side_effect = [
            # _get_fleet_truck_ids response
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
            # _get_trucks_with_mileage_data response — only truck_001 has data
            {
                "hits": {"hits": [], "total": {"value": 5}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 5},
                        ]
                    }
                },
            },
        ]

        flags = await ifta_reporter_no_notifications.check_data_completeness(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        # Flags are still returned even without notification service
        assert len(flags) == 1
        assert flags[0].truck_id == "truck_002"
        assert flags[0].flag_type == "ifta_data_incomplete"

    @pytest.mark.asyncio
    async def test_empty_fleet_returns_empty(
        self, ifta_reporter, mock_es_service
    ):
        """Returns empty list when no trucks exist in the fleet."""
        mock_es_service.search_documents.side_effect = [
            # _get_fleet_truck_ids response — no trucks
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {
                    "truck_ids": {"buckets": []}
                },
            },
        ]

        flags = await ifta_reporter.check_data_completeness(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert flags == []

    @pytest.mark.asyncio
    async def test_trucks_index_failure_returns_empty(
        self, ifta_reporter, mock_es_service
    ):
        """Returns empty list when trucks index query fails."""
        mock_es_service.search_documents.side_effect = [
            # _get_fleet_truck_ids raises exception
            Exception("index_not_found_exception"),
        ]

        flags = await ifta_reporter.check_data_completeness(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert flags == []


# ---------------------------------------------------------------------------
# Tests: generate_quarterly_report excludes flagged trucks (Task 12.8, Req 7.6)
# ---------------------------------------------------------------------------


class TestGenerateQuarterlyReportExcludesFlaggedTrucks:
    """Tests that generate_quarterly_report excludes trucks flagged as incomplete.

    Validates: Requirement 7.6 — flagged trucks are excluded from the
    automated return.
    """

    @pytest.mark.asyncio
    async def test_flagged_trucks_excluded_from_report(
        self, ifta_reporter, mock_es_service, mock_notification_service
    ):
        """Trucks with missing data are excluded from the IFTA report trucks list."""
        mock_notification_service.notify_event = AsyncMock(return_value=[])

        mock_es_service.search_documents.side_effect = [
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
            # check_data_completeness: _get_trucks_with_mileage_data
            # Only truck_001 has data; truck_002 is missing
            {
                "hits": {"hits": [], "total": {"value": 5}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 5},
                        ]
                    }
                },
            },
            # get_all_trucks_mileage — only truck_001 has mileage
            {
                "hits": {"hits": [], "total": {"value": 5}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 5,
                                "truck_total_miles": {"value": 1000.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 5,
                                            "total_miles": {"value": 1000.0},
                                            "taxable_miles": {"value": 1000.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols
            {
                "hits": {"hits": [], "total": {"value": 2}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 2,
                                "truck_total_gallons": {"value": 200.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 2,
                                            "total_gallons": {"value": 200.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        report = await ifta_reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        # Only truck_001 should be in the report (truck_002 is flagged)
        assert report.truck_count == 1
        assert len(report.trucks) == 1
        assert report.trucks[0].truck_id == "truck_001"

        # Incomplete trucks should be listed
        assert len(report.incomplete_trucks) == 1
        assert report.incomplete_trucks[0].truck_id == "truck_002"
        assert report.incomplete_trucks[0].flag_type == "ifta_data_incomplete"

    @pytest.mark.asyncio
    async def test_report_with_no_flagged_trucks(
        self, ifta_reporter, mock_es_service
    ):
        """Report includes all trucks when none are flagged."""
        mock_es_service.search_documents.side_effect = [
            # check_data_completeness: _get_fleet_truck_ids
            {
                "hits": {"hits": [], "total": {"value": 1}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 5},
                        ]
                    }
                },
            },
            # check_data_completeness: _get_trucks_with_mileage_data
            {
                "hits": {"hits": [], "total": {"value": 5}},
                "aggregations": {
                    "truck_ids": {
                        "buckets": [
                            {"key": "truck_001", "doc_count": 5},
                        ]
                    }
                },
            },
            # get_all_trucks_mileage
            {
                "hits": {"hits": [], "total": {"value": 5}},
                "aggregations": {
                    "by_truck": {
                        "buckets": [
                            {
                                "key": "truck_001",
                                "doc_count": 5,
                                "truck_total_miles": {"value": 500.0},
                                "by_jurisdiction": {
                                    "buckets": [
                                        {
                                            "key": "TX",
                                            "doc_count": 5,
                                            "total_miles": {"value": 500.0},
                                            "taxable_miles": {"value": 500.0},
                                        },
                                    ]
                                },
                            },
                        ]
                    }
                },
            },
            # get_fuel_gallons_by_jurisdiction: terminal_bols
            {
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"by_truck": {"buckets": []}},
            },
            # fuel_card_transactions
            Exception("index_not_found_exception"),
        ]

        report = await ifta_reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert report.truck_count == 1
        assert len(report.trucks) == 1
        assert report.trucks[0].truck_id == "truck_001"
        assert report.incomplete_trucks == []


# ---------------------------------------------------------------------------
# Tests: IncompleteDataFlag model
# ---------------------------------------------------------------------------


class TestIncompleteDataFlagModel:
    """Tests for the IncompleteDataFlag Pydantic model."""

    def test_model_creation(self):
        """IncompleteDataFlag can be created with required fields."""
        from compliance.services.ifta_reporter import IncompleteDataFlag

        flag = IncompleteDataFlag(
            truck_id="truck_001",
            quarter="2026-Q1",
            reason="No Geotab data for truck_001 during 2026-Q1",
        )

        assert flag.truck_id == "truck_001"
        assert flag.flag_type == "ifta_data_incomplete"
        assert flag.quarter == "2026-Q1"
        assert flag.reason == "No Geotab data for truck_001 during 2026-Q1"
        assert flag.flagged_at is not None

    def test_default_flag_type(self):
        """flag_type defaults to 'ifta_data_incomplete'."""
        from compliance.services.ifta_reporter import IncompleteDataFlag

        flag = IncompleteDataFlag(
            truck_id="truck_002",
            quarter="2026-Q2",
            reason="Missing data",
        )

        assert flag.flag_type == "ifta_data_incomplete"


# ---------------------------------------------------------------------------
# Tests: MileageAdjustment model
# ---------------------------------------------------------------------------


class TestMileageAdjustmentModel:
    """Tests for the MileageAdjustment Pydantic model.

    Validates: Requirement 7.7
    """

    def test_model_creation_with_positive_miles(self):
        """MileageAdjustment can be created with positive miles."""
        from compliance.services.ifta_reporter import MileageAdjustment

        adj = MileageAdjustment(
            tenant_id="tenant_abc",
            truck_id="truck_001",
            jurisdiction="TX",
            miles=50.0,
            quarter="2026-Q1",
            operator_id="operator_001",
            reason="Odometer discrepancy correction",
        )

        assert adj.tenant_id == "tenant_abc"
        assert adj.truck_id == "truck_001"
        assert adj.jurisdiction == "TX"
        assert adj.miles == 50.0
        assert adj.quarter == "2026-Q1"
        assert adj.operator_id == "operator_001"
        assert adj.reason == "Odometer discrepancy correction"
        assert adj.adjustment_id.startswith("adj_")
        assert adj.created_at is not None

    def test_model_creation_with_negative_miles(self):
        """MileageAdjustment supports negative miles for subtracting."""
        from compliance.services.ifta_reporter import MileageAdjustment

        adj = MileageAdjustment(
            tenant_id="tenant_abc",
            truck_id="truck_002",
            jurisdiction="OK",
            miles=-25.5,
            quarter="2026-Q2",
            operator_id="operator_002",
            reason="Duplicate segment removed",
        )

        assert adj.miles == -25.5

    def test_adjustment_id_auto_generated(self):
        """adjustment_id is auto-generated with adj_ prefix."""
        from compliance.services.ifta_reporter import MileageAdjustment

        adj = MileageAdjustment(
            tenant_id="tenant_abc",
            truck_id="truck_001",
            jurisdiction="TX",
            miles=10.0,
            quarter="2026-Q1",
            operator_id="op_1",
            reason="Test",
        )

        assert adj.adjustment_id.startswith("adj_")
        assert len(adj.adjustment_id) > 4  # adj_ + uuid


# ---------------------------------------------------------------------------
# Tests: record_manual_adjustment
# ---------------------------------------------------------------------------


class TestRecordManualAdjustment:
    """Tests for IFTAReporter.record_manual_adjustment.

    Validates: Requirement 7.7 — manual mileage adjustments with audit trail.
    """

    @pytest.mark.asyncio
    async def test_positive_adjustment_persisted_to_es(
        self, ifta_reporter, mock_es_service
    ):
        """Positive adjustment creates a record in ifta_mileage index."""
        from compliance.services.ifta_reporter import MileageAdjustment

        result = await ifta_reporter.record_manual_adjustment(
            tenant_id="tenant_abc",
            truck_id="truck_001",
            jurisdiction="TX",
            miles=75.0,
            quarter="2026-Q1",
            operator_id="operator_001",
            reason="Odometer recalibration correction",
        )

        # Verify return type
        assert isinstance(result, MileageAdjustment)
        assert result.miles == 75.0
        assert result.jurisdiction == "TX"
        assert result.operator_id == "operator_001"
        assert result.reason == "Odometer recalibration correction"

        # Verify ES index_document was called
        mock_es_service.index_document.assert_called_once()
        call_args = mock_es_service.index_document.call_args

        # Check index name
        assert call_args[0][0] == "ifta_mileage"

        # Check document content
        doc = call_args[0][2]
        assert doc["tenant_id"] == "tenant_abc"
        assert doc["truck_id"] == "truck_001"
        assert doc["jurisdiction"] == "TX"
        assert doc["miles"] == 75.0
        assert doc["quarter"] == "2026-Q1"
        assert doc["source"] == "manual_adjustment"
        assert doc["operator_id"] == "operator_001"
        assert doc["reason"] == "Odometer recalibration correction"

    @pytest.mark.asyncio
    async def test_negative_adjustment_persisted_to_es(
        self, ifta_reporter, mock_es_service
    ):
        """Negative adjustment (subtracting miles) is persisted correctly."""
        from compliance.services.ifta_reporter import MileageAdjustment

        result = await ifta_reporter.record_manual_adjustment(
            tenant_id="tenant_abc",
            truck_id="truck_002",
            jurisdiction="OK",
            miles=-30.5,
            quarter="2026-Q2",
            operator_id="operator_002",
            reason="Duplicate trip segment identified during review",
        )

        assert isinstance(result, MileageAdjustment)
        assert result.miles == -30.5

        # Verify ES document has negative miles
        doc = mock_es_service.index_document.call_args[0][2]
        assert doc["miles"] == -30.5
        assert doc["taxable_miles"] == -30.5
        assert doc["source"] == "manual_adjustment"

    @pytest.mark.asyncio
    async def test_audit_trail_fields_persisted(
        self, ifta_reporter, mock_es_service
    ):
        """Operator ID and reason are persisted for audit trail purposes."""
        await ifta_reporter.record_manual_adjustment(
            tenant_id="tenant_abc",
            truck_id="truck_003",
            jurisdiction="CA",
            miles=100.0,
            quarter="2026-Q3",
            operator_id="admin_jane",
            reason="GPS signal loss caused missed boundary crossing",
        )

        doc = mock_es_service.index_document.call_args[0][2]
        assert doc["operator_id"] == "admin_jane"
        assert doc["reason"] == "GPS signal loss caused missed boundary crossing"
        assert "created_at" in doc
        assert "updated_at" in doc
        assert doc["source"] == "manual_adjustment"

    @pytest.mark.asyncio
    async def test_jurisdiction_uppercased(
        self, ifta_reporter, mock_es_service
    ):
        """Jurisdiction is stored as uppercase regardless of input case."""
        result = await ifta_reporter.record_manual_adjustment(
            tenant_id="tenant_abc",
            truck_id="truck_001",
            jurisdiction="tx",
            miles=50.0,
            quarter="2026-Q1",
            operator_id="op_1",
            reason="Test",
        )

        assert result.jurisdiction == "TX"
        doc = mock_es_service.index_document.call_args[0][2]
        assert doc["jurisdiction"] == "TX"

    @pytest.mark.asyncio
    async def test_adjustment_id_used_as_document_id(
        self, ifta_reporter, mock_es_service
    ):
        """The adjustment_id is used as the ES document ID."""
        result = await ifta_reporter.record_manual_adjustment(
            tenant_id="tenant_abc",
            truck_id="truck_001",
            jurisdiction="TX",
            miles=10.0,
            quarter="2026-Q1",
            operator_id="op_1",
            reason="Test",
        )

        # Document ID (second positional arg) should be the adjustment_id
        doc_id = mock_es_service.index_document.call_args[0][1]
        assert doc_id == result.adjustment_id
        assert doc_id.startswith("adj_")


# ---------------------------------------------------------------------------
# Tests: get_adjustment_history
# ---------------------------------------------------------------------------


class TestGetAdjustmentHistory:
    """Tests for IFTAReporter.get_adjustment_history.

    Validates: Requirement 7.7 — querying manual adjustment records.
    """

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_adjustments(
        self, ifta_reporter, mock_es_service
    ):
        """Returns empty list when no manual adjustments exist."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
        }

        result = await ifta_reporter.get_adjustment_history(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_adjustment_records(
        self, ifta_reporter, mock_es_service
    ):
        """Returns MileageAdjustment records from ES query results."""
        from compliance.services.ifta_reporter import MileageAdjustment

        mock_es_service.search_documents.return_value = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "record_id": "adj_001",
                            "tenant_id": "tenant_abc",
                            "truck_id": "truck_001",
                            "jurisdiction": "TX",
                            "miles": 50.0,
                            "quarter": "2026-Q1",
                            "operator_id": "operator_001",
                            "reason": "Correction for GPS loss",
                            "created_at": "2026-03-15T10:00:00+00:00",
                        }
                    },
                    {
                        "_source": {
                            "record_id": "adj_002",
                            "tenant_id": "tenant_abc",
                            "truck_id": "truck_002",
                            "jurisdiction": "OK",
                            "miles": -20.0,
                            "quarter": "2026-Q1",
                            "operator_id": "operator_002",
                            "reason": "Duplicate segment removed",
                            "created_at": "2026-03-14T09:00:00+00:00",
                        }
                    },
                ],
                "total": {"value": 2},
            },
        }

        result = await ifta_reporter.get_adjustment_history(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert len(result) == 2
        assert all(isinstance(r, MileageAdjustment) for r in result)

        # First result (most recent)
        assert result[0].adjustment_id == "adj_001"
        assert result[0].truck_id == "truck_001"
        assert result[0].jurisdiction == "TX"
        assert result[0].miles == 50.0
        assert result[0].operator_id == "operator_001"
        assert result[0].reason == "Correction for GPS loss"

        # Second result
        assert result[1].adjustment_id == "adj_002"
        assert result[1].truck_id == "truck_002"
        assert result[1].jurisdiction == "OK"
        assert result[1].miles == -20.0
        assert result[1].operator_id == "operator_002"

    @pytest.mark.asyncio
    async def test_query_filters_by_manual_adjustment_source(
        self, ifta_reporter, mock_es_service
    ):
        """Query includes source=manual_adjustment filter."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
        }

        await ifta_reporter.get_adjustment_history(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        # Verify search was called with correct filters
        mock_es_service.search_documents.assert_called_once()
        call_args = mock_es_service.search_documents.call_args

        # Check index name
        assert call_args[0][0] == "ifta_mileage"

        # Check query includes source filter
        query = call_args[0][1]
        query_str = str(query)
        assert "manual_adjustment" in query_str
        assert "tenant_id" in query_str

    @pytest.mark.asyncio
    async def test_results_sorted_by_created_at_descending(
        self, ifta_reporter, mock_es_service
    ):
        """Query specifies sort by created_at descending."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
        }

        await ifta_reporter.get_adjustment_history(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        call_args = mock_es_service.search_documents.call_args
        query = call_args[0][1]

        # Verify sort is specified
        assert "sort" in query
        assert query["sort"] == [{"created_at": {"order": "desc"}}]
