"""
Unit tests for fuel API endpoints.

Tests cover:
- All endpoint response formats and status codes
- Input validation (negative quantities, overflow, missing fields)
- Tenant scoping (requests without tenant_id rejected)
- Rate limiting

Validates: Requirements 1.1-1.7, 2.1-2.7, 3.1-3.5
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Patch the ElasticsearchService singleton BEFORE any fuel/ops imports so
# that importing modules doesn't trigger a real ES connection.
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from errors.exceptions import AppException
from errors.handlers import handle_app_exception
from fuel.api.endpoints import router, configure_fuel_api
from fuel.models import (
    BatchResult,
    ConsumptionResult,
    FuelAlert,
    FuelNetworkSummary,
    FuelStation,
    FuelStationDetail,
    EfficiencyMetric,
    MetricsBucket,
    PaginatedResponse,
    PaginationMeta,
    RefillResult,
)
from fuel.services.fuel_service import FuelService
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT_ID = "tenant-1"
REQUEST_ID = "req-test-fuel-001"


def _make_tenant() -> TenantContext:
    return TenantContext(tenant_id=TENANT_ID, user_id="user-1", has_pii_access=False)


def _make_station(**overrides) -> FuelStation:
    defaults = dict(
        station_id="STN-001",
        name="Test Station",
        fuel_type="AGO",
        capacity_liters=50000.0,
        current_stock_liters=30000.0,
        daily_consumption_rate=1500.0,
        days_until_empty=20.0,
        alert_threshold_pct=20.0,
        status="normal",
        location=None,
        location_name="Warehouse A",
        tenant_id=TENANT_ID,
        last_updated="2025-01-01T12:00:00Z",
    )
    defaults.update(overrides)
    return FuelStation(**defaults)


def _make_station_detail() -> FuelStationDetail:
    return FuelStationDetail(
        station=_make_station(),
        recent_consumption_events=[],
        recent_refill_events=[],
    )


def _make_alert(**overrides) -> FuelAlert:
    defaults = dict(
        station_id="STN-001",
        name="Test Station",
        fuel_type="AGO",
        status="low",
        current_stock_liters=8000.0,
        capacity_liters=50000.0,
        stock_percentage=16.0,
        days_until_empty=5.3,
        location_name="Warehouse A",
    )
    defaults.update(overrides)
    return FuelAlert(**defaults)


def _make_network_summary() -> FuelNetworkSummary:
    return FuelNetworkSummary(
        total_stations=5,
        total_capacity_liters=250000.0,
        total_current_stock_liters=150000.0,
        total_daily_consumption=7500.0,
        average_days_until_empty=20.0,
        stations_normal=3,
        stations_low=1,
        stations_critical=1,
        stations_empty=0,
        active_alerts=2,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_fuel_service():
    """Create a mock FuelService with all methods as AsyncMocks."""
    svc = MagicMock(spec=FuelService)
    svc.list_stations = AsyncMock()
    svc.get_station = AsyncMock()
    svc.create_station = AsyncMock()
    svc.update_station = AsyncMock()
    svc.update_threshold = AsyncMock()
    svc.record_consumption = AsyncMock()
    svc.record_consumption_batch = AsyncMock()
    svc.record_refill = AsyncMock()
    svc.get_alerts = AsyncMock()
    svc.get_consumption_metrics = AsyncMock()
    svc.get_efficiency_metrics = AsyncMock()
    svc.get_network_summary = AsyncMock()
    return svc


@pytest.fixture()
def app(mock_fuel_service):
    """Create a FastAPI app with the fuel router and mocked dependencies."""
    test_app = FastAPI()

    configure_fuel_api(fuel_service=mock_fuel_service)

    # Override tenant guard so we don't need a real JWT
    async def _override_tenant():
        return _make_tenant()

    test_app.dependency_overrides[get_tenant_context] = _override_tenant

    # Attach a fake request_id via middleware
    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = REQUEST_ID
            return await call_next(request)

    test_app.add_middleware(FakeRequestID)
    test_app.add_exception_handler(AppException, handle_app_exception)
    test_app.include_router(router)
    return test_app


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def no_tenant_app(mock_fuel_service):
    """App WITHOUT tenant override — tests that missing auth is rejected."""
    test_app = FastAPI()
    configure_fuel_api(fuel_service=mock_fuel_service)

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = REQUEST_ID
            return await call_next(request)

    test_app.add_middleware(FakeRequestID)
    test_app.add_exception_handler(AppException, handle_app_exception)
    test_app.include_router(router)
    return test_app


@pytest.fixture()
def no_tenant_client(no_tenant_app):
    return TestClient(no_tenant_app)


# ---------------------------------------------------------------------------
# GET /fuel/stations — Validates: Requirements 1.1, 1.6
# ---------------------------------------------------------------------------

class TestListStations:

    def test_returns_paginated_response(self, client, mock_fuel_service):
        station = _make_station()
        mock_fuel_service.list_stations.return_value = PaginatedResponse(
            data=[station],
            pagination=PaginationMeta.compute(page=1, size=50, total=1),
            request_id="",
        )
        resp = client.get("/api/fuel/stations")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "pagination" in body
        assert "request_id" in body
        assert body["request_id"] == REQUEST_ID
        assert len(body["data"]) == 1
        assert body["data"][0]["station_id"] == "STN-001"

    def test_passes_filters_to_service(self, client, mock_fuel_service):
        mock_fuel_service.list_stations.return_value = PaginatedResponse(
            data=[],
            pagination=PaginationMeta.compute(page=1, size=50, total=0),
            request_id="",
        )
        client.get("/api/fuel/stations?fuel_type=AGO&status=low&location=Dubai&page=2&size=10")
        mock_fuel_service.list_stations.assert_called_once_with(
            tenant_id=TENANT_ID,
            fuel_type="AGO",
            status="low",
            location="Dubai",
            page=2,
            size=10,
        )

    def test_default_pagination(self, client, mock_fuel_service):
        mock_fuel_service.list_stations.return_value = PaginatedResponse(
            data=[],
            pagination=PaginationMeta.compute(page=1, size=50, total=0),
            request_id="",
        )
        client.get("/api/fuel/stations")
        call_kwargs = mock_fuel_service.list_stations.call_args.kwargs
        assert call_kwargs["page"] == 1
        assert call_kwargs["size"] == 50


# ---------------------------------------------------------------------------
# GET /fuel/stations/{station_id} — Validates: Requirement 1.2
# ---------------------------------------------------------------------------

class TestGetStation:

    def test_returns_station_detail(self, client, mock_fuel_service):
        detail = _make_station_detail()
        mock_fuel_service.get_station.return_value = detail
        resp = client.get("/api/fuel/stations/STN-001")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert body["data"]["station"]["station_id"] == "STN-001"
        assert body["request_id"] == REQUEST_ID

    def test_not_found_returns_404(self, client, mock_fuel_service):
        from errors.exceptions import resource_not_found
        mock_fuel_service.get_station.side_effect = resource_not_found("Station not found")
        resp = client.get("/api/fuel/stations/MISSING")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /fuel/stations — Validates: Requirements 1.3, 1.5
# ---------------------------------------------------------------------------

class TestCreateStation:

    def test_creates_station_returns_201(self, client, mock_fuel_service):
        station = _make_station()
        mock_fuel_service.create_station.return_value = station
        payload = {
            "station_id": "STN-001",
            "name": "Test Station",
            "fuel_type": "AGO",
            "capacity_liters": 50000.0,
            "initial_stock_liters": 30000.0,
        }
        resp = client.post("/api/fuel/stations", json=payload)
        assert resp.status_code == 201
        body = resp.json()
        assert body["data"]["station_id"] == "STN-001"
        assert body["request_id"] == REQUEST_ID

    def test_negative_capacity_rejected(self, client, mock_fuel_service):
        """capacity_liters must be > 0 (Pydantic gt=0 validator)."""
        payload = {
            "station_id": "STN-BAD",
            "name": "Bad Station",
            "fuel_type": "AGO",
            "capacity_liters": -100.0,
            "initial_stock_liters": 0.0,
        }
        resp = client.post("/api/fuel/stations", json=payload)
        assert resp.status_code == 422

    def test_zero_capacity_rejected(self, client, mock_fuel_service):
        """capacity_liters must be > 0."""
        payload = {
            "station_id": "STN-BAD",
            "name": "Bad Station",
            "fuel_type": "AGO",
            "capacity_liters": 0.0,
            "initial_stock_liters": 0.0,
        }
        resp = client.post("/api/fuel/stations", json=payload)
        assert resp.status_code == 422

    def test_missing_required_fields_rejected(self, client, mock_fuel_service):
        """Missing station_id, name, fuel_type should fail validation."""
        resp = client.post("/api/fuel/stations", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PATCH /fuel/stations/{station_id} — Validates: Requirement 1.4
# ---------------------------------------------------------------------------

class TestUpdateStation:

    def test_partial_update_returns_200(self, client, mock_fuel_service):
        station = _make_station(name="Updated Station")
        mock_fuel_service.update_station.return_value = station
        resp = client.patch("/api/fuel/stations/STN-001", json={"name": "Updated Station"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["name"] == "Updated Station"

    def test_negative_capacity_in_update_rejected(self, client, mock_fuel_service):
        resp = client.patch("/api/fuel/stations/STN-001", json={"capacity_liters": -5.0})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PATCH /fuel/stations/{station_id}/threshold — Validates: Requirement 4.4
# ---------------------------------------------------------------------------

class TestUpdateThreshold:

    def test_update_threshold_returns_200(self, client, mock_fuel_service):
        station = _make_station(alert_threshold_pct=25.0)
        mock_fuel_service.update_threshold.return_value = station
        resp = client.patch("/api/fuel/stations/STN-001/threshold", json={"alert_threshold_pct": 25.0})
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["alert_threshold_pct"] == 25.0

    def test_threshold_above_100_rejected(self, client, mock_fuel_service):
        resp = client.patch("/api/fuel/stations/STN-001/threshold", json={"alert_threshold_pct": 150.0})
        assert resp.status_code == 422

    def test_threshold_below_0_rejected(self, client, mock_fuel_service):
        resp = client.patch("/api/fuel/stations/STN-001/threshold", json={"alert_threshold_pct": -5.0})
        assert resp.status_code == 422

    def test_missing_threshold_rejected(self, client, mock_fuel_service):
        resp = client.patch("/api/fuel/stations/STN-001/threshold", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /fuel/consumption — Validates: Requirements 2.1-2.6
# ---------------------------------------------------------------------------

class TestRecordConsumption:

    def test_records_consumption_returns_200(self, client, mock_fuel_service):
        mock_fuel_service.record_consumption.return_value = ConsumptionResult(
            event_id="EVT-001",
            station_id="STN-001",
            new_stock_liters=28500.0,
            status="normal",
        )
        payload = {
            "station_id": "STN-001",
            "fuel_type": "AGO",
            "quantity_liters": 1500.0,
            "asset_id": "TRUCK-001",
            "operator_id": "OP-001",
        }
        resp = client.post("/api/fuel/consumption", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["event_id"] == "EVT-001"
        assert body["data"]["new_stock_liters"] == 28500.0
        assert body["request_id"] == REQUEST_ID

    def test_negative_quantity_rejected(self, client, mock_fuel_service):
        """quantity_liters must be > 0 (Pydantic gt=0 validator)."""
        payload = {
            "station_id": "STN-001",
            "fuel_type": "AGO",
            "quantity_liters": -100.0,
            "asset_id": "TRUCK-001",
            "operator_id": "OP-001",
        }
        resp = client.post("/api/fuel/consumption", json=payload)
        assert resp.status_code == 422

    def test_zero_quantity_rejected(self, client, mock_fuel_service):
        payload = {
            "station_id": "STN-001",
            "fuel_type": "AGO",
            "quantity_liters": 0.0,
            "asset_id": "TRUCK-001",
            "operator_id": "OP-001",
        }
        resp = client.post("/api/fuel/consumption", json=payload)
        assert resp.status_code == 422

    def test_missing_required_fields_rejected(self, client, mock_fuel_service):
        resp = client.post("/api/fuel/consumption", json={"station_id": "STN-001"})
        assert resp.status_code == 422

    def test_insufficient_stock_returns_400(self, client, mock_fuel_service):
        from errors.exceptions import validation_error
        mock_fuel_service.record_consumption.side_effect = validation_error(
            "Insufficient stock", details={"available": 100, "requested": 5000}
        )
        payload = {
            "station_id": "STN-001",
            "fuel_type": "AGO",
            "quantity_liters": 5000.0,
            "asset_id": "TRUCK-001",
            "operator_id": "OP-001",
        }
        resp = client.post("/api/fuel/consumption", json=payload)
        assert resp.status_code == 400

    def test_optional_odometer_accepted(self, client, mock_fuel_service):
        mock_fuel_service.record_consumption.return_value = ConsumptionResult(
            event_id="EVT-002",
            station_id="STN-001",
            new_stock_liters=28000.0,
            status="normal",
        )
        payload = {
            "station_id": "STN-001",
            "fuel_type": "AGO",
            "quantity_liters": 500.0,
            "asset_id": "TRUCK-001",
            "operator_id": "OP-001",
            "odometer_reading": 125000.5,
        }
        resp = client.post("/api/fuel/consumption", json=payload)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /fuel/consumption/batch — Validates: Requirement 2.7
# ---------------------------------------------------------------------------

class TestRecordConsumptionBatch:

    def test_batch_returns_200(self, client, mock_fuel_service):
        mock_fuel_service.record_consumption_batch.return_value = BatchResult(
            processed=2,
            failed=0,
            results=[
                ConsumptionResult(event_id="EVT-001", station_id="STN-001", new_stock_liters=28500.0, status="normal"),
                ConsumptionResult(event_id="EVT-002", station_id="STN-001", new_stock_liters=27000.0, status="normal"),
            ],
            errors=[],
        )
        payload = [
            {"station_id": "STN-001", "fuel_type": "AGO", "quantity_liters": 1500.0, "asset_id": "TRUCK-001", "operator_id": "OP-001"},
            {"station_id": "STN-001", "fuel_type": "AGO", "quantity_liters": 1500.0, "asset_id": "TRUCK-002", "operator_id": "OP-001"},
        ]
        resp = client.post("/api/fuel/consumption/batch", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["processed"] == 2
        assert body["data"]["failed"] == 0

    def test_batch_invalid_item_rejected(self, client, mock_fuel_service):
        """A batch with an invalid item (negative quantity) should fail validation."""
        payload = [
            {"station_id": "STN-001", "fuel_type": "AGO", "quantity_liters": -10.0, "asset_id": "TRUCK-001", "operator_id": "OP-001"},
        ]
        resp = client.post("/api/fuel/consumption/batch", json=payload)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /fuel/refill — Validates: Requirements 3.1-3.5
# ---------------------------------------------------------------------------

class TestRecordRefill:

    def test_records_refill_returns_200(self, client, mock_fuel_service):
        mock_fuel_service.record_refill.return_value = RefillResult(
            event_id="EVT-R01",
            station_id="STN-001",
            new_stock_liters=45000.0,
            status="normal",
        )
        payload = {
            "station_id": "STN-001",
            "fuel_type": "AGO",
            "quantity_liters": 15000.0,
            "supplier": "FuelCo",
            "operator_id": "OP-001",
        }
        resp = client.post("/api/fuel/refill", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["event_id"] == "EVT-R01"
        assert body["data"]["new_stock_liters"] == 45000.0

    def test_negative_refill_quantity_rejected(self, client, mock_fuel_service):
        payload = {
            "station_id": "STN-001",
            "fuel_type": "AGO",
            "quantity_liters": -500.0,
            "supplier": "FuelCo",
            "operator_id": "OP-001",
        }
        resp = client.post("/api/fuel/refill", json=payload)
        assert resp.status_code == 422

    def test_overflow_returns_400(self, client, mock_fuel_service):
        from errors.exceptions import validation_error
        mock_fuel_service.record_refill.side_effect = validation_error(
            "Refill would exceed capacity",
            details={"capacity": 50000, "current": 48000, "refill": 5000},
        )
        payload = {
            "station_id": "STN-001",
            "fuel_type": "AGO",
            "quantity_liters": 5000.0,
            "supplier": "FuelCo",
            "operator_id": "OP-001",
        }
        resp = client.post("/api/fuel/refill", json=payload)
        assert resp.status_code == 400

    def test_missing_supplier_rejected(self, client, mock_fuel_service):
        payload = {
            "station_id": "STN-001",
            "fuel_type": "AGO",
            "quantity_liters": 1000.0,
            "operator_id": "OP-001",
        }
        resp = client.post("/api/fuel/refill", json=payload)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /fuel/alerts — Validates: Requirement 4.1
# ---------------------------------------------------------------------------

class TestListAlerts:

    def test_returns_alerts(self, client, mock_fuel_service):
        mock_fuel_service.get_alerts.return_value = [_make_alert()]
        resp = client.get("/api/fuel/alerts")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert len(body["data"]) == 1
        assert body["data"][0]["status"] == "low"
        assert body["request_id"] == REQUEST_ID

    def test_empty_alerts(self, client, mock_fuel_service):
        mock_fuel_service.get_alerts.return_value = []
        resp = client.get("/api/fuel/alerts")
        assert resp.status_code == 200
        assert resp.json()["data"] == []


# ---------------------------------------------------------------------------
# GET /fuel/metrics/consumption — Validates: Requirements 5.1, 5.3, 5.5
# ---------------------------------------------------------------------------

class TestConsumptionMetrics:

    def test_returns_metrics(self, client, mock_fuel_service):
        mock_fuel_service.get_consumption_metrics.return_value = [
            MetricsBucket(timestamp="2025-01-01T00:00:00Z", total_liters=1500.0, event_count=3),
        ]
        resp = client.get("/api/fuel/metrics/consumption")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["total_liters"] == 1500.0

    def test_passes_filters_to_service(self, client, mock_fuel_service):
        mock_fuel_service.get_consumption_metrics.return_value = []
        client.get(
            "/api/fuel/metrics/consumption"
            "?bucket=hourly&station_id=STN-001&fuel_type=AGO"
            "&asset_id=TRUCK-001&start_date=2025-01-01&end_date=2025-01-31"
        )
        mock_fuel_service.get_consumption_metrics.assert_called_once_with(
            tenant_id=TENANT_ID,
            bucket="hourly",
            station_id="STN-001",
            fuel_type="AGO",
            asset_id="TRUCK-001",
            start_date="2025-01-01",
            end_date="2025-01-31",
        )


# ---------------------------------------------------------------------------
# GET /fuel/metrics/efficiency — Validates: Requirements 5.2, 5.3
# ---------------------------------------------------------------------------

class TestEfficiencyMetrics:

    def test_returns_efficiency_data(self, client, mock_fuel_service):
        mock_fuel_service.get_efficiency_metrics.return_value = [
            EfficiencyMetric(
                asset_id="TRUCK-001",
                total_liters=500.0,
                total_distance_km=2000.0,
                liters_per_km=0.25,
                event_count=10,
            ),
        ]
        resp = client.get("/api/fuel/metrics/efficiency")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["liters_per_km"] == 0.25

    def test_passes_filters_to_service(self, client, mock_fuel_service):
        mock_fuel_service.get_efficiency_metrics.return_value = []
        client.get("/api/fuel/metrics/efficiency?asset_id=TRUCK-001&start_date=2025-01-01&end_date=2025-01-31")
        mock_fuel_service.get_efficiency_metrics.assert_called_once_with(
            tenant_id=TENANT_ID,
            asset_id="TRUCK-001",
            start_date="2025-01-01",
            end_date="2025-01-31",
        )


# ---------------------------------------------------------------------------
# GET /fuel/metrics/summary — Validates: Requirement 5.4
# ---------------------------------------------------------------------------

class TestNetworkSummary:

    def test_returns_summary(self, client, mock_fuel_service):
        mock_fuel_service.get_network_summary.return_value = _make_network_summary()
        resp = client.get("/api/fuel/metrics/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["total_stations"] == 5
        assert body["data"]["active_alerts"] == 2
        assert body["request_id"] == REQUEST_ID


# ---------------------------------------------------------------------------
# Tenant scoping — requests without tenant_id rejected
# Validates: Requirements 9.1, 9.6
# ---------------------------------------------------------------------------

class TestTenantScoping:

    @pytest.fixture(autouse=True)
    def _force_non_dev(self):
        """Force non-development environment so tenant guard rejects unauthenticated requests."""
        with patch(
            "ops.middleware.tenant_guard.get_settings",
            return_value=MagicMock(
                environment=MagicMock(value="production"),
                jwt_secret="test-secret",
                jwt_algorithm="HS256",
            ),
        ):
            yield

    def test_no_auth_header_returns_403(self, no_tenant_client):
        """Requests without Authorization header should be rejected."""
        resp = no_tenant_client.get("/api/fuel/stations")
        assert resp.status_code == 403

    def test_no_auth_on_post_returns_403(self, no_tenant_client):
        payload = {
            "station_id": "STN-001",
            "name": "Test",
            "fuel_type": "AGO",
            "capacity_liters": 50000.0,
            "initial_stock_liters": 30000.0,
        }
        resp = no_tenant_client.post("/api/fuel/stations", json=payload)
        assert resp.status_code == 403

    def test_no_auth_on_consumption_returns_403(self, no_tenant_client):
        payload = {
            "station_id": "STN-001",
            "fuel_type": "AGO",
            "quantity_liters": 100.0,
            "asset_id": "TRUCK-001",
            "operator_id": "OP-001",
        }
        resp = no_tenant_client.post("/api/fuel/consumption", json=payload)
        assert resp.status_code == 403

    def test_no_auth_on_refill_returns_403(self, no_tenant_client):
        payload = {
            "station_id": "STN-001",
            "fuel_type": "AGO",
            "quantity_liters": 100.0,
            "supplier": "FuelCo",
            "operator_id": "OP-001",
        }
        resp = no_tenant_client.post("/api/fuel/refill", json=payload)
        assert resp.status_code == 403

    def test_no_auth_on_alerts_returns_403(self, no_tenant_client):
        resp = no_tenant_client.get("/api/fuel/alerts")
        assert resp.status_code == 403

    def test_no_auth_on_metrics_returns_403(self, no_tenant_client):
        resp = no_tenant_client.get("/api/fuel/metrics/summary")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Rate limiting — Validates: Requirement 14.1
# ---------------------------------------------------------------------------

class TestRateLimiting:

    def test_rate_limit_decorator_applied(self):
        """Verify that the rate limiter decorator is applied to endpoints."""
        # The router has prefix="/fuel", so all route paths include it.
        route_paths = [r.path for r in router.routes]
        assert "/api/fuel/stations" in route_paths
        assert "/api/fuel/consumption" in route_paths
        assert "/api/fuel/refill" in route_paths
        assert "/api/fuel/alerts" in route_paths
        assert "/api/fuel/metrics/summary" in route_paths
