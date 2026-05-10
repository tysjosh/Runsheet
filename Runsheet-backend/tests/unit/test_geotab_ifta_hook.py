"""Unit tests for the GeotabConnector IFTA boundary detection hook.

Tests cover:
- set_ifta_reporter() injection method
- _process_ifta_boundary_check() state boundary detection
- _compute_segment_miles() odometer-based and haversine-based computation
- Integration with sync_pull flow (IFTA hook is called for mapped trucks)
- Optional behavior (sync_pull works normally without IFTA configured)
- Error isolation (IFTA failures don't abort sync_pull)

Validates: Requirement 7.1
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.geotab import (
    GeotabConnector,
    _haversine_miles,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_vault():
    """Create a mock credentials vault."""
    vault = AsyncMock()
    vault.get = AsyncMock(return_value={
        "username": "test@example.com",
        "password": "secret",
        "database": "test_db",
        "server": "my.geotab.com",
        "session_id": "test_session_123",
        "session_expires_at": "2026-12-31T00:00:00Z",
    })
    vault.put = AsyncMock(return_value="vault_ref_123")
    vault.delete = AsyncMock(return_value=None)
    return vault


@pytest.fixture
def mock_es_service():
    """Create a mock ElasticsearchService."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value=None)
    es.update_document = AsyncMock(return_value=None)
    return es


@pytest.fixture
def mock_ifta_reporter():
    """Create a mock IFTAReporter."""
    reporter = AsyncMock()
    reporter.record_trip_segment = AsyncMock(return_value=MagicMock(
        record_id="ifta_test_123",
        tenant_id="tenant_abc",
        truck_id="truck_001",
        jurisdiction="TX",
        miles=50.0,
        quarter="2026-Q1",
    ))
    return reporter


@pytest.fixture
def mock_boundary_detector():
    """Create a mock StateBoundaryDetector."""
    detector = MagicMock()
    detector.get_state = MagicMock(return_value="TX")
    return detector


@pytest.fixture
def base_connector(mock_vault, mock_es_service):
    """Create a base GeotabConnector without IFTA hook."""
    return GeotabConnector(
        tenant_id="tenant_abc",
        instance_id="inst_001",
        instance_config={"device_map": {"device_A": "truck_001"}},
        credentials_vault=mock_vault,
        credentials_ref="vault_ref_123",
        es_service=mock_es_service,
        clock=lambda: datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def connector_with_ifta(base_connector, mock_ifta_reporter, mock_boundary_detector):
    """Create a GeotabConnector with IFTA hook configured."""
    base_connector.set_ifta_reporter(mock_ifta_reporter, mock_boundary_detector)
    return base_connector


# ---------------------------------------------------------------------------
# Tests: set_ifta_reporter
# ---------------------------------------------------------------------------


class TestSetIftaReporter:
    """Tests for GeotabConnector.set_ifta_reporter."""

    def test_sets_ifta_reporter(self, base_connector, mock_ifta_reporter, mock_boundary_detector):
        """set_ifta_reporter stores the reporter and detector."""
        base_connector.set_ifta_reporter(mock_ifta_reporter, mock_boundary_detector)

        assert base_connector._ifta_reporter is mock_ifta_reporter
        assert base_connector._state_boundary_detector is mock_boundary_detector

    def test_ifta_reporter_initially_none(self, base_connector):
        """IFTA reporter is None by default."""
        assert base_connector._ifta_reporter is None
        assert base_connector._state_boundary_detector is None

    def test_ifta_truck_state_initially_empty(self, base_connector):
        """Per-truck IFTA state tracking is empty by default."""
        assert base_connector._ifta_truck_state == {}


# ---------------------------------------------------------------------------
# Tests: _process_ifta_boundary_check
# ---------------------------------------------------------------------------


class TestProcessIftaBoundaryCheck:
    """Tests for GeotabConnector._process_ifta_boundary_check."""

    @pytest.mark.asyncio
    async def test_first_reading_initializes_state(
        self, connector_with_ifta, mock_boundary_detector, mock_ifta_reporter
    ):
        """First reading for a truck initializes state without recording a segment."""
        mock_boundary_detector.get_state.return_value = "TX"

        reading = {
            "device_id": "device_A",
            "latitude": 32.7767,
            "longitude": -96.7970,
            "odometer_km": 50000.0,
            "recorded_at": datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
        }

        await connector_with_ifta._process_ifta_boundary_check(
            reading=reading,
            truck_id="truck_001",
            recorded_at=reading["recorded_at"],
        )

        # No segment should be recorded on first reading
        mock_ifta_reporter.record_trip_segment.assert_not_called()

        # State should be initialized
        assert "truck_001" in connector_with_ifta._ifta_truck_state
        assert connector_with_ifta._ifta_truck_state["truck_001"]["last_state"] == "TX"

    @pytest.mark.asyncio
    async def test_same_state_no_crossing(
        self, connector_with_ifta, mock_boundary_detector, mock_ifta_reporter
    ):
        """Consecutive readings in the same state do not record a segment."""
        mock_boundary_detector.get_state.return_value = "TX"

        # Initialize state
        connector_with_ifta._ifta_truck_state["truck_001"] = {
            "last_state": "TX",
            "last_odometer_km": 50000.0,
            "last_lat": 32.7767,
            "last_lon": -96.7970,
            "last_timestamp": datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
        }

        reading = {
            "device_id": "device_A",
            "latitude": 32.8000,
            "longitude": -96.8000,
            "odometer_km": 50010.0,
            "recorded_at": datetime(2026, 2, 15, 10, 5, 0, tzinfo=timezone.utc),
        }

        await connector_with_ifta._process_ifta_boundary_check(
            reading=reading,
            truck_id="truck_001",
            recorded_at=reading["recorded_at"],
        )

        mock_ifta_reporter.record_trip_segment.assert_not_called()

    @pytest.mark.asyncio
    async def test_boundary_crossing_records_segment(
        self, connector_with_ifta, mock_boundary_detector, mock_ifta_reporter
    ):
        """State boundary crossing triggers record_trip_segment call."""
        # Truck was in TX, now crosses into OK
        mock_boundary_detector.get_state.return_value = "OK"

        connector_with_ifta._ifta_truck_state["truck_001"] = {
            "last_state": "TX",
            "last_odometer_km": 50000.0,
            "last_lat": 33.9,
            "last_lon": -96.5,
            "last_timestamp": datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
        }

        reading = {
            "device_id": "device_A",
            "latitude": 34.0,
            "longitude": -96.5,
            "odometer_km": 50100.0,  # 100 km driven
            "recorded_at": datetime(2026, 2, 15, 11, 0, 0, tzinfo=timezone.utc),
        }

        await connector_with_ifta._process_ifta_boundary_check(
            reading=reading,
            truck_id="truck_001",
            recorded_at=reading["recorded_at"],
        )

        # record_trip_segment should be called
        mock_ifta_reporter.record_trip_segment.assert_called_once()
        call_kwargs = mock_ifta_reporter.record_trip_segment.call_args[1]
        assert call_kwargs["tenant_id"] == "tenant_abc"
        assert call_kwargs["truck_id"] == "truck_001"
        assert call_kwargs["from_state"] == "TX"
        assert call_kwargs["to_state"] == "OK"
        assert call_kwargs["source"] == "geotab"
        # 100 km * 0.621371 = 62.1 miles
        assert abs(call_kwargs["miles"] - 62.1) < 0.2

    @pytest.mark.asyncio
    async def test_boundary_crossing_updates_truck_state(
        self, connector_with_ifta, mock_boundary_detector
    ):
        """After a crossing, truck state is updated to the new state."""
        mock_boundary_detector.get_state.return_value = "OK"

        connector_with_ifta._ifta_truck_state["truck_001"] = {
            "last_state": "TX",
            "last_odometer_km": 50000.0,
            "last_lat": 33.9,
            "last_lon": -96.5,
            "last_timestamp": datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
        }

        reading = {
            "device_id": "device_A",
            "latitude": 34.0,
            "longitude": -96.5,
            "odometer_km": 50100.0,
            "recorded_at": datetime(2026, 2, 15, 11, 0, 0, tzinfo=timezone.utc),
        }

        await connector_with_ifta._process_ifta_boundary_check(
            reading=reading,
            truck_id="truck_001",
            recorded_at=reading["recorded_at"],
        )

        # State should now be OK
        assert connector_with_ifta._ifta_truck_state["truck_001"]["last_state"] == "OK"
        assert connector_with_ifta._ifta_truck_state["truck_001"]["last_odometer_km"] == 50100.0

    @pytest.mark.asyncio
    async def test_none_state_skips_processing(
        self, connector_with_ifta, mock_boundary_detector, mock_ifta_reporter
    ):
        """When boundary detector returns None, no segment is recorded."""
        mock_boundary_detector.get_state.return_value = None

        reading = {
            "device_id": "device_A",
            "latitude": 50.0,  # Outside US
            "longitude": -130.0,
            "odometer_km": 50000.0,
            "recorded_at": datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
        }

        await connector_with_ifta._process_ifta_boundary_check(
            reading=reading,
            truck_id="truck_001",
            recorded_at=reading["recorded_at"],
        )

        mock_ifta_reporter.record_trip_segment.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_lat_lon_skips_processing(
        self, connector_with_ifta, mock_boundary_detector, mock_ifta_reporter
    ):
        """When lat/lon is None, IFTA processing is skipped."""
        reading = {
            "device_id": "device_A",
            "latitude": None,
            "longitude": None,
            "odometer_km": 50000.0,
            "recorded_at": datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
        }

        await connector_with_ifta._process_ifta_boundary_check(
            reading=reading,
            truck_id="truck_001",
            recorded_at=reading["recorded_at"],
        )

        mock_boundary_detector.get_state.assert_not_called()
        mock_ifta_reporter.record_trip_segment.assert_not_called()

    @pytest.mark.asyncio
    async def test_ifta_reporter_error_is_non_fatal(
        self, connector_with_ifta, mock_boundary_detector, mock_ifta_reporter
    ):
        """Errors from IFTAReporter.record_trip_segment are logged but non-fatal."""
        mock_boundary_detector.get_state.return_value = "OK"
        mock_ifta_reporter.record_trip_segment.side_effect = RuntimeError("ES down")

        connector_with_ifta._ifta_truck_state["truck_001"] = {
            "last_state": "TX",
            "last_odometer_km": 50000.0,
            "last_lat": 33.9,
            "last_lon": -96.5,
            "last_timestamp": datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc),
        }

        reading = {
            "device_id": "device_A",
            "latitude": 34.0,
            "longitude": -96.5,
            "odometer_km": 50100.0,
            "recorded_at": datetime(2026, 2, 15, 11, 0, 0, tzinfo=timezone.utc),
        }

        # Should not raise
        await connector_with_ifta._process_ifta_boundary_check(
            reading=reading,
            truck_id="truck_001",
            recorded_at=reading["recorded_at"],
        )

        # The call was attempted
        mock_ifta_reporter.record_trip_segment.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: _compute_segment_miles
# ---------------------------------------------------------------------------


class TestComputeSegmentMiles:
    """Tests for GeotabConnector._compute_segment_miles."""

    def test_odometer_based_computation(self, base_connector):
        """Uses odometer difference when both readings are available."""
        truck_state = {
            "last_state": "TX",
            "last_odometer_km": 50000.0,
            "last_lat": 32.0,
            "last_lon": -96.0,
        }

        miles = base_connector._compute_segment_miles(
            truck_state=truck_state,
            current_odometer_km=50161.0,  # 161 km = ~100 miles
            current_lat=34.0,
            current_lon=-96.0,
        )

        # 161 km * 0.621371 = ~100.0 miles
        assert abs(miles - 100.0) < 0.5

    def test_haversine_fallback_when_no_current_odometer(self, base_connector):
        """Falls back to haversine when current odometer is None."""
        truck_state = {
            "last_state": "TX",
            "last_odometer_km": 50000.0,
            "last_lat": 32.0,
            "last_lon": -96.0,
        }

        miles = base_connector._compute_segment_miles(
            truck_state=truck_state,
            current_odometer_km=None,
            current_lat=33.0,
            current_lon=-96.0,
        )

        # ~69 miles (1 degree latitude ≈ 69 miles)
        assert 60.0 < miles < 80.0

    def test_haversine_fallback_when_no_previous_odometer(self, base_connector):
        """Falls back to haversine when previous odometer is None."""
        truck_state = {
            "last_state": "TX",
            "last_odometer_km": None,
            "last_lat": 32.0,
            "last_lon": -96.0,
        }

        miles = base_connector._compute_segment_miles(
            truck_state=truck_state,
            current_odometer_km=50100.0,
            current_lat=33.0,
            current_lon=-96.0,
        )

        # Should use haversine since prev odometer is None
        assert 60.0 < miles < 80.0

    def test_returns_zero_when_no_data(self, base_connector):
        """Returns 0.0 when neither odometer nor GPS data is available."""
        truck_state = {
            "last_state": "TX",
            "last_odometer_km": None,
            "last_lat": None,
            "last_lon": None,
        }

        miles = base_connector._compute_segment_miles(
            truck_state=truck_state,
            current_odometer_km=None,
            current_lat=34.0,
            current_lon=-96.0,
        )

        assert miles == 0.0

    def test_odometer_decrease_uses_haversine(self, base_connector):
        """When odometer decreases (reset/error), falls back to haversine."""
        truck_state = {
            "last_state": "TX",
            "last_odometer_km": 50000.0,
            "last_lat": 32.0,
            "last_lon": -96.0,
        }

        miles = base_connector._compute_segment_miles(
            truck_state=truck_state,
            current_odometer_km=49000.0,  # Decreased — anomaly
            current_lat=33.0,
            current_lon=-96.0,
        )

        # Should use haversine fallback
        assert 60.0 < miles < 80.0


# ---------------------------------------------------------------------------
# Tests: _haversine_miles helper
# ---------------------------------------------------------------------------


class TestHaversineMiles:
    """Tests for the _haversine_miles helper function."""

    def test_same_point_returns_zero(self):
        """Same point returns 0 distance."""
        assert _haversine_miles(32.0, -96.0, 32.0, -96.0) == 0.0

    def test_one_degree_latitude(self):
        """One degree of latitude is approximately 69 miles."""
        miles = _haversine_miles(32.0, -96.0, 33.0, -96.0)
        assert 68.0 < miles < 70.0

    def test_known_distance_dallas_to_okc(self):
        """Dallas (32.78, -96.80) to OKC (35.47, -97.52) is ~190 miles."""
        miles = _haversine_miles(32.78, -96.80, 35.47, -97.52)
        assert 180.0 < miles < 200.0


# ---------------------------------------------------------------------------
# Tests: Integration with sync_pull (optional hook behavior)
# ---------------------------------------------------------------------------


class TestSyncPullIftaIntegration:
    """Tests verifying IFTA hook integrates correctly with sync_pull flow."""

    @pytest.mark.asyncio
    async def test_sync_pull_works_without_ifta_configured(
        self, mock_vault, mock_es_service
    ):
        """sync_pull operates normally when IFTA reporter is not configured."""
        # Create a connector with a scripted SDK call that returns one reading
        def sdk_call(method, params, **kwargs):
            if method == "Get" and params.get("typeName") == "DeviceStatusInfo":
                return {
                    "result": [
                        {
                            "device": {"id": "device_A"},
                            "latitude": 32.7767,
                            "longitude": -96.7970,
                            "speed": 60.0,
                            "odometer": 50000.0,
                            "dateTime": "2026-02-15T12:00:00Z",
                        }
                    ]
                }
            return {"result": []}

        connector = GeotabConnector(
            tenant_id="tenant_abc",
            instance_id="inst_001",
            instance_config={"device_map": {"device_A": "truck_001"}},
            credentials_vault=mock_vault,
            credentials_ref="vault_ref_123",
            es_service=mock_es_service,
            sdk_call=sdk_call,
            clock=lambda: datetime(2026, 2, 15, 12, 0, 5, tzinfo=timezone.utc),
        )

        since = datetime(2026, 2, 15, 11, 0, 0, tzinfo=timezone.utc)
        run = await connector.sync_pull(since)

        # Should succeed without IFTA configured
        assert run.status in ("success", "partial")
        assert run.record_counts["readings_persisted"] >= 1

    @pytest.mark.asyncio
    async def test_sync_pull_calls_ifta_hook_on_mapped_trucks(
        self, mock_vault, mock_es_service, mock_ifta_reporter, mock_boundary_detector
    ):
        """sync_pull calls IFTA boundary check for mapped truck readings."""
        mock_boundary_detector.get_state.return_value = "TX"

        def sdk_call(method, params, **kwargs):
            if method == "Get" and params.get("typeName") == "DeviceStatusInfo":
                return {
                    "result": [
                        {
                            "device": {"id": "device_A"},
                            "latitude": 32.7767,
                            "longitude": -96.7970,
                            "speed": 60.0,
                            "odometer": 50000.0,
                            "dateTime": "2026-02-15T12:00:00Z",
                        }
                    ]
                }
            return {"result": []}

        connector = GeotabConnector(
            tenant_id="tenant_abc",
            instance_id="inst_001",
            instance_config={"device_map": {"device_A": "truck_001"}},
            credentials_vault=mock_vault,
            credentials_ref="vault_ref_123",
            es_service=mock_es_service,
            sdk_call=sdk_call,
            clock=lambda: datetime(2026, 2, 15, 12, 0, 5, tzinfo=timezone.utc),
        )
        connector.set_ifta_reporter(mock_ifta_reporter, mock_boundary_detector)

        since = datetime(2026, 2, 15, 11, 0, 0, tzinfo=timezone.utc)
        run = await connector.sync_pull(since)

        assert run.status in ("success", "partial")
        # Boundary detector should have been called (first reading initializes state)
        mock_boundary_detector.get_state.assert_called()

    @pytest.mark.asyncio
    async def test_sync_pull_does_not_call_ifta_for_unmapped_devices(
        self, mock_vault, mock_es_service, mock_ifta_reporter, mock_boundary_detector
    ):
        """sync_pull skips IFTA processing for devices not in device_map."""
        def sdk_call(method, params, **kwargs):
            if method == "Get" and params.get("typeName") == "DeviceStatusInfo":
                return {
                    "result": [
                        {
                            "device": {"id": "unmapped_device"},
                            "latitude": 32.7767,
                            "longitude": -96.7970,
                            "speed": 60.0,
                            "odometer": 50000.0,
                            "dateTime": "2026-02-15T12:00:00Z",
                        }
                    ]
                }
            return {"result": []}

        connector = GeotabConnector(
            tenant_id="tenant_abc",
            instance_id="inst_001",
            instance_config={"device_map": {"device_A": "truck_001"}},
            credentials_vault=mock_vault,
            credentials_ref="vault_ref_123",
            es_service=mock_es_service,
            sdk_call=sdk_call,
            clock=lambda: datetime(2026, 2, 15, 12, 0, 5, tzinfo=timezone.utc),
        )
        connector.set_ifta_reporter(mock_ifta_reporter, mock_boundary_detector)

        since = datetime(2026, 2, 15, 11, 0, 0, tzinfo=timezone.utc)
        run = await connector.sync_pull(since)

        # Boundary detector should NOT be called for unmapped devices
        mock_boundary_detector.get_state.assert_not_called()
        mock_ifta_reporter.record_trip_segment.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_pull_ifta_error_does_not_abort_run(
        self, mock_vault, mock_es_service, mock_ifta_reporter, mock_boundary_detector
    ):
        """IFTA hook errors don't abort the sync_pull run."""
        # Make boundary detector raise an exception
        mock_boundary_detector.get_state.side_effect = RuntimeError("shapefile corrupt")

        def sdk_call(method, params, **kwargs):
            if method == "Get" and params.get("typeName") == "DeviceStatusInfo":
                return {
                    "result": [
                        {
                            "device": {"id": "device_A"},
                            "latitude": 32.7767,
                            "longitude": -96.7970,
                            "speed": 60.0,
                            "odometer": 50000.0,
                            "dateTime": "2026-02-15T12:00:00Z",
                        }
                    ]
                }
            return {"result": []}

        connector = GeotabConnector(
            tenant_id="tenant_abc",
            instance_id="inst_001",
            instance_config={"device_map": {"device_A": "truck_001"}},
            credentials_vault=mock_vault,
            credentials_ref="vault_ref_123",
            es_service=mock_es_service,
            sdk_call=sdk_call,
            clock=lambda: datetime(2026, 2, 15, 12, 0, 5, tzinfo=timezone.utc),
        )
        connector.set_ifta_reporter(mock_ifta_reporter, mock_boundary_detector)

        since = datetime(2026, 2, 15, 11, 0, 0, tzinfo=timezone.utc)
        run = await connector.sync_pull(since)

        # Run should still succeed — IFTA errors are non-fatal
        assert run.status in ("success", "partial")
        assert run.record_counts["readings_persisted"] >= 1

    @pytest.mark.asyncio
    async def test_boundary_crossing_during_sync_pull(
        self, mock_vault, mock_es_service, mock_ifta_reporter, mock_boundary_detector
    ):
        """Full integration: sync_pull detects crossing and records segment."""
        # Simulate: truck was in TX, now reading shows OK
        call_count = [0]

        def get_state_side_effect(lat, lon):
            call_count[0] += 1
            # Second call returns different state to simulate crossing
            if call_count[0] <= 1:
                return "TX"
            return "OK"

        mock_boundary_detector.get_state.side_effect = get_state_side_effect

        readings_returned = [False]

        def sdk_call(method, params, **kwargs):
            if method == "Get" and params.get("typeName") == "DeviceStatusInfo":
                if not readings_returned[0]:
                    readings_returned[0] = True
                    return {
                        "result": [
                            {
                                "device": {"id": "device_A"},
                                "latitude": 33.9,
                                "longitude": -96.5,
                                "speed": 60.0,
                                "odometer": 50000.0,
                                "dateTime": "2026-02-15T12:00:00Z",
                            },
                            {
                                "device": {"id": "device_A"},
                                "latitude": 34.1,
                                "longitude": -96.5,
                                "speed": 60.0,
                                "odometer": 50020.0,
                                "dateTime": "2026-02-15T12:05:00Z",
                            },
                        ]
                    }
                return {"result": []}
            return {"result": []}

        connector = GeotabConnector(
            tenant_id="tenant_abc",
            instance_id="inst_001",
            instance_config={"device_map": {"device_A": "truck_001"}},
            credentials_vault=mock_vault,
            credentials_ref="vault_ref_123",
            es_service=mock_es_service,
            sdk_call=sdk_call,
            clock=lambda: datetime(2026, 2, 15, 12, 0, 5, tzinfo=timezone.utc),
        )
        connector.set_ifta_reporter(mock_ifta_reporter, mock_boundary_detector)

        since = datetime(2026, 2, 15, 11, 0, 0, tzinfo=timezone.utc)
        run = await connector.sync_pull(since)

        assert run.status in ("success", "partial")

        # The first reading initializes state (TX), the second detects
        # crossing to OK and records a segment
        mock_ifta_reporter.record_trip_segment.assert_called_once()
        call_kwargs = mock_ifta_reporter.record_trip_segment.call_args[1]
        assert call_kwargs["from_state"] == "TX"
        assert call_kwargs["to_state"] == "OK"
        assert call_kwargs["truck_id"] == "truck_001"
        assert call_kwargs["tenant_id"] == "tenant_abc"
