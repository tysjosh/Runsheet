"""
Unit tests for FuelService.record_refill().

Validates: Requirements 3.1-3.5
- 3.1: POST /fuel/refill records a fuel delivery event
- 3.2: Adds quantity_liters to current_stock_liters
- 3.3: Appends refill event to fuel_events index with event_type "refill"
- 3.4: Rejects with 400 if refill would exceed capacity
- 3.5: Clears active alerts when stock restored above threshold
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fuel.models import RefillEvent
from fuel.services.fuel_service import FuelService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT = "tenant-001"


def _make_refill(**overrides) -> RefillEvent:
    """Build a minimal RefillEvent with sensible defaults."""
    data = {
        "station_id": "STATION_001",
        "fuel_type": "AGO",
        "quantity_liters": 5000.0,
        "supplier": "FuelCorp",
        "delivery_reference": "DEL-001",
        "operator_id": "OP-001",
    }
    data.update(overrides)
    return RefillEvent(**data)


def _station_doc(current_stock: float = 10000.0, capacity: float = 50000.0,
                 threshold_pct: float = 20.0, status: str = "low",
                 daily_rate: float = 500.0):
    """Build a station ES document."""
    return {
        "station_id": "STATION_001",
        "name": "Test Station",
        "fuel_type": "AGO",
        "capacity_liters": capacity,
        "current_stock_liters": current_stock,
        "daily_consumption_rate": daily_rate,
        "days_until_empty": current_stock / daily_rate if daily_rate > 0 else float("inf"),
        "alert_threshold_pct": threshold_pct,
        "status": status,
        "tenant_id": TENANT,
        "last_updated": "2025-01-01T00:00:00+00:00",
    }


def _mock_es(station_doc=None):
    """Create a mock ES service returning the given station document."""
    es = MagicMock()
    if station_doc is not None:
        es.search_documents = AsyncMock(return_value={
            "hits": {"hits": [{"_id": "STATION_001::AGO", "_source": station_doc}]}
        })
    else:
        es.search_documents = AsyncMock(return_value={
            "hits": {"hits": []}
        })
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    return es


# ---------------------------------------------------------------------------
# Tests: Successful refill recording (Req 3.1, 3.2, 3.3)
# ---------------------------------------------------------------------------


class TestRecordRefillSuccess:
    """Validates that a valid refill adds stock and creates an event."""

    async def test_adds_quantity_to_stock(self):
        """Req 3.2: quantity_liters is added to current_stock_liters."""
        station = _station_doc(current_stock=10000.0, capacity=50000.0)
        es = _mock_es(station)
        svc = FuelService(es)

        result = await svc.record_refill(_make_refill(quantity_liters=5000.0), TENANT)

        assert result.new_stock_liters == 15000.0

    async def test_returns_refill_result_fields(self):
        """Req 3.1: Returns event_id, station_id, new_stock_liters, status."""
        station = _station_doc(current_stock=10000.0)
        es = _mock_es(station)
        svc = FuelService(es)

        result = await svc.record_refill(_make_refill(), TENANT)

        assert result.event_id  # non-empty UUID
        assert result.station_id == "STATION_001"
        assert result.new_stock_liters == 15000.0
        assert result.status in ("normal", "low", "critical", "empty")

    async def test_appends_refill_event_to_events_index(self):
        """Req 3.3: Appends event doc with event_type='refill' to fuel_events."""
        station = _station_doc(current_stock=10000.0)
        es = _mock_es(station)
        svc = FuelService(es)

        await svc.record_refill(_make_refill(supplier="FuelCorp", delivery_reference="DEL-001"), TENANT)

        # index_document should be called for the event
        call_args = es.index_document.call_args
        event_doc = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get("document", call_args[0][2])
        assert event_doc["event_type"] == "refill"
        assert event_doc["supplier"] == "FuelCorp"
        assert event_doc["delivery_reference"] == "DEL-001"
        assert event_doc["station_id"] == "STATION_001"
        assert event_doc["tenant_id"] == TENANT

    async def test_updates_station_document(self):
        """Req 3.2: Station document is updated with new stock and status."""
        station = _station_doc(current_stock=10000.0)
        es = _mock_es(station)
        svc = FuelService(es)

        await svc.record_refill(_make_refill(quantity_liters=5000.0), TENANT)

        es.update_document.assert_called_once()
        update_args = es.update_document.call_args
        partial = update_args[0][2] if len(update_args[0]) > 2 else update_args[1].get("document", update_args[0][2])
        assert partial["current_stock_liters"] == 15000.0
        assert "status" in partial
        assert "last_updated" in partial


# ---------------------------------------------------------------------------
# Tests: Overflow rejection (Req 3.4)
# ---------------------------------------------------------------------------


class TestRecordRefillOverflow:
    """Validates that refills exceeding capacity are rejected."""

    async def test_rejects_overflow(self):
        """Req 3.4: Rejects with error if stock + quantity > capacity."""
        station = _station_doc(current_stock=45000.0, capacity=50000.0)
        es = _mock_es(station)
        svc = FuelService(es)

        with pytest.raises(Exception) as exc_info:
            await svc.record_refill(_make_refill(quantity_liters=10000.0), TENANT)

        # Should mention overflow / capacity
        assert "capacity" in str(exc_info.value).lower() or "exceed" in str(exc_info.value).lower()

    async def test_exact_capacity_is_allowed(self):
        """Edge case: refill that brings stock exactly to capacity should succeed."""
        station = _station_doc(current_stock=45000.0, capacity=50000.0)
        es = _mock_es(station)
        svc = FuelService(es)

        result = await svc.record_refill(_make_refill(quantity_liters=5000.0), TENANT)

        assert result.new_stock_liters == 50000.0


# ---------------------------------------------------------------------------
# Tests: Station not found
# ---------------------------------------------------------------------------


class TestRecordRefillNotFound:
    """Validates that refill for non-existent station raises error."""

    async def test_station_not_found(self):
        es = _mock_es(station_doc=None)
        svc = FuelService(es)

        with pytest.raises(Exception) as exc_info:
            await svc.record_refill(_make_refill(), TENANT)

        assert "not found" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Tests: Alert clearance (Req 3.5)
# ---------------------------------------------------------------------------


class TestRecordRefillAlertClearance:
    """Validates that refill restoring stock above threshold clears alerts."""

    async def test_status_becomes_normal_when_stock_above_threshold(self):
        """Req 3.5: Refill restoring stock above threshold sets status to normal."""
        # Station at 5000/50000 = 10% (critical), threshold 20%
        station = _station_doc(current_stock=5000.0, capacity=50000.0,
                               threshold_pct=20.0, status="critical", daily_rate=500.0)
        es = _mock_es(station)
        svc = FuelService(es)

        # Refill 20000 liters -> 25000/50000 = 50% (above 20% threshold)
        result = await svc.record_refill(_make_refill(quantity_liters=20000.0), TENANT)

        assert result.status == "normal"

    async def test_status_stays_low_when_still_below_threshold(self):
        """Refill that doesn't restore above threshold keeps low/critical status."""
        # Station at 2000/50000 = 4% (critical), threshold 20%
        station = _station_doc(current_stock=2000.0, capacity=50000.0,
                               threshold_pct=20.0, status="critical", daily_rate=500.0)
        es = _mock_es(station)
        svc = FuelService(es)

        # Refill 3000 liters -> 5000/50000 = 10% (still below 20% threshold)
        result = await svc.record_refill(_make_refill(quantity_liters=3000.0), TENANT)

        assert result.status in ("low", "critical")
