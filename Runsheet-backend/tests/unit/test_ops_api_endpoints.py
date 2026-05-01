"""
Unit tests for ops/api/endpoints.py — Ops API read endpoints.

Tests cover:
- GET /ops/shipments (paginated, filtered, sorted)
- GET /ops/shipments/{shipment_id} (single shipment + event history)
- GET /ops/riders (paginated, filtered)
- GET /ops/riders/{rider_id} (single rider + assigned shipments)
- GET /ops/events (paginated, filtered)
- Consistent JSON envelope {data, pagination, request_id}
- Tenant scoping via TenantContext

Validates: Requirements 8.1-8.6
"""

import math
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Patch the ElasticsearchService singleton BEFORE any ops imports so that
# importing ops_es_service doesn't trigger a real ES connection.
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ops.api.endpoints import router, configure_ops_api
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from ops.services.ops_es_service import OpsElasticsearchService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tenant() -> TenantContext:
    return TenantContext(tenant_id="tenant-1", user_id="user-1", has_pii_access=False)


def _es_search_response(hits: list[dict], total: int | None = None) -> dict:
    """Build a minimal ES search response."""
    return {
        "hits": {
            "hits": [{"_source": h} for h in hits],
            "total": {"value": total if total is not None else len(hits)},
        }
    }


SAMPLE_SHIPMENT = {
    "shipment_id": "SHP-001",
    "status": "in_transit",
    "tenant_id": "tenant-1",
    "rider_id": "RDR-001",
    "origin": "Warehouse A",
    "destination": "Customer B",
    "updated_at": "2025-01-01T12:00:00Z",
}

SAMPLE_EVENT = {
    "event_id": "EVT-001",
    "shipment_id": "SHP-001",
    "event_type": "shipment_created",
    "tenant_id": "tenant-1",
    "event_timestamp": "2025-01-01T10:00:00Z",
}

SAMPLE_RIDER = {
    "rider_id": "RDR-001",
    "rider_name": "Test Rider",
    "status": "active",
    "tenant_id": "tenant-1",
    "last_seen": "2025-01-01T12:00:00Z",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_es_client():
    """Mock ES client whose .search is a MagicMock (ops API calls search synchronously)."""
    client = MagicMock()
    client.search = MagicMock(return_value=_es_search_response([]))
    return client


@pytest.fixture()
def app(mock_es_client):
    """Create a FastAPI app with the ops router and mocked dependencies."""
    test_app = FastAPI()

    # Build a mock OpsElasticsearchService
    mock_ops_es = MagicMock(spec=OpsElasticsearchService)
    mock_ops_es.client = mock_es_client

    configure_ops_api(ops_es_service=mock_ops_es)

    # Override the tenant guard dependency so we don't need a real JWT
    async def _override_tenant():
        return _make_tenant()

    test_app.dependency_overrides[get_tenant_context] = _override_tenant

    # Attach a fake request_id via middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = "req-test-123"
            return await call_next(request)

    test_app.add_middleware(FakeRequestID)
    test_app.include_router(router)
    return test_app


@pytest.fixture()
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /ops/shipments
# ---------------------------------------------------------------------------

class TestListShipments:
    """Validates: Requirement 8.1"""

    def test_returns_paginated_envelope(self, client, mock_es_client):
        mock_es_client.search = MagicMock(
            return_value=_es_search_response([SAMPLE_SHIPMENT], total=1)
        )
        resp = client.get("/api/ops/shipments")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "pagination" in body
        assert "request_id" in body
        assert body["request_id"] == "req-test-123"

    def test_pagination_meta(self, client, mock_es_client):
        mock_es_client.search = MagicMock(
            return_value=_es_search_response([SAMPLE_SHIPMENT] * 5, total=42)
        )
        resp = client.get("/api/ops/shipments?page=2&size=5")
        body = resp.json()
        pag = body["pagination"]
        assert pag["page"] == 2
        assert pag["size"] == 5
        assert pag["total"] == 42
        assert pag["total_pages"] == math.ceil(42 / 5)

    def test_status_filter_passed_to_es(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments?status=delivered")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        # The tenant filter wraps the inner query
        must_clauses = call_body["query"]["bool"]["must"]
        # Inner bool must contain the status term
        inner_must = must_clauses[0]["bool"]["must"]
        assert any(f.get("term", {}).get("status") == "delivered" for f in inner_must)

    def test_date_range_filter(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments?start_date=2025-01-01&end_date=2025-01-31")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        range_filter = [f for f in inner_must if "range" in f]
        assert len(range_filter) == 1
        assert "updated_at" in range_filter[0]["range"]

    def test_sort_params(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments?sort_by=created_at&sort_order=asc")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        assert call_body["sort"] == [{"created_at": {"order": "asc"}}]

    def test_tenant_filter_injected(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        tenant_filter = call_body["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "tenant-1"}} in tenant_filter



# ---------------------------------------------------------------------------
# GET /ops/shipments/{shipment_id}
# ---------------------------------------------------------------------------

class TestGetShipment:
    """Validates: Requirement 8.2"""

    def test_returns_shipment_with_events(self, client, mock_es_client):
        mock_es_client.search = MagicMock(
            side_effect=[
                _es_search_response([SAMPLE_SHIPMENT], total=1),
                _es_search_response([SAMPLE_EVENT], total=1),
            ]
        )
        resp = client.get("/api/ops/shipments/SHP-001")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["shipment_id"] == "SHP-001"
        assert len(body["data"]["events"]) == 1
        assert body["request_id"] == "req-test-123"

    def test_not_found(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        resp = client.get("/api/ops/shipments/NONEXISTENT")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /ops/riders
# ---------------------------------------------------------------------------

class TestListRiders:
    """Validates: Requirement 8.3"""

    def test_returns_paginated_riders(self, client, mock_es_client):
        mock_es_client.search = MagicMock(
            return_value=_es_search_response([SAMPLE_RIDER], total=1)
        )
        resp = client.get("/api/ops/riders")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["pagination"]["total"] == 1

    def test_status_filter(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/riders?status=idle")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        assert must_clauses[0] == {"term": {"status": "idle"}}


# ---------------------------------------------------------------------------
# GET /ops/riders/{rider_id}
# ---------------------------------------------------------------------------

class TestGetRider:
    """Validates: Requirement 8.4"""

    def test_returns_rider_with_shipments(self, client, mock_es_client):
        mock_es_client.search = MagicMock(
            side_effect=[
                _es_search_response([SAMPLE_RIDER], total=1),
                _es_search_response([SAMPLE_SHIPMENT], total=1),
            ]
        )
        resp = client.get("/api/ops/riders/RDR-001")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["rider_id"] == "RDR-001"
        assert len(body["data"]["assigned_shipments"]) == 1

    def test_not_found(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        resp = client.get("/api/ops/riders/NONEXISTENT")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /ops/events
# ---------------------------------------------------------------------------

class TestListEvents:
    """Validates: Requirement 8.5"""

    def test_returns_paginated_events(self, client, mock_es_client):
        mock_es_client.search = MagicMock(
            return_value=_es_search_response([SAMPLE_EVENT], total=1)
        )
        resp = client.get("/api/ops/events")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["pagination"]["total"] == 1

    def test_shipment_id_filter(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/events?shipment_id=SHP-001")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        assert any(f.get("term", {}).get("shipment_id") == "SHP-001" for f in inner_must)

    def test_event_type_filter(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/events?event_type=shipment_created")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        assert any(f.get("term", {}).get("event_type") == "shipment_created" for f in inner_must)

    def test_time_range_filter(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/events?start_date=2025-01-01&end_date=2025-01-31")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        range_filter = [f for f in inner_must if "range" in f]
        assert len(range_filter) == 1
        assert "event_timestamp" in range_filter[0]["range"]


# ---------------------------------------------------------------------------
# JSON envelope consistency (Requirement 8.6)
# ---------------------------------------------------------------------------

class TestResponseEnvelope:
    """Validates: Requirement 8.6 — consistent {data, pagination, request_id}."""

    @pytest.mark.parametrize("path", ["/api/ops/shipments", "/api/ops/riders", "/api/ops/events"])
    def test_envelope_keys(self, client, mock_es_client, path):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        resp = client.get(path)
        body = resp.json()
        # Dual-field deprecation: both old and new keys present
        assert {"data", "pagination", "request_id"}.issubset(set(body.keys()))
        assert {"items", "total", "page", "page_size", "has_next"}.issubset(set(body.keys()))

    @pytest.mark.parametrize("path", ["/api/ops/shipments", "/api/ops/riders", "/api/ops/events"])
    def test_pagination_keys(self, client, mock_es_client, path):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        resp = client.get(path)
        pag = resp.json()["pagination"]
        assert set(pag.keys()) == {"page", "size", "total", "total_pages"}


# ---------------------------------------------------------------------------
# GET /ops/shipments/sla-breaches
# ---------------------------------------------------------------------------

SAMPLE_SLA_BREACH_SHIPMENT = {
    "shipment_id": "SHP-SLA-001",
    "status": "in_transit",
    "tenant_id": "tenant-1",
    "rider_id": "RDR-001",
    "estimated_delivery": "2025-01-01T10:00:00Z",
    "updated_at": "2025-01-01T12:00:00Z",
}


class TestGetSlaBreaches:
    """Validates: Requirement 10.2"""

    def test_returns_paginated_envelope(self, client, mock_es_client):
        mock_es_client.search = MagicMock(
            return_value=_es_search_response([SAMPLE_SLA_BREACH_SHIPMENT], total=1)
        )
        resp = client.get("/api/ops/shipments/sla-breaches")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "pagination" in body
        assert "request_id" in body

    def test_queries_estimated_delivery_lt_now(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments/sla-breaches")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        range_filters = [f for f in inner_must if "range" in f]
        assert any("estimated_delivery" in rf["range"] for rf in range_filters)

    def test_supports_status_filter(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments/sla-breaches?status=in_transit")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        assert any(f.get("term", {}).get("status") == "in_transit" for f in inner_must)

    def test_supports_rider_id_filter(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments/sla-breaches?rider_id=RDR-001")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        assert any(f.get("term", {}).get("rider_id") == "RDR-001" for f in inner_must)

    def test_invalid_status_returns_400(self, client, mock_es_client):
        resp = client.get("/api/ops/shipments/sla-breaches?status=bogus")
        assert resp.status_code == 400
        assert "Invalid status" in resp.json()["detail"]

    def test_tenant_filter_injected(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments/sla-breaches")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        tenant_filter = call_body["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "tenant-1"}} in tenant_filter


# ---------------------------------------------------------------------------
# GET /ops/shipments/failures
# ---------------------------------------------------------------------------

SAMPLE_FAILED_SHIPMENT = {
    "shipment_id": "SHP-FAIL-001",
    "status": "failed",
    "tenant_id": "tenant-1",
    "rider_id": "RDR-001",
    "failure_reason": "customer_unavailable",
    "updated_at": "2025-01-01T12:00:00Z",
}


class TestGetShipmentFailures:
    """Validates: Requirement 10.4"""

    def test_returns_paginated_envelope(self, client, mock_es_client):
        mock_es_client.search = MagicMock(
            return_value=_es_search_response([SAMPLE_FAILED_SHIPMENT], total=1)
        )
        resp = client.get("/api/ops/shipments/failures")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "pagination" in body
        assert "request_id" in body

    def test_queries_failed_status(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments/failures")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        assert any(f.get("term", {}).get("status") == "failed" for f in inner_must)

    def test_enriches_failure_reason_from_event(self, client, mock_es_client):
        """When failure_reason is missing from shipment doc, fetch from latest event."""
        shipment_no_reason = {
            "shipment_id": "SHP-FAIL-002",
            "status": "failed",
            "tenant_id": "tenant-1",
            "updated_at": "2025-01-01T12:00:00Z",
        }
        event_with_reason = {
            "event_id": "EVT-FAIL-001",
            "shipment_id": "SHP-FAIL-002",
            "event_type": "shipment_failed",
            "tenant_id": "tenant-1",
            "event_timestamp": "2025-01-01T12:00:00Z",
            "event_payload": {"failure_reason": "address_not_found"},
        }
        mock_es_client.search = MagicMock(
            side_effect=[
                _es_search_response([shipment_no_reason], total=1),
                _es_search_response([event_with_reason], total=1),
            ]
        )
        resp = client.get("/api/ops/shipments/failures")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"][0]["failure_reason"] == "address_not_found"

    def test_supports_date_range_filter(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments/failures?start_date=2025-01-01&end_date=2025-01-31")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        range_filter = [f for f in inner_must if "range" in f]
        assert len(range_filter) == 1

    def test_invalid_date_returns_400(self, client, mock_es_client):
        resp = client.get("/api/ops/shipments/failures?start_date=not-a-date")
        assert resp.status_code == 400
        assert "Invalid start_date" in resp.json()["detail"]

    def test_tenant_filter_injected(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments/failures")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        tenant_filter = call_body["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "tenant-1"}} in tenant_filter


# ---------------------------------------------------------------------------
# GET /ops/riders/utilization
# ---------------------------------------------------------------------------

SAMPLE_RIDER_WITH_METRICS = {
    "rider_id": "RDR-001",
    "rider_name": "Test Rider",
    "status": "active",
    "tenant_id": "tenant-1",
    "active_shipment_count": 3,
    "completed_today": 5,
    "last_seen": "2025-01-01T12:00:00Z",
}


class TestGetRiderUtilization:
    """Validates: Requirement 10.3"""

    def test_returns_paginated_envelope(self, client, mock_es_client):
        mock_es_client.search = MagicMock(
            return_value=_es_search_response([SAMPLE_RIDER_WITH_METRICS], total=1)
        )
        resp = client.get("/api/ops/riders/utilization")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "pagination" in body
        assert "request_id" in body

    def test_includes_utilization_metrics(self, client, mock_es_client):
        mock_es_client.search = MagicMock(
            return_value=_es_search_response([SAMPLE_RIDER_WITH_METRICS], total=1)
        )
        resp = client.get("/api/ops/riders/utilization")
        body = resp.json()
        rider = body["data"][0]
        assert "utilization" in rider
        assert rider["utilization"]["active_shipments"] == 3
        assert rider["utilization"]["completed_today"] == 5
        assert rider["utilization"]["idle_minutes"] is not None

    def test_supports_status_filter(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/riders/utilization?status=active")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        assert any(f.get("term", {}).get("status") == "active" for f in inner_must)

    def test_invalid_status_returns_400(self, client, mock_es_client):
        resp = client.get("/api/ops/riders/utilization?status=bogus")
        assert resp.status_code == 400
        assert "Invalid status" in resp.json()["detail"]

    def test_tenant_filter_injected(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/riders/utilization")
        call_body = mock_es_client.search.call_args.kwargs["body"]
        tenant_filter = call_body["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "tenant-1"}} in tenant_filter


# ---------------------------------------------------------------------------
# Validation: 400 for invalid filter values (Requirement 10.6)
# ---------------------------------------------------------------------------

class TestInvalidFilterValidation:
    """Validates: Requirement 10.6 — 400 for invalid filter values."""

    def test_invalid_status_on_list_shipments(self, client, mock_es_client):
        resp = client.get("/api/ops/shipments?status=invalid_status")
        assert resp.status_code == 400

    def test_invalid_date_on_list_shipments(self, client, mock_es_client):
        resp = client.get("/api/ops/shipments?start_date=not-a-date")
        assert resp.status_code == 400

    def test_valid_status_on_list_shipments(self, client, mock_es_client):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        resp = client.get("/api/ops/shipments?status=pending")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Response envelope for new filtered endpoints (Requirement 8.6)
# ---------------------------------------------------------------------------

class TestFilteredEndpointEnvelopes:
    """Validates: Requirement 8.6 — consistent envelope on filtered endpoints."""

    @pytest.mark.parametrize("path", [
        "/api/ops/shipments/sla-breaches",
        "/api/ops/shipments/failures",
        "/api/ops/riders/utilization",
    ])
    def test_envelope_keys(self, client, mock_es_client, path):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        resp = client.get(path)
        body = resp.json()
        # Dual-field deprecation: both old and new keys present
        assert {"data", "pagination", "request_id"}.issubset(set(body.keys()))
        assert {"items", "total", "page", "page_size", "has_next"}.issubset(set(body.keys()))

    @pytest.mark.parametrize("path", [
        "/api/ops/shipments/sla-breaches",
        "/api/ops/shipments/failures",
        "/api/ops/riders/utilization",
    ])
    def test_pagination_keys(self, client, mock_es_client, path):
        mock_es_client.search = MagicMock(return_value=_es_search_response([]))
        resp = client.get(path)
        pag = resp.json()["pagination"]
        assert set(pag.keys()) == {"page", "size", "total", "total_pages"}
