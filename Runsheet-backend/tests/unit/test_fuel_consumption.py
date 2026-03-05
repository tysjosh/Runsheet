"""
Unit tests for FuelService consumption recording (record_consumption, record_consumption_batch).

Validates: Requirements 2.1-2.7
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from errors.exceptions import AppException
from fuel.models import ConsumptionEvent, ConsumptionResult, BatchResult
from fuel.services.fuel_service import FuelService
from fuel.services.fuel_es_mappings import FUEL_STATIONS_INDEX, FUEL_EVENTS_INDEX


TENANT_ID = "tenant-001"


def _make_station_doc(
    station_id="STATION-001",
    fuel_type="AGO",
    current_stock=10000.0,
    capacity=50000.0,
    threshold_pct=20.0,
    daily_rate=500.0,
    days_until_empty=20.0,
    status="normal",
):
    return {
        "station_id": station_id,
        "name": "Test Station",
        "fuel_type": fuel_type,
        "capacity_liters": capacity,
        "current_stock_liters": current_stock,
        "daily_consumption_rate": daily_rate,
        "days_until_empty": days_until_empty,
        "alert_threshold_pct": threshold_pct,
        "status": status,
        "location": None,
        "location_name": "Test Location",
        "tenant_id": TENANT_ID,
        "created_at": "2024-01-01T00:00:00+00:00",
        "last_updated": "2024-01-01T00:00:00+00:00",
    }


def _make_consumption_event(
    station_id="STATION-001",
    fuel_type="AGO",
    quantity=100.0,
    asset_id="TRUCK-001",
    operator_id="OP-001",
    odometer_reading=None,
):
    return ConsumptionEvent(
        station_id=station_id,
        fuel_type=fuel_type,
        quantity_liters=quantity,
        asset_id=asset_id,
        operator_id=operator_id,
        odometer_reading=odometer_reading,
    )


def _make_es_service(station_doc=None, recent_events=None):
    """Create a mock ElasticsearchService with configurable responses."""
    es = MagicMock()

    station_hit = (
        {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_id": f"{station_doc['station_id']}::{station_doc['fuel_type']}",
                        "_source": station_doc,
                    }
                ],
            }
        }
        if station_doc
        else {"hits": {"total": {"value": 0}, "hits": []}}
    )

    events_hit = {
        "hits": {
            "total": {"value": len(recent_events or [])},
            "hits": [{"_source": e} for e in (recent_events or [])],
        }
    }

    # First call returns station, second call returns events window
    es.search_documents = AsyncMock(side_effect=[station_hit, events_hit])
    es.index_document = AsyncMock()
    es.update_document = AsyncMock()
    return es


@pytest.fixture
def settings_mock():
    with patch("fuel.services.fuel_service.get_settings") as mock:
        s = MagicMock()
        s.fuel_consumption_rolling_window_days = 7
        s.fuel_critical_days_threshold = 3
        mock.return_value = s
        yield s


class TestRecordConsumption:
    """Tests for FuelService.record_consumption()."""

    @pytest.mark.asyncio
    async def test_deducts_stock_and_returns_result(self, settings_mock):
        """Validates: Req 2.1, 2.2 — stock is deducted and result returned."""
        station_doc = _make_station_doc(current_stock=10000.0)
        es = _make_es_service(station_doc=station_doc, recent_events=[])
        svc = FuelService(es)

        event = _make_consumption_event(quantity=200.0)
        result = await svc.record_consumption(event, TENANT_ID)

        assert isinstance(result, ConsumptionResult)
        assert result.station_id == "STATION-001"
        assert result.new_stock_liters == 9800.0
        assert result.event_id  # UUID generated

    @pytest.mark.asyncio
    async def test_appends_event_to_fuel_events_index(self, settings_mock):
        """Validates: Req 2.3 — consumption event appended to fuel_events."""
        station_doc = _make_station_doc(current_stock=5000.0)
        es = _make_es_service(station_doc=station_doc, recent_events=[])
        svc = FuelService(es)

        event = _make_consumption_event(quantity=100.0, odometer_reading=55000.0)
        result = await svc.record_consumption(event, TENANT_ID)

        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args[0][0] == FUEL_EVENTS_INDEX
        doc = call_args[0][2]
        assert doc["event_type"] == "consumption"
        assert doc["station_id"] == "STATION-001"
        assert doc["fuel_type"] == "AGO"
        assert doc["quantity_liters"] == 100.0
        assert doc["asset_id"] == "TRUCK-001"
        assert doc["operator_id"] == "OP-001"
        assert doc["odometer_reading"] == 55000.0
        assert doc["tenant_id"] == TENANT_ID
        assert doc["event_timestamp"]
        assert doc["ingested_at"]

    @pytest.mark.asyncio
    async def test_rejects_insufficient_stock(self, settings_mock):
        """Validates: Req 2.4 — 400 error when quantity > current stock."""
        station_doc = _make_station_doc(current_stock=50.0)
        es = _make_es_service(station_doc=station_doc)
        svc = FuelService(es)

        event = _make_consumption_event(quantity=100.0)
        with pytest.raises(AppException) as exc_info:
            await svc.record_consumption(event, TENANT_ID)

        assert exc_info.value.status_code == 400
        assert "Insufficient" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_recalculates_daily_rate(self, settings_mock):
        """Validates: Req 2.5 — daily rate recalculated from 7-day window."""
        station_doc = _make_station_doc(current_stock=10000.0)
        recent = [
            {"quantity_liters": 100.0},
            {"quantity_liters": 200.0},
            {"quantity_liters": 150.0},
        ]
        es = _make_es_service(station_doc=station_doc, recent_events=recent)
        svc = FuelService(es)

        event = _make_consumption_event(quantity=50.0)
        await svc.record_consumption(event, TENANT_ID)

        # Station update should include recalculated daily_rate
        update_call = es.update_document.call_args
        partial = update_call[0][2]
        # (100+200+150) / 7 = 64.28...
        assert abs(partial["daily_consumption_rate"] - 64.2857) < 0.1

    @pytest.mark.asyncio
    async def test_updates_station_status(self, settings_mock):
        """Validates: Req 2.5, 2.6 — status updated based on new stock level."""
        # Stock will drop to 100 out of 50000 = 0.2% → critical
        station_doc = _make_station_doc(current_stock=200.0, capacity=50000.0)
        es = _make_es_service(station_doc=station_doc, recent_events=[])
        svc = FuelService(es)

        event = _make_consumption_event(quantity=100.0)
        result = await svc.record_consumption(event, TENANT_ID)

        assert result.status == "critical"

    @pytest.mark.asyncio
    async def test_station_not_found_raises(self, settings_mock):
        """Station not found raises resource_not_found."""
        es = _make_es_service(station_doc=None)
        svc = FuelService(es)

        event = _make_consumption_event()
        with pytest.raises(AppException) as exc_info:
            await svc.record_consumption(event, TENANT_ID)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_updates_station_document_in_es(self, settings_mock):
        """Validates: Req 2.2 — station document updated with new stock, rate, status."""
        station_doc = _make_station_doc(current_stock=5000.0)
        es = _make_es_service(station_doc=station_doc, recent_events=[])
        svc = FuelService(es)

        event = _make_consumption_event(quantity=1000.0)
        await svc.record_consumption(event, TENANT_ID)

        es.update_document.assert_called_once()
        call_args = es.update_document.call_args
        assert call_args[0][0] == FUEL_STATIONS_INDEX
        partial = call_args[0][2]
        assert partial["current_stock_liters"] == 4000.0
        assert "daily_consumption_rate" in partial
        assert "days_until_empty" in partial
        assert "status" in partial
        assert "last_updated" in partial


class TestRecordConsumptionBatch:
    """Tests for FuelService.record_consumption_batch()."""

    @pytest.mark.asyncio
    async def test_processes_multiple_events(self, settings_mock):
        """Validates: Req 2.7 — batch processes multiple events."""
        station_doc = _make_station_doc(current_stock=10000.0)

        es = MagicMock()
        # Each call to record_consumption does 2 search calls
        station_hit = {
            "hits": {
                "total": {"value": 1},
                "hits": [{"_id": "STATION-001::AGO", "_source": station_doc}],
            }
        }
        events_hit = {"hits": {"total": {"value": 0}, "hits": []}}
        es.search_documents = AsyncMock(
            side_effect=[station_hit, events_hit, station_hit, events_hit]
        )
        es.index_document = AsyncMock()
        es.update_document = AsyncMock()

        svc = FuelService(es)
        events = [
            _make_consumption_event(quantity=100.0, asset_id="TRUCK-001"),
            _make_consumption_event(quantity=200.0, asset_id="TRUCK-002"),
        ]
        result = await svc.record_consumption_batch(events, TENANT_ID)

        assert isinstance(result, BatchResult)
        assert result.processed == 2
        assert result.failed == 0
        assert len(result.results) == 2

    @pytest.mark.asyncio
    async def test_batch_collects_errors(self, settings_mock):
        """Validates: Req 2.7 — batch collects errors for failed events."""
        # First event succeeds, second fails (insufficient stock)
        station_ok = _make_station_doc(current_stock=10000.0)
        station_low = _make_station_doc(current_stock=10.0)

        es = MagicMock()
        station_hit_ok = {
            "hits": {
                "total": {"value": 1},
                "hits": [{"_id": "STATION-001::AGO", "_source": station_ok}],
            }
        }
        station_hit_low = {
            "hits": {
                "total": {"value": 1},
                "hits": [{"_id": "STATION-001::AGO", "_source": station_low}],
            }
        }
        events_hit = {"hits": {"total": {"value": 0}, "hits": []}}
        es.search_documents = AsyncMock(
            side_effect=[station_hit_ok, events_hit, station_hit_low]
        )
        es.index_document = AsyncMock()
        es.update_document = AsyncMock()

        svc = FuelService(es)
        events = [
            _make_consumption_event(quantity=100.0),
            _make_consumption_event(quantity=500.0),  # exceeds 10L stock
        ]
        result = await svc.record_consumption_batch(events, TENANT_ID)

        assert result.processed == 1
        assert result.failed == 1
        assert len(result.errors) == 1
        assert "Insufficient" in result.errors[0]
