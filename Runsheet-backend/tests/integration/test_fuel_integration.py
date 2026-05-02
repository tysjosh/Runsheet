"""
Integration tests for the Fuel Monitoring module.

Tests cover end-to-end flows through the API layer with mocked
Elasticsearch, verifying that the service layer correctly orchestrates
multi-step operations (consumption, refill, metrics, alerts).

Validates: Requirements 2.6, 3.5, 4.3, 5.1-5.5
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Patch elasticsearch_service BEFORE any fuel/ops imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from errors.exceptions import AppException
from errors.handlers import handle_app_exception
from fuel.api.endpoints import router, configure_fuel_api
from fuel.services.fuel_service import FuelService
from fuel.services.fuel_alert_service import FuelAlertService
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.elasticsearch_service import ElasticsearchService

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "test-tenant"
REQUEST_ID = "req-integ-fuel-001"

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

STATION_DOC = {
    "station_id": "STN-001",
    "name": "Industrial Depot",
    "fuel_type": "AGO",
    "capacity_liters": 50000.0,
    "current_stock_liters": 30000.0,
    "daily_consumption_rate": 1500.0,
    "days_until_empty": 20.0,
    "alert_threshold_pct": 20.0,
    "status": "normal",
    "location": None,
    "location_name": "Warehouse A",
    "tenant_id": TENANT_ID,
    "created_at": "2025-01-01T00:00:00Z",
    "last_updated": "2025-01-15T12:00:00Z",
}

LOW_STOCK_STATION_DOC = {
    **STATION_DOC,
    "current_stock_liters": 5000.0,
    "daily_consumption_rate": 1500.0,
    "days_until_empty": 3.3,
    "status": "low",
}


def _es_search_response(hits: list[dict], total: int | None = None) -> dict:
    """Build a mock ES search response."""
    return {
        "hits": {
            "hits": [{"_id": h.get("station_id", "doc-1"), "_source": h} for h in hits],
            "total": {"value": total if total is not None else len(hits)},
        }
    }


def _es_agg_response(
    total: int = 0,
    aggregations: dict | None = None,
) -> dict:
    """Build a mock ES search response with aggregations."""
    resp: dict = {
        "hits": {
            "hits": [],
            "total": {"value": total},
        },
    }
    if aggregations:
        resp["aggregations"] = aggregations
    return resp


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app():
    """Build a FastAPI test app with a real FuelService backed by a mocked ES."""
    app = FastAPI()

    mock_es = AsyncMock(spec=ElasticsearchService)
    fuel_service = FuelService(mock_es)

    configure_fuel_api(fuel_service=fuel_service)

    tenant_ctx = TenantContext(
        tenant_id=TENANT_ID, user_id="user-1", has_pii_access=False
    )

    async def _override_tenant():
        return tenant_ctx

    app.dependency_overrides[get_tenant_context] = _override_tenant

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = REQUEST_ID
            return await call_next(request)

    app.add_middleware(FakeRequestID)
    app.add_exception_handler(AppException, handle_app_exception)
    app.include_router(router)

    client = TestClient(app)
    return client, mock_es, fuel_service


# ===========================================================================
# TestFuelConsumptionFlow
# ===========================================================================


class TestFuelConsumptionFlow:
    """
    End-to-end consumption: create station → consume fuel → verify stock
    deducted → verify alert if below threshold.

    Validates: Requirements 2.1-2.6
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client, self.mock_es, self.fuel_service = _build_app()

    def test_create_station_then_consume(self):
        """Create a station, then record consumption and verify stock deducted."""
        # --- Step 1: Create station ---
        self.mock_es.index_document = AsyncMock(return_value=None)

        resp = self.client.post(
            "/api/fuel/stations",
            json={
                "station_id": "STN-001",
                "name": "Industrial Depot",
                "fuel_type": "AGO",
                "capacity_liters": 50000.0,
                "initial_stock_liters": 30000.0,
                "alert_threshold_pct": 20.0,
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["data"]["station_id"] == "STN-001"
        assert body["data"]["current_stock_liters"] == 30000.0
        assert body["data"]["status"] == "normal"

        # --- Step 2: Record consumption ---
        # The consumption flow calls:
        #   1. search_documents (find station)
        #   2. index_document (append event)
        #   3. search_documents (7-day window for rate calc)
        #   4. update_document (update station)
        self.mock_es.search_documents = AsyncMock(
            side_effect=[
                # 1. Find station
                _es_search_response([STATION_DOC]),
                # 3. Recent consumption events (7-day window)
                _es_search_response([
                    {
                        "event_id": "evt-1",
                        "quantity_liters": 1500.0,
                        "event_timestamp": "2025-01-14T12:00:00Z",
                    },
                ]),
            ]
        )
        self.mock_es.index_document = AsyncMock(return_value=None)
        self.mock_es.update_document = AsyncMock(return_value=None)

        resp = self.client.post(
            "/api/fuel/consumption",
            json={
                "station_id": "STN-001",
                "fuel_type": "AGO",
                "quantity_liters": 500.0,
                "asset_id": "TRUCK-001",
                "operator_id": "OP-001",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["station_id"] == "STN-001"
        # Stock should be 30000 - 500 = 29500
        assert data["new_stock_liters"] == 29500.0
        assert data["status"] == "normal"

        # Verify ES update was called with the deducted stock
        update_call = self.mock_es.update_document.call_args
        partial = update_call[0][2]  # third positional arg is the partial doc
        assert partial["current_stock_liters"] == 29500.0

    def test_consumption_triggers_alert_when_below_threshold(self):
        """Consuming fuel that drops stock below threshold sets status to low."""
        # Station with stock just above threshold (10500 / 50000 = 21%)
        station_near_threshold = {
            **STATION_DOC,
            "current_stock_liters": 10500.0,
            "daily_consumption_rate": 1500.0,
            "days_until_empty": 7.0,
        }

        self.mock_es.search_documents = AsyncMock(
            side_effect=[
                # Find station
                _es_search_response([station_near_threshold]),
                # Recent events for rate calc
                _es_search_response([
                    {"event_id": "e1", "quantity_liters": 1500.0, "event_timestamp": "2025-01-14T12:00:00Z"},
                ]),
            ]
        )
        self.mock_es.index_document = AsyncMock(return_value=None)
        self.mock_es.update_document = AsyncMock(return_value=None)

        # Consume 1500 liters → stock becomes 9000 / 50000 = 18% (below 20% threshold)
        resp = self.client.post(
            "/api/fuel/consumption",
            json={
                "station_id": "STN-001",
                "fuel_type": "AGO",
                "quantity_liters": 1500.0,
                "asset_id": "TRUCK-002",
                "operator_id": "OP-001",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["new_stock_liters"] == 9000.0
        assert data["status"] == "low"

    def test_consumption_rejected_when_insufficient_stock(self):
        """Attempting to consume more than available stock returns 400."""
        station_low = {**STATION_DOC, "current_stock_liters": 100.0}

        self.mock_es.search_documents = AsyncMock(
            return_value=_es_search_response([station_low])
        )

        resp = self.client.post(
            "/api/fuel/consumption",
            json={
                "station_id": "STN-001",
                "fuel_type": "AGO",
                "quantity_liters": 500.0,
                "asset_id": "TRUCK-001",
                "operator_id": "OP-001",
            },
        )
        assert resp.status_code == 400


# ===========================================================================
# TestFuelRefillFlow
# ===========================================================================


class TestFuelRefillFlow:
    """
    End-to-end refill: station with low stock → record refill → verify
    stock increased → verify alert cleared (status back to normal).

    Validates: Requirements 3.1-3.5
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client, self.mock_es, self.fuel_service = _build_app()

    def test_refill_restores_stock_and_clears_alert(self):
        """Refilling a low-stock station restores status to normal."""
        # The refill flow calls:
        #   1. search_documents (find station)
        #   2. index_document (append refill event)
        #   3. update_document (update station)
        self.mock_es.search_documents = AsyncMock(
            return_value=_es_search_response([LOW_STOCK_STATION_DOC])
        )
        self.mock_es.index_document = AsyncMock(return_value=None)
        self.mock_es.update_document = AsyncMock(return_value=None)

        # Refill 40000 liters → stock becomes 5000 + 40000 = 45000 / 50000 = 90%
        resp = self.client.post(
            "/api/fuel/refill",
            json={
                "station_id": "STN-001",
                "fuel_type": "AGO",
                "quantity_liters": 40000.0,
                "supplier": "FuelCorp",
                "operator_id": "OP-002",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["station_id"] == "STN-001"
        assert data["new_stock_liters"] == 45000.0
        assert data["status"] == "normal"

        # Verify ES update was called with restored stock
        update_call = self.mock_es.update_document.call_args
        partial = update_call[0][2]
        assert partial["current_stock_liters"] == 45000.0
        assert partial["status"] == "normal"

    def test_refill_overflow_rejected(self):
        """Refilling beyond capacity returns 400."""
        self.mock_es.search_documents = AsyncMock(
            return_value=_es_search_response([STATION_DOC])
        )

        # Station has 30000 stock, capacity 50000 → refill 25000 would overflow
        resp = self.client.post(
            "/api/fuel/refill",
            json={
                "station_id": "STN-001",
                "fuel_type": "AGO",
                "quantity_liters": 25000.0,
                "supplier": "FuelCorp",
                "operator_id": "OP-002",
            },
        )
        assert resp.status_code == 400

    def test_refill_keeps_low_status_when_still_below_threshold(self):
        """A small refill that doesn't cross the threshold keeps low status."""
        self.mock_es.search_documents = AsyncMock(
            return_value=_es_search_response([LOW_STOCK_STATION_DOC])
        )
        self.mock_es.index_document = AsyncMock(return_value=None)
        self.mock_es.update_document = AsyncMock(return_value=None)

        # Refill only 1000 liters → stock becomes 6000 / 50000 = 12% (still below 20%)
        resp = self.client.post(
            "/api/fuel/refill",
            json={
                "station_id": "STN-001",
                "fuel_type": "AGO",
                "quantity_liters": 1000.0,
                "supplier": "FuelCorp",
                "operator_id": "OP-002",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["new_stock_liters"] == 6000.0
        assert data["status"] == "low"


# ===========================================================================
# TestFuelMetricsIntegration
# ===========================================================================


class TestFuelMetricsIntegration:
    """
    Test metrics endpoints return correct data with seeded ES responses.

    Validates: Requirements 5.1-5.5
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client, self.mock_es, self.fuel_service = _build_app()

    def test_consumption_metrics_returns_bucketed_data(self):
        """GET /fuel/metrics/consumption returns time-bucketed aggregation."""
        self.mock_es.search_documents = AsyncMock(
            return_value=_es_agg_response(
                total=0,
                aggregations={
                    "consumption_over_time": {
                        "buckets": [
                            {
                                "key_as_string": "2025-01-13T00:00:00Z",
                                "doc_count": 5,
                                "total_liters": {"value": 7500.0},
                            },
                            {
                                "key_as_string": "2025-01-14T00:00:00Z",
                                "doc_count": 3,
                                "total_liters": {"value": 4500.0},
                            },
                        ]
                    }
                },
            )
        )

        resp = self.client.get("/api/fuel/metrics/consumption?bucket=daily")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 2
        assert data[0]["total_liters"] == 7500.0
        assert data[0]["event_count"] == 5
        assert data[1]["total_liters"] == 4500.0

    def test_consumption_metrics_with_filters(self):
        """Filters are passed through to the ES query."""
        self.mock_es.search_documents = AsyncMock(
            return_value=_es_agg_response(aggregations={"consumption_over_time": {"buckets": []}})
        )

        resp = self.client.get(
            "/api/fuel/metrics/consumption?bucket=hourly&station_id=STN-001&fuel_type=AGO"
        )
        assert resp.status_code == 200

        # Verify the ES query included the filters
        call_args = self.mock_es.search_documents.call_args
        query = call_args[0][1]  # second positional arg is the query
        filters = query["query"]["bool"]["must"]
        station_filter = [f for f in filters if f.get("term", {}).get("station_id")]
        fuel_filter = [f for f in filters if f.get("term", {}).get("fuel_type")]
        assert len(station_filter) == 1
        assert len(fuel_filter) == 1

    def test_efficiency_metrics_returns_per_asset_data(self):
        """GET /fuel/metrics/efficiency returns per-asset efficiency."""
        self.mock_es.search_documents = AsyncMock(
            return_value=_es_agg_response(
                aggregations={
                    "by_asset": {
                        "buckets": [
                            {
                                "key": "TRUCK-001",
                                "doc_count": 10,
                                "total_liters": {"value": 500.0},
                                "min_odometer": {"value": 10000.0},
                                "max_odometer": {"value": 12000.0},
                            },
                        ]
                    }
                },
            )
        )

        resp = self.client.get("/api/fuel/metrics/efficiency")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["asset_id"] == "TRUCK-001"
        assert data[0]["total_liters"] == 500.0
        # 500 liters / 2000 km = 0.25 l/km
        assert data[0]["liters_per_km"] == 0.25

    def test_network_summary_aggregates_all_stations(self):
        """GET /fuel/metrics/summary returns network-wide aggregation."""
        self.mock_es.search_documents = AsyncMock(
            return_value=_es_agg_response(
                total=5,
                aggregations={
                    "total_capacity": {"value": 250000.0},
                    "total_stock": {"value": 150000.0},
                    "total_daily_consumption": {"value": 7500.0},
                    "avg_days_until_empty": {"value": 20.0},
                    "by_status": {
                        "buckets": [
                            {"key": "normal", "doc_count": 3},
                            {"key": "low", "doc_count": 1},
                            {"key": "critical", "doc_count": 1},
                        ]
                    },
                },
            )
        )

        resp = self.client.get("/api/fuel/metrics/summary")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total_stations"] == 5
        assert data["total_capacity_liters"] == 250000.0
        assert data["total_current_stock_liters"] == 150000.0
        assert data["total_daily_consumption"] == 7500.0
        assert data["average_days_until_empty"] == 20.0
        assert data["stations_normal"] == 3
        assert data["stations_low"] == 1
        assert data["stations_critical"] == 1
        assert data["stations_empty"] == 0
        assert data["active_alerts"] == 2

    def test_alerts_endpoint_returns_active_alerts(self):
        """GET /fuel/alerts returns stations with non-normal status."""
        alert_station = {
            **STATION_DOC,
            "current_stock_liters": 8000.0,
            "status": "low",
            "days_until_empty": 5.3,
        }
        self.mock_es.search_documents = AsyncMock(
            return_value=_es_search_response([alert_station])
        )

        resp = self.client.get("/api/fuel/alerts")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["station_id"] == "STN-001"
        assert data[0]["status"] == "low"
        assert data[0]["stock_percentage"] == 16.0


# ===========================================================================
# TestFuelWebSocketAlerts
# ===========================================================================


class TestFuelWebSocketAlerts:
    """
    Test FuelAlertService broadcasts alerts correctly via WebSocket manager.

    Validates: Requirements 2.6, 4.3
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.mock_es = AsyncMock(spec=ElasticsearchService)
        self.mock_ws_manager = AsyncMock()
        self.mock_ws_manager.broadcast_fuel_alert = AsyncMock()
        self.alert_service = FuelAlertService(
            es_service=self.mock_es,
            ws_manager=self.mock_ws_manager,
        )

    @pytest.mark.asyncio
    async def test_broadcasts_alert_when_status_is_low(self):
        """Alert is broadcast when station status is low."""
        station_data = {
            "station_id": "STN-001",
            "name": "Industrial Depot",
            "fuel_type": "AGO",
            "status": "low",
            "current_stock_liters": 8000.0,
            "capacity_liters": 50000.0,
            "days_until_empty": 5.3,
            "location_name": "Warehouse A",
            "tenant_id": TENANT_ID,
        }

        await self.alert_service.check_thresholds(station_data)

        self.mock_ws_manager.broadcast_fuel_alert.assert_called_once()
        call_data = self.mock_ws_manager.broadcast_fuel_alert.call_args[0][0]
        assert call_data["station_id"] == "STN-001"
        assert call_data["status"] == "low"
        assert call_data["stock_percentage"] == 16.0
        assert call_data["tenant_id"] == TENANT_ID

    @pytest.mark.asyncio
    async def test_broadcasts_alert_when_status_is_critical(self):
        """Alert is broadcast when station status is critical."""
        station_data = {
            "station_id": "STN-002",
            "name": "Airport Depot",
            "fuel_type": "ATK",
            "status": "critical",
            "current_stock_liters": 2000.0,
            "capacity_liters": 100000.0,
            "days_until_empty": 1.5,
            "location_name": "Airport",
            "tenant_id": TENANT_ID,
        }

        await self.alert_service.check_thresholds(station_data)

        self.mock_ws_manager.broadcast_fuel_alert.assert_called_once()
        call_data = self.mock_ws_manager.broadcast_fuel_alert.call_args[0][0]
        assert call_data["status"] == "critical"
        assert call_data["days_until_empty"] == 1.5

    @pytest.mark.asyncio
    async def test_no_broadcast_when_status_is_normal(self):
        """No alert is broadcast when station status is normal."""
        station_data = {
            "station_id": "STN-001",
            "name": "Industrial Depot",
            "fuel_type": "AGO",
            "status": "normal",
            "current_stock_liters": 40000.0,
            "capacity_liters": 50000.0,
            "days_until_empty": 26.7,
            "tenant_id": TENANT_ID,
        }

        await self.alert_service.check_thresholds(station_data)

        self.mock_ws_manager.broadcast_fuel_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_includes_correct_stock_percentage(self):
        """Broadcast data includes correctly calculated stock percentage."""
        station_data = {
            "station_id": "STN-003",
            "name": "City Station",
            "fuel_type": "PMS",
            "status": "low",
            "current_stock_liters": 3000.0,
            "capacity_liters": 20000.0,
            "days_until_empty": 4.0,
            "tenant_id": TENANT_ID,
        }

        await self.alert_service.check_thresholds(station_data)

        call_data = self.mock_ws_manager.broadcast_fuel_alert.call_args[0][0]
        assert call_data["stock_percentage"] == 15.0

    @pytest.mark.asyncio
    async def test_broadcast_handles_ws_manager_failure_gracefully(self):
        """If WebSocket broadcast fails, no exception propagates."""
        self.mock_ws_manager.broadcast_fuel_alert = AsyncMock(
            side_effect=Exception("WebSocket connection lost")
        )

        station_data = {
            "station_id": "STN-001",
            "name": "Industrial Depot",
            "fuel_type": "AGO",
            "status": "low",
            "current_stock_liters": 8000.0,
            "capacity_liters": 50000.0,
            "days_until_empty": 5.3,
            "tenant_id": TENANT_ID,
        }

        # Should not raise
        await self.alert_service.check_thresholds(station_data)
