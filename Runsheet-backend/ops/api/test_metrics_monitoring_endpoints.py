"""
Tests for aggregated metrics endpoints (Req 11.1-11.6) and
monitoring endpoints (Req 23.1-23.3) in ops/api/endpoints.py.
"""

import math
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Prevent the real ElasticsearchService from connecting on import
_mock_es_module = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from ops.api.endpoints import configure_ops_api, router  # noqa: E402
from ops.middleware.tenant_guard import TenantContext  # noqa: E402
from ops.services.ops_es_service import OpsElasticsearchService  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _tenant_ctx() -> TenantContext:
    return TenantContext(tenant_id="t1", user_id="u1", has_pii_access=False)


@pytest.fixture()
def mock_es():
    """Return a mock OpsElasticsearchService with an async client.search."""
    svc = MagicMock(spec=OpsElasticsearchService)
    svc.client = MagicMock()
    svc.client.search = AsyncMock()
    svc.SHIPMENTS_CURRENT = OpsElasticsearchService.SHIPMENTS_CURRENT
    svc.SHIPMENT_EVENTS = OpsElasticsearchService.SHIPMENT_EVENTS
    svc.RIDERS_CURRENT = OpsElasticsearchService.RIDERS_CURRENT
    svc.POISON_QUEUE = OpsElasticsearchService.POISON_QUEUE
    return svc


@pytest.fixture()
def client(mock_es):
    app = _make_app()
    configure_ops_api(ops_es_service=mock_es)

    # Override tenant guard dependency
    from ops.middleware.tenant_guard import get_tenant_context
    app.dependency_overrides[get_tenant_context] = _tenant_ctx

    # Inject a fake request_id via middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = "test-req-id"
            return await call_next(request)

    app.add_middleware(FakeRequestID)

    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _es_agg_response(agg_buckets: list, total: int = 0) -> dict:
    """Build a minimal ES search response with aggregations."""
    return {
        "hits": {"total": {"value": total}, "hits": []},
        "aggregations": {"over_time": {"buckets": agg_buckets}},
    }


# ===========================================================================
# Task 6.5 — Aggregated Metrics Endpoints
# ===========================================================================


class TestShipmentMetrics:
    """GET /ops/metrics/shipments — Req 11.1"""

    def test_returns_bucketed_status_counts(self, client, mock_es):
        mock_es.client.search.return_value = _es_agg_response([
            {
                "key_as_string": "2024-01-01T00:00:00.000Z",
                "doc_count": 10,
                "by_status": {
                    "buckets": [
                        {"key": "delivered", "doc_count": 6},
                        {"key": "in_transit", "doc_count": 4},
                    ]
                },
            }
        ])

        resp = client.get("/ops/metrics/shipments")
        assert resp.status_code == 200
        body = resp.json()
        assert body["bucket"] == "hourly"
        assert body["request_id"] == "test-req-id"
        assert len(body["data"]) == 1
        assert body["data"][0]["values"]["delivered"] == 6
        assert body["data"][0]["values"]["total"] == 10

    def test_invalid_bucket_returns_400(self, client, mock_es):
        resp = client.get("/ops/metrics/shipments?bucket=weekly")
        assert resp.status_code == 400

    def test_enforces_daily_for_large_range(self, client, mock_es):
        """Req 11.5 — ranges > 90 days force daily bucket."""
        mock_es.client.search.return_value = _es_agg_response([])

        start = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = client.get(f"/ops/metrics/shipments?bucket=hourly&start_date={start}&end_date={end}")
        assert resp.status_code == 200
        assert resp.json()["bucket"] == "daily"

    def test_respects_hourly_for_short_range(self, client, mock_es):
        mock_es.client.search.return_value = _es_agg_response([])

        start = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = client.get(f"/ops/metrics/shipments?bucket=hourly&start_date={start}&end_date={end}")
        assert resp.status_code == 200
        assert resp.json()["bucket"] == "hourly"


class TestSLAMetrics:
    """GET /ops/metrics/sla — Req 11.2"""

    def test_returns_compliance_data(self, client, mock_es):
        mock_es.client.search.return_value = _es_agg_response([
            {
                "key_as_string": "2024-01-01T00:00:00.000Z",
                "doc_count": 20,
                "sla_breached": {"doc_count": 5},
            }
        ])

        resp = client.get("/ops/metrics/sla")
        assert resp.status_code == 200
        body = resp.json()
        bucket_data = body["data"][0]["values"]
        assert bucket_data["total"] == 20
        assert bucket_data["breached"] == 5
        assert bucket_data["compliant"] == 15
        assert bucket_data["compliance_pct"] == 75.0


class TestRiderMetrics:
    """GET /ops/metrics/riders — Req 11.3"""

    def test_returns_rider_utilization_buckets(self, client, mock_es):
        mock_es.client.search.return_value = _es_agg_response([
            {
                "key_as_string": "2024-01-01T00:00:00.000Z",
                "doc_count": 8,
                "by_status": {
                    "buckets": [
                        {"key": "active", "doc_count": 5},
                        {"key": "idle", "doc_count": 3},
                    ]
                },
                "avg_active_shipments": {"value": 2.5},
                "avg_completed_today": {"value": 7.0},
            }
        ])

        resp = client.get("/ops/metrics/riders")
        assert resp.status_code == 200
        body = resp.json()
        vals = body["data"][0]["values"]
        assert vals["total_riders"] == 8
        assert vals["status_active"] == 5
        assert vals["avg_active_shipments"] == 2.5


class TestFailureMetrics:
    """GET /ops/metrics/failures — Req 11.4"""

    def test_returns_failure_counts_by_reason(self, client, mock_es):
        mock_es.client.search.return_value = _es_agg_response([
            {
                "key_as_string": "2024-01-01T00:00:00.000Z",
                "doc_count": 12,
                "by_reason": {
                    "buckets": [
                        {"key": "address_not_found", "doc_count": 7},
                        {"key": "customer_refused", "doc_count": 5},
                    ]
                },
            }
        ])

        resp = client.get("/ops/metrics/failures")
        assert resp.status_code == 200
        body = resp.json()
        vals = body["data"][0]["values"]
        assert vals["total_failures"] == 12
        assert vals["address_not_found"] == 7


# ===========================================================================
# Task 6.6 — Monitoring Endpoints
# ===========================================================================


class TestIngestionMonitoring:
    """GET /ops/monitoring/ingestion — Req 23.1"""

    def test_returns_ingestion_stats(self, client, mock_es):
        # First call: shipment_events count + latency
        events_resp = {
            "hits": {"total": {"value": 100}, "hits": []},
            "aggregations": {"avg_latency": {"value": 250.5}},
        }
        # Second call: poison queue count
        poison_resp = {
            "hits": {"total": {"value": 3}, "hits": []},
        }
        mock_es.client.search.side_effect = [events_resp, poison_resp]

        resp = client.get("/ops/monitoring/ingestion")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert data["events_processed"] == 100
        assert data["events_failed"] == 3
        assert data["events_received"] == 103
        assert data["avg_processing_latency_ms"] == 250.5
        assert body["request_id"] == "test-req-id"

    def test_custom_window(self, client, mock_es):
        mock_es.client.search.side_effect = [
            {"hits": {"total": {"value": 50}, "hits": []}, "aggregations": {"avg_latency": {"value": None}}},
            {"hits": {"total": {"value": 0}, "hits": []}},
        ]
        resp = client.get("/ops/monitoring/ingestion?window=1h")
        assert resp.status_code == 200
        assert resp.json()["data"]["window"] == "1h"


class TestIndexingMonitoring:
    """GET /ops/monitoring/indexing — Req 23.2"""

    def test_returns_indexing_stats(self, client, mock_es):
        idx_resp = {
            "hits": {"total": {"value": 50}, "hits": []},
            "aggregations": {"avg_latency": {"value": 120.0}},
        }
        poison_resp = {
            "hits": {"total": {"value": 2}, "hits": []},
        }
        # 3 index queries + 1 poison query
        mock_es.client.search.side_effect = [idx_resp, idx_resp, idx_resp, poison_resp]

        resp = client.get("/ops/monitoring/indexing")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert data["total_documents_indexed"] == 150  # 50 * 3
        assert data["indexing_errors"] == 2
        assert data["bulk_success_rate_pct"] == pytest.approx(98.68, abs=0.01)


class TestPoisonQueueMonitoring:
    """GET /ops/monitoring/poison-queue — Req 23.3"""

    def test_returns_queue_depth_and_stats(self, client, mock_es):
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        oldest_ms = now_ms - 3600_000  # 1 hour ago

        mock_es.client.search.return_value = {
            "hits": {"total": {"value": 15}, "hits": []},
            "aggregations": {
                "oldest_event": {"value": oldest_ms},
                "by_status": {
                    "buckets": [
                        {"key": "pending", "doc_count": 12},
                        {"key": "retrying", "doc_count": 3},
                    ]
                },
                "avg_retry_count": {"value": 1.5},
                "max_retry_count": {"value": 4.0},
                "by_error_type": {
                    "buckets": [
                        {"key": "transform_error", "doc_count": 10},
                        {"key": "indexing_error", "doc_count": 5},
                    ]
                },
            },
        }

        resp = client.get("/ops/monitoring/poison-queue")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert data["queue_depth"] == 15
        assert data["oldest_event_age_seconds"] == pytest.approx(3600, abs=5)
        assert data["status_breakdown"]["pending"] == 12
        assert data["retry_stats"]["avg_retry_count"] == 1.5
        assert data["retry_stats"]["max_retry_count"] == 4
