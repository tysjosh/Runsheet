"""
Unit tests for FuelService — station CRUD, alert threshold logic,
days_until_empty calculation, update_threshold, and get_alerts.

These tests complement the existing test files:
- test_fuel_consumption.py (record_consumption, record_consumption_batch)
- test_fuel_refill.py (record_refill, overflow, alert clearance)
- test_fuel_analytics.py (consumption metrics, efficiency, network summary)

Validates: Requirements 1.5, 2.4, 3.4, 4.2, 4.5, 4.6
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from errors.exceptions import AppException
from fuel.models import (
    CreateFuelStation,
    FuelStation,
    FuelStationDetail,
    GeoPoint,
    UpdateFuelStation,
)
from fuel.services.fuel_service import FuelService
from fuel.services.fuel_es_mappings import FUEL_STATIONS_INDEX, FUEL_EVENTS_INDEX


TENANT_ID = "tenant-001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _station_doc(
    station_id="STATION-001",
    fuel_type="AGO",
    current_stock=25000.0,
    capacity=50000.0,
    threshold_pct=20.0,
    daily_rate=500.0,
    days_until_empty=50.0,
    status="normal",
    name="Test Station",
    location_name="Test Location",
):
    return {
        "station_id": station_id,
        "name": name,
        "fuel_type": fuel_type,
        "capacity_liters": capacity,
        "current_stock_liters": current_stock,
        "daily_consumption_rate": daily_rate,
        "days_until_empty": days_until_empty,
        "alert_threshold_pct": threshold_pct,
        "status": status,
        "location": None,
        "location_name": location_name,
        "tenant_id": TENANT_ID,
        "created_at": "2024-01-01T00:00:00+00:00",
        "last_updated": "2024-01-01T00:00:00+00:00",
    }


def _es_search_response(docs, total=None):
    """Build an ES search response from a list of (doc_id, source) tuples."""
    hits = [{"_id": doc_id, "_source": src} for doc_id, src in docs]
    return {
        "hits": {
            "total": {"value": total if total is not None else len(docs)},
            "hits": hits,
        }
    }


def _mock_es_for_station(station_doc_val=None, events=None):
    """Create a mock ES service that returns a station and optional events."""
    es = MagicMock()

    if station_doc_val is not None:
        station_resp = _es_search_response([
            (f"{station_doc_val['station_id']}::{station_doc_val['fuel_type']}", station_doc_val)
        ])
    else:
        station_resp = _es_search_response([])

    events_resp = _es_search_response(
        [(f"evt-{i}", e) for i, e in enumerate(events or [])]
    )

    es.search_documents = AsyncMock(side_effect=[station_resp, events_resp])
    es.index_document = AsyncMock()
    es.update_document = AsyncMock()
    return es


@pytest.fixture
def settings_mock():
    with patch("fuel.services.fuel_service.get_settings") as mock:
        s = MagicMock()
        s.fuel_consumption_rolling_window_days = 7
        s.fuel_critical_days_threshold = 3
        s.fuel_alert_default_threshold_pct = 20.0
        mock.return_value = s
        yield s


# =========================================================================
# 1. Station CRUD operations
# =========================================================================


class TestCreateStation:
    """Tests for FuelService.create_station(). Validates: Req 1.3, 1.5"""

    @pytest.mark.asyncio
    async def test_creates_station_and_returns_model(self, settings_mock):
        """Req 1.3: Registers a new station and returns FuelStation."""
        es = MagicMock()
        es.index_document = AsyncMock()
        svc = FuelService(es)

        payload = CreateFuelStation(
            station_id="ST-NEW",
            name="New Station",
            fuel_type="AGO",
            capacity_liters=50000.0,
            initial_stock_liters=30000.0,
            alert_threshold_pct=20.0,
            location_name="Nairobi Depot",
        )
        result = await svc.create_station(payload, TENANT_ID)

        assert isinstance(result, FuelStation)
        assert result.station_id == "ST-NEW"
        assert result.current_stock_liters == 30000.0
        assert result.capacity_liters == 50000.0
        assert result.daily_consumption_rate == 0.0
        assert result.tenant_id == TENANT_ID

    @pytest.mark.asyncio
    async def test_indexes_document_with_correct_id(self, settings_mock):
        """Station doc is indexed with composite ID station_id::fuel_type."""
        es = MagicMock()
        es.index_document = AsyncMock()
        svc = FuelService(es)

        payload = CreateFuelStation(
            station_id="ST-001",
            name="Station",
            fuel_type="PMS",
            capacity_liters=10000.0,
            initial_stock_liters=5000.0,
        )
        await svc.create_station(payload, TENANT_ID)

        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args[0][0] == FUEL_STATIONS_INDEX
        assert call_args[0][1] == "ST-001::PMS"

    @pytest.mark.asyncio
    async def test_rejects_initial_stock_exceeding_capacity(self, settings_mock):
        """Req 1.5: initial_stock_liters > capacity_liters raises 400."""
        es = MagicMock()
        svc = FuelService(es)

        payload = CreateFuelStation(
            station_id="ST-BAD",
            name="Bad Station",
            fuel_type="AGO",
            capacity_liters=10000.0,
            initial_stock_liters=15000.0,
        )
        with pytest.raises(AppException) as exc_info:
            await svc.create_station(payload, TENANT_ID)

        assert exc_info.value.status_code == 400
        assert "capacity" in exc_info.value.message.lower()

    @pytest.mark.asyncio
    async def test_initial_stock_equal_to_capacity_allowed(self, settings_mock):
        """Edge case: initial_stock == capacity should succeed."""
        es = MagicMock()
        es.index_document = AsyncMock()
        svc = FuelService(es)

        payload = CreateFuelStation(
            station_id="ST-FULL",
            name="Full Station",
            fuel_type="AGO",
            capacity_liters=10000.0,
            initial_stock_liters=10000.0,
        )
        result = await svc.create_station(payload, TENANT_ID)

        assert result.current_stock_liters == 10000.0

    @pytest.mark.asyncio
    async def test_new_station_status_is_normal_when_stock_above_threshold(self, settings_mock):
        """New station with stock above threshold gets status 'normal'."""
        es = MagicMock()
        es.index_document = AsyncMock()
        svc = FuelService(es)

        payload = CreateFuelStation(
            station_id="ST-OK",
            name="OK Station",
            fuel_type="AGO",
            capacity_liters=10000.0,
            initial_stock_liters=5000.0,  # 50% > 20% threshold
        )
        result = await svc.create_station(payload, TENANT_ID)

        assert result.status == "normal"

    @pytest.mark.asyncio
    async def test_new_station_with_zero_stock_is_empty(self, settings_mock):
        """New station with 0 stock gets status 'empty'."""
        es = MagicMock()
        es.index_document = AsyncMock()
        svc = FuelService(es)

        payload = CreateFuelStation(
            station_id="ST-EMPTY",
            name="Empty Station",
            fuel_type="AGO",
            capacity_liters=10000.0,
            initial_stock_liters=0.0,
        )
        result = await svc.create_station(payload, TENANT_ID)

        assert result.status == "empty"


class TestListStations:
    """Tests for FuelService.list_stations(). Validates: Req 1.1, 1.6"""

    @pytest.mark.asyncio
    async def test_returns_paginated_response(self, settings_mock):
        """Req 1.1: Returns paginated list of stations."""
        doc = _station_doc()
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response(
            [("STATION-001::AGO", doc)], total=1
        ))
        svc = FuelService(es)

        result = await svc.list_stations(TENANT_ID)

        assert len(result.data) == 1
        assert result.pagination.total == 1
        assert result.data[0].station_id == "STATION-001"

    @pytest.mark.asyncio
    async def test_filters_by_fuel_type(self, settings_mock):
        """Req 1.6: Supports filtering by fuel_type."""
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response([], total=0))
        svc = FuelService(es)

        await svc.list_stations(TENANT_ID, fuel_type="PMS")

        query_body = es.search_documents.call_args[0][1]
        filters = query_body["query"]["bool"]["must"]
        fuel_filter = [f for f in filters if f.get("term", {}).get("fuel_type")]
        assert len(fuel_filter) == 1
        assert fuel_filter[0]["term"]["fuel_type"] == "PMS"

    @pytest.mark.asyncio
    async def test_filters_by_status(self, settings_mock):
        """Req 1.6: Supports filtering by status."""
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response([], total=0))
        svc = FuelService(es)

        await svc.list_stations(TENANT_ID, status="critical")

        query_body = es.search_documents.call_args[0][1]
        filters = query_body["query"]["bool"]["must"]
        status_filter = [f for f in filters if f.get("term", {}).get("status")]
        assert len(status_filter) == 1
        assert status_filter[0]["term"]["status"] == "critical"

    @pytest.mark.asyncio
    async def test_filters_by_location(self, settings_mock):
        """Req 1.6: Supports filtering by location text."""
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response([], total=0))
        svc = FuelService(es)

        await svc.list_stations(TENANT_ID, location="Nairobi")

        query_body = es.search_documents.call_args[0][1]
        filters = query_body["query"]["bool"]["must"]
        loc_filter = [f for f in filters if "multi_match" in f]
        assert len(loc_filter) == 1
        assert loc_filter[0]["multi_match"]["query"] == "Nairobi"

    @pytest.mark.asyncio
    async def test_pagination_offset(self, settings_mock):
        """Pagination uses correct from/size offsets."""
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response([], total=0))
        svc = FuelService(es)

        await svc.list_stations(TENANT_ID, page=3, size=10)

        query_body = es.search_documents.call_args[0][1]
        assert query_body["from"] == 20  # (3-1)*10
        assert query_body["size"] == 10


class TestGetStation:
    """Tests for FuelService.get_station(). Validates: Req 1.2"""

    @pytest.mark.asyncio
    async def test_returns_station_detail_with_events(self, settings_mock):
        """Req 1.2: Returns station with recent events."""
        doc = _station_doc()
        consumption_event = {
            "event_type": "consumption",
            "station_id": "STATION-001",
            "fuel_type": "AGO",
            "quantity_liters": 100.0,
            "asset_id": "TRUCK-001",
            "operator_id": "OP-001",
        }
        refill_event = {
            "event_type": "refill",
            "station_id": "STATION-001",
            "fuel_type": "AGO",
            "quantity_liters": 5000.0,
            "supplier": "FuelCorp",
            "operator_id": "OP-002",
        }
        es = MagicMock()
        es.search_documents = AsyncMock(side_effect=[
            _es_search_response([("STATION-001::AGO", doc)]),
            _es_search_response([
                ("evt-1", consumption_event),
                ("evt-2", refill_event),
            ]),
        ])
        svc = FuelService(es)

        result = await svc.get_station("STATION-001", TENANT_ID)

        assert isinstance(result, FuelStationDetail)
        assert result.station.station_id == "STATION-001"
        assert len(result.recent_consumption_events) == 1
        assert len(result.recent_refill_events) == 1

    @pytest.mark.asyncio
    async def test_station_not_found_raises_404(self, settings_mock):
        """Station not found raises resource_not_found (404)."""
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        svc = FuelService(es)

        with pytest.raises(AppException) as exc_info:
            await svc.get_station("NONEXISTENT", TENANT_ID)

        assert exc_info.value.status_code == 404


class TestUpdateStation:
    """Tests for FuelService.update_station(). Validates: Req 1.4"""

    @pytest.mark.asyncio
    async def test_updates_name(self, settings_mock):
        """Req 1.4: Updates station metadata fields."""
        doc = _station_doc()
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response(
            [("STATION-001::AGO", doc)]
        ))
        es.update_document = AsyncMock()
        svc = FuelService(es)

        update = UpdateFuelStation(name="Renamed Station")
        result = await svc.update_station("STATION-001", update, TENANT_ID)

        assert result.name == "Renamed Station"
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_op_update_returns_current(self, settings_mock):
        """Empty update returns current station without ES update."""
        doc = _station_doc()
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response(
            [("STATION-001::AGO", doc)]
        ))
        es.update_document = AsyncMock()
        svc = FuelService(es)

        update = UpdateFuelStation()  # all None
        result = await svc.update_station("STATION-001", update, TENANT_ID)

        assert result.station_id == "STATION-001"
        es.update_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_station_not_found(self, settings_mock):
        """Update on non-existent station raises 404."""
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        svc = FuelService(es)

        update = UpdateFuelStation(name="New Name")
        with pytest.raises(AppException) as exc_info:
            await svc.update_station("NONEXISTENT", update, TENANT_ID)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_capacity_change_recalculates_status(self, settings_mock):
        """Changing capacity triggers status recalculation."""
        # Stock 5000 / capacity 50000 = 10% → critical
        # After capacity change to 10000: 5000/10000 = 50% → normal
        doc = _station_doc(current_stock=5000.0, capacity=50000.0, status="critical")
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response(
            [("STATION-001::AGO", doc)]
        ))
        es.update_document = AsyncMock()
        svc = FuelService(es)

        update = UpdateFuelStation(capacity_liters=10000.0)
        result = await svc.update_station("STATION-001", update, TENANT_ID)

        assert result.status == "normal"


# =========================================================================
# 2. Alert threshold logic — _determine_status()
# =========================================================================


class TestDetermineStatus:
    """Tests for FuelService._determine_status(). Validates: Req 4.2, 4.6"""

    def _make_svc(self, settings_mock):
        es = MagicMock()
        return FuelService(es)

    def test_empty_when_stock_is_zero(self, settings_mock):
        """Req 4.2: Stock == 0 → 'empty'."""
        svc = self._make_svc(settings_mock)
        assert svc._determine_status(0.0, 50000.0, 20.0, float("inf")) == "empty"

    def test_empty_when_stock_is_negative(self, settings_mock):
        """Edge: Negative stock → 'empty'."""
        svc = self._make_svc(settings_mock)
        assert svc._determine_status(-1.0, 50000.0, 20.0, float("inf")) == "empty"

    def test_critical_when_below_10_percent(self, settings_mock):
        """Req 4.2: Stock < 10% of capacity → 'critical'."""
        svc = self._make_svc(settings_mock)
        # 4000/50000 = 8%
        assert svc._determine_status(4000.0, 50000.0, 20.0, 100.0) == "critical"

    def test_critical_when_days_until_empty_below_threshold(self, settings_mock):
        """Req 4.6: days_until_empty < 3 → 'critical' regardless of percentage."""
        svc = self._make_svc(settings_mock)
        # 15000/50000 = 30% (above threshold), but days_until_empty = 2
        assert svc._determine_status(15000.0, 50000.0, 20.0, 2.0) == "critical"

    def test_low_when_below_threshold_above_10_percent(self, settings_mock):
        """Req 4.2: Stock below threshold but above 10% → 'low'."""
        svc = self._make_svc(settings_mock)
        # 7500/50000 = 15% (below 20% threshold, above 10%)
        assert svc._determine_status(7500.0, 50000.0, 20.0, 100.0) == "low"

    def test_normal_when_above_threshold(self, settings_mock):
        """Req 4.2: Stock above threshold → 'normal'."""
        svc = self._make_svc(settings_mock)
        # 25000/50000 = 50%
        assert svc._determine_status(25000.0, 50000.0, 20.0, 100.0) == "normal"

    def test_exactly_at_10_percent_is_low_not_critical(self, settings_mock):
        """Boundary: stock exactly at 10% is 'low' (not critical, since < 10 triggers critical)."""
        svc = self._make_svc(settings_mock)
        # 5000/50000 = 10.0% — not < 10, so should be 'low' (below 20% threshold)
        assert svc._determine_status(5000.0, 50000.0, 20.0, 100.0) == "low"

    def test_exactly_at_threshold_is_normal(self, settings_mock):
        """Boundary: stock exactly at threshold% is 'normal' (not < threshold)."""
        svc = self._make_svc(settings_mock)
        # 10000/50000 = 20.0% — not < 20, so 'normal'
        assert svc._determine_status(10000.0, 50000.0, 20.0, 100.0) == "normal"

    def test_critical_days_overrides_normal_percentage(self, settings_mock):
        """Req 4.6: Even with 90% stock, days_until_empty < 3 → 'critical'."""
        svc = self._make_svc(settings_mock)
        assert svc._determine_status(45000.0, 50000.0, 20.0, 1.5) == "critical"


# =========================================================================
# 3. days_until_empty calculation
# =========================================================================


class TestCalculateDaysUntilEmpty:
    """Tests for FuelService._calculate_days_until_empty(). Validates: Req 4.5"""

    def _make_svc(self, settings_mock):
        es = MagicMock()
        return FuelService(es)

    def test_positive_rate(self, settings_mock):
        """Req 4.5: days = current_stock / daily_rate."""
        svc = self._make_svc(settings_mock)
        assert svc._calculate_days_until_empty(10000.0, 500.0) == 20.0

    def test_zero_rate_returns_infinity(self, settings_mock):
        """Zero consumption rate → infinite days."""
        svc = self._make_svc(settings_mock)
        assert svc._calculate_days_until_empty(10000.0, 0.0) == float("inf")

    def test_negative_rate_returns_infinity(self, settings_mock):
        """Negative rate (edge case) → infinite days."""
        svc = self._make_svc(settings_mock)
        assert svc._calculate_days_until_empty(10000.0, -5.0) == float("inf")

    def test_zero_stock_returns_zero(self, settings_mock):
        """Zero stock with positive rate → 0 days."""
        svc = self._make_svc(settings_mock)
        assert svc._calculate_days_until_empty(0.0, 500.0) == 0.0


class TestCalculateDailyRate:
    """Tests for FuelService._calculate_daily_rate(). Validates: Req 2.4"""

    def _make_svc(self, settings_mock):
        es = MagicMock()
        return FuelService(es)

    def test_calculates_average_over_window(self, settings_mock):
        """Daily rate = total_liters / window_days."""
        svc = self._make_svc(settings_mock)
        events = [
            {"quantity_liters": 100.0},
            {"quantity_liters": 200.0},
            {"quantity_liters": 400.0},
        ]
        # (100+200+400) / 7 = 100.0
        assert svc._calculate_daily_rate(events) == 100.0

    def test_empty_events_returns_zero(self, settings_mock):
        """No events → 0.0 daily rate."""
        svc = self._make_svc(settings_mock)
        assert svc._calculate_daily_rate([]) == 0.0

    def test_single_event(self, settings_mock):
        """Single event divided by window."""
        svc = self._make_svc(settings_mock)
        events = [{"quantity_liters": 350.0}]
        assert svc._calculate_daily_rate(events) == 50.0  # 350/7


# =========================================================================
# 4. update_threshold
# =========================================================================


class TestUpdateThreshold:
    """Tests for FuelService.update_threshold(). Validates: Req 4.4"""

    @pytest.mark.asyncio
    async def test_updates_threshold_and_recalculates_status(self, settings_mock):
        """Req 4.4: Threshold update triggers status recalculation."""
        # Stock 7500/50000 = 15%. With threshold 20% → low. With threshold 10% → normal.
        doc = _station_doc(current_stock=7500.0, capacity=50000.0, threshold_pct=20.0, status="low")
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response(
            [("STATION-001::AGO", doc)]
        ))
        es.update_document = AsyncMock()
        svc = FuelService(es)

        result = await svc.update_threshold("STATION-001", 10.0, TENANT_ID)

        assert result.alert_threshold_pct == 10.0
        assert result.status == "normal"
        es.update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejects_threshold_below_zero(self, settings_mock):
        """Threshold < 0 raises validation error."""
        es = MagicMock()
        svc = FuelService(es)

        with pytest.raises(AppException) as exc_info:
            await svc.update_threshold("STATION-001", -5.0, TENANT_ID)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_threshold_above_100(self, settings_mock):
        """Threshold > 100 raises validation error."""
        es = MagicMock()
        svc = FuelService(es)

        with pytest.raises(AppException) as exc_info:
            await svc.update_threshold("STATION-001", 150.0, TENANT_ID)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_station_not_found(self, settings_mock):
        """Threshold update on non-existent station raises 404."""
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        svc = FuelService(es)

        with pytest.raises(AppException) as exc_info:
            await svc.update_threshold("NONEXISTENT", 25.0, TENANT_ID)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_raising_threshold_can_trigger_low_status(self, settings_mock):
        """Raising threshold above current stock% changes status to low."""
        # Stock 25000/50000 = 50%. Threshold raised to 60% → low
        doc = _station_doc(current_stock=25000.0, capacity=50000.0, threshold_pct=20.0, status="normal")
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response(
            [("STATION-001::AGO", doc)]
        ))
        es.update_document = AsyncMock()
        svc = FuelService(es)

        result = await svc.update_threshold("STATION-001", 60.0, TENANT_ID)

        assert result.status == "low"


# =========================================================================
# 5. get_alerts — returns only non-normal stations
# =========================================================================


class TestGetAlerts:
    """Tests for FuelService.get_alerts(). Validates: Req 4.1, 4.5"""

    @pytest.mark.asyncio
    async def test_returns_non_normal_stations(self, settings_mock):
        """Req 4.1: Returns stations with status != normal."""
        low_doc = _station_doc(station_id="ST-LOW", status="low", current_stock=7500.0)
        critical_doc = _station_doc(station_id="ST-CRIT", status="critical", current_stock=2000.0)
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response([
            ("ST-LOW::AGO", low_doc),
            ("ST-CRIT::AGO", critical_doc),
        ]))
        svc = FuelService(es)

        alerts = await svc.get_alerts(TENANT_ID)

        assert len(alerts) == 2
        statuses = {a.status for a in alerts}
        assert statuses == {"low", "critical"}

    @pytest.mark.asyncio
    async def test_alert_includes_days_until_empty(self, settings_mock):
        """Req 4.5: Alert data includes days_until_empty."""
        doc = _station_doc(status="low", days_until_empty=5.0)
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response(
            [("STATION-001::AGO", doc)]
        ))
        svc = FuelService(es)

        alerts = await svc.get_alerts(TENANT_ID)

        assert len(alerts) == 1
        assert alerts[0].days_until_empty == 5.0

    @pytest.mark.asyncio
    async def test_alert_stock_percentage_calculated(self, settings_mock):
        """Alert includes calculated stock_percentage."""
        doc = _station_doc(current_stock=7500.0, capacity=50000.0, status="low")
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response(
            [("STATION-001::AGO", doc)]
        ))
        svc = FuelService(es)

        alerts = await svc.get_alerts(TENANT_ID)

        assert alerts[0].stock_percentage == 15.0

    @pytest.mark.asyncio
    async def test_empty_alerts_when_all_normal(self, settings_mock):
        """No alerts when all stations are normal."""
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response([], total=0))
        svc = FuelService(es)

        alerts = await svc.get_alerts(TENANT_ID)

        assert alerts == []

    @pytest.mark.asyncio
    async def test_queries_with_status_filter(self, settings_mock):
        """get_alerts queries ES for low/critical/empty statuses."""
        es = MagicMock()
        es.search_documents = AsyncMock(return_value=_es_search_response([]))
        svc = FuelService(es)

        await svc.get_alerts(TENANT_ID)

        query_body = es.search_documents.call_args[0][1]
        filters = query_body["query"]["bool"]["must"]
        terms_filter = [f for f in filters if "terms" in f]
        assert len(terms_filter) == 1
        assert set(terms_filter[0]["terms"]["status"]) == {"low", "critical", "empty"}
