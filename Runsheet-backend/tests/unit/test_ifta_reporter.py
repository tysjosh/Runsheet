"""Unit tests for compliance/services/ifta_reporter.py.

Tests cover:
- Service initialization with required dependencies
- record_trip_segment persistence to the ifta_mileage ES index
- Quarter computation from timestamps
- get_mileage_by_jurisdiction aggregation query

Validates: Requirement 7.1
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from compliance.services.ifta_reporter import (
    IFTAReport,
    IFTAReporter,
    JurisdictionMileage,
    TripSegment,
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
    """Tests for IFTAReporter.generate_quarterly_report."""

    @pytest.mark.asyncio
    async def test_returns_ifta_report(self, ifta_reporter, mock_es_service):
        """generate_quarterly_report returns an IFTAReport model."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {
                "by_jurisdiction": {"buckets": []},
                "unique_trucks": {"value": 0},
                "total_miles_all": {"value": 0.0},
            },
        }

        report = await ifta_reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q1",
        )

        assert isinstance(report, IFTAReport)
        assert report.tenant_id == "tenant_abc"
        assert report.quarter == "2026-Q1"
        assert report.jurisdictions == []
        assert report.total_miles == 0.0
        assert report.truck_count == 0

    @pytest.mark.asyncio
    async def test_report_with_data(self, ifta_reporter, mock_es_service):
        """Report includes jurisdiction breakdown when data exists."""
        mock_es_service.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 10}},
            "aggregations": {
                "by_jurisdiction": {
                    "buckets": [
                        {
                            "key": "TX",
                            "doc_count": 5,
                            "total_miles": {"value": 1200.0},
                            "taxable_miles": {"value": 1200.0},
                        },
                        {
                            "key": "OK",
                            "doc_count": 3,
                            "total_miles": {"value": 600.0},
                            "taxable_miles": {"value": 600.0},
                        },
                    ]
                },
                "unique_trucks": {"value": 3},
                "total_miles_all": {"value": 1800.0},
            },
        }

        report = await ifta_reporter.generate_quarterly_report(
            tenant_id="tenant_abc",
            quarter="2026-Q2",
        )

        assert report.total_miles == 1800.0
        assert report.truck_count == 3
        assert len(report.jurisdictions) == 2
        assert report.jurisdictions[0].jurisdiction == "TX"
        assert report.jurisdictions[0].total_miles == 1200.0


# ---------------------------------------------------------------------------
# Tests: compute_fleet_mpg
# ---------------------------------------------------------------------------


class TestComputeFleetMpg:
    """Tests for IFTAReporter.compute_fleet_mpg."""

    @pytest.mark.asyncio
    async def test_returns_zero_placeholder(self, ifta_reporter):
        """compute_fleet_mpg returns 0.0 as placeholder (task 12.7)."""
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
