"""
Unit tests for the communication metrics REST endpoint.

Tests the GET /api/metrics/communications endpoint against a mocked
CommunicationMetricsService.

Validates: Requirements 13.5
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from fastapi import FastAPI

from notifications.api.metrics_endpoints import (
    router,
    configure_metrics_endpoints,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metrics_service_mock() -> MagicMock:
    """Return a mock CommunicationMetricsService."""
    svc = MagicMock()
    svc.get_all_metrics = AsyncMock(return_value={
        "ack_latency": {"buckets": [], "overall": {}},
        "notification_send_latency": {"by_channel": {}, "buckets": []},
        "driver_response_latency": {"buckets": [], "overall": {}},
        "failed_notification_rate": {"by_channel": {}, "buckets": []},
    })
    return svc


def _create_test_app(metrics_service, tenant_id: str = "tenant-1") -> FastAPI:
    """Create a minimal FastAPI app with the metrics router for testing."""
    app = FastAPI()
    configure_metrics_endpoints(metrics_service=metrics_service)
    app.include_router(router)

    # Override the tenant context dependency
    async def _override_tenant():
        return TenantContext(
            tenant_id=tenant_id,
            user_id="test-user",
            has_pii_access=False,
            roles=["admin"],
        )

    app.dependency_overrides[get_tenant_context] = _override_tenant
    return app


# ---------------------------------------------------------------------------
# GET /api/metrics/communications
# ---------------------------------------------------------------------------


class TestGetCommunicationMetrics:
    """Tests for GET /api/metrics/communications endpoint."""

    def test_returns_all_metrics(self):
        """GET /api/metrics/communications returns all four metric categories."""
        svc = _make_metrics_service_mock()
        app = _create_test_app(svc)

        with TestClient(app) as client:
            response = client.get("/api/metrics/communications")

        assert response.status_code == 200
        data = response.json()
        assert "ack_latency" in data
        assert "notification_send_latency" in data
        assert "driver_response_latency" in data
        assert "failed_notification_rate" in data

    def test_passes_date_range_params(self):
        """GET /api/metrics/communications passes start_date and end_date to service."""
        svc = _make_metrics_service_mock()
        app = _create_test_app(svc)

        with TestClient(app) as client:
            response = client.get(
                "/api/metrics/communications",
                params={
                    "start_date": "2025-01-01T00:00:00Z",
                    "end_date": "2025-01-31T23:59:59Z",
                },
            )

        assert response.status_code == 200
        svc.get_all_metrics.assert_called_once_with(
            tenant_id="tenant-1",
            start_date="2025-01-01T00:00:00Z",
            end_date="2025-01-31T23:59:59Z",
            interval="1d",
        )

    def test_passes_interval_param(self):
        """GET /api/metrics/communications passes interval parameter to service."""
        svc = _make_metrics_service_mock()
        app = _create_test_app(svc)

        with TestClient(app) as client:
            response = client.get(
                "/api/metrics/communications",
                params={"interval": "1h"},
            )

        assert response.status_code == 200
        svc.get_all_metrics.assert_called_once_with(
            tenant_id="tenant-1",
            start_date=None,
            end_date=None,
            interval="1h",
        )

    def test_defaults_interval_to_1d(self):
        """GET /api/metrics/communications defaults interval to '1d'."""
        svc = _make_metrics_service_mock()
        app = _create_test_app(svc)

        with TestClient(app) as client:
            response = client.get("/api/metrics/communications")

        assert response.status_code == 200
        svc.get_all_metrics.assert_called_once_with(
            tenant_id="tenant-1",
            start_date=None,
            end_date=None,
            interval="1d",
        )

    def test_returns_500_on_service_error(self):
        """GET /api/metrics/communications returns 500 on unexpected service error."""
        svc = _make_metrics_service_mock()
        svc.get_all_metrics = AsyncMock(side_effect=Exception("Unexpected error"))
        app = _create_test_app(svc)

        from errors.handlers import register_exception_handlers
        register_exception_handlers(app)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/metrics/communications")

        assert response.status_code == 500
