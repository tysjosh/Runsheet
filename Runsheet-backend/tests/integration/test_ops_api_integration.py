"""
Integration tests for the Ops API endpoints.

Tests all endpoints with tenant scoping, verifies cross-tenant isolation,
and tests filter combinations and pagination.

Validates: Requirements 8.1-8.6, 9.1-9.8
"""

import math
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Patch elasticsearch_service BEFORE any ops imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from ops.api.endpoints import router, configure_ops_api, require_ops_enabled
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from ops.services.ops_es_service import OpsElasticsearchService

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

TENANT_A = "tenant-A"
TENANT_B = "tenant-B"

SHIPMENTS_A = [
    {"shipment_id": "SHP-A-001", "status": "in_transit", "tenant_id": TENANT_A,
     "rider_id": "RDR-A-1", "origin": "WH-A", "destination": "Cust-A",
     "updated_at": "2025-01-15T12:00:00Z", "estimated_delivery": "2025-01-15T17:00:00Z"},
    {"shipment_id": "SHP-A-002", "status": "delivered", "tenant_id": TENANT_A,
     "rider_id": "RDR-A-2", "origin": "WH-A", "destination": "Cust-B",
     "updated_at": "2025-01-14T10:00:00Z", "estimated_delivery": "2025-01-14T15:00:00Z"},
    {"shipment_id": "SHP-A-003", "status": "failed", "tenant_id": TENANT_A,
     "rider_id": "RDR-A-1", "origin": "WH-A", "destination": "Cust-C",
     "updated_at": "2025-01-13T08:00:00Z", "failure_reason": "Address not found",
     "estimated_delivery": "2025-01-13T12:00:00Z"},
]

RIDERS_A = [
    {"rider_id": "RDR-A-1", "rider_name": "Rider One", "status": "active",
     "tenant_id": TENANT_A, "last_seen": "2025-01-15T12:00:00Z",
     "active_shipment_count": 2, "completed_today": 5},
    {"rider_id": "RDR-A-2", "rider_name": "Rider Two", "status": "idle",
     "tenant_id": TENANT_A, "last_seen": "2025-01-15T11:00:00Z",
     "active_shipment_count": 0, "completed_today": 3},
]

EVENTS_A = [
    {"event_id": "EVT-A-001", "shipment_id": "SHP-A-001", "event_type": "shipment_created",
     "tenant_id": TENANT_A, "event_timestamp": "2025-01-15T09:00:00Z"},
    {"event_id": "EVT-A-002", "shipment_id": "SHP-A-001", "event_type": "shipment_updated",
     "tenant_id": TENANT_A, "event_timestamp": "2025-01-15T12:00:00Z"},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _es_search_response(hits: list[dict], total: int | None = None) -> dict:
    return {
        "hits": {
            "hits": [{"_source": h} for h in hits],
            "total": {"value": total if total is not None else len(hits)},
        }
    }


def _build_app(tenant_id: str = TENANT_A, pii_access: bool = False):
    """Build a FastAPI test app with mocked ES and overridden tenant guard."""
    app = FastAPI()

    mock_es_client = MagicMock()
    mock_es_client.search = MagicMock(return_value=_es_search_response([]))

    mock_ops_es = MagicMock(spec=OpsElasticsearchService)
    mock_ops_es.client = mock_es_client

    # No feature flag service → require_ops_enabled passes through
    configure_ops_api(ops_es_service=mock_ops_es, feature_flag_service=None)

    # Register structured exception handlers so AppException → proper JSON
    from errors.handlers import register_exception_handlers
    register_exception_handlers(app)

    tenant_ctx = TenantContext(
        tenant_id=tenant_id, user_id="user-1", has_pii_access=pii_access
    )

    async def _override_tenant():
        return tenant_ctx

    # Override both the raw tenant context and the require_ops_enabled dependency
    app.dependency_overrides[get_tenant_context] = _override_tenant
    app.dependency_overrides[require_ops_enabled] = _override_tenant

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = "req-integ-001"
            return await call_next(request)

    app.add_middleware(FakeRequestID)
    app.include_router(router)

    client = TestClient(app)
    return client, mock_es_client


# ===========================================================================
# 23.2 — Ops API integration tests
# ===========================================================================


class TestOpsApiEndpointIntegration:
    """
    Test all endpoints return correct JSON envelope with tenant scoping.

    Validates: Requirements 8.1-8.6
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client, self.mock_es = _build_app(TENANT_A)

    def test_list_shipments_returns_envelope(self):
        self.mock_es.search = MagicMock(
            return_value=_es_search_response(SHIPMENTS_A, total=3)
        )
        resp = self.client.get("/api/ops/shipments")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "pagination" in body
        assert "request_id" in body
        assert body["request_id"] == "req-integ-001"

    def test_list_shipments_pagination_meta(self):
        self.mock_es.search = MagicMock(
            return_value=_es_search_response(SHIPMENTS_A[:2], total=3)
        )
        resp = self.client.get("/api/ops/shipments?page=1&size=2")
        body = resp.json()
        pag = body["pagination"]
        assert pag["page"] == 1
        assert pag["size"] == 2
        assert pag["total"] == 3
        assert pag["total_pages"] == math.ceil(3 / 2)

    def test_get_single_shipment(self):
        """GET /ops/shipments/{id} returns shipment with event history."""
        # The endpoint calls search twice: once for shipment, once for events
        self.mock_es.search = MagicMock(
            side_effect=[
                _es_search_response([SHIPMENTS_A[0]], total=1),
                _es_search_response(EVENTS_A, total=2),
            ]
        )
        resp = self.client.get("/api/ops/shipments/SHP-A-001")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "request_id" in body

    def test_list_riders_returns_envelope(self):
        self.mock_es.search = MagicMock(
            return_value=_es_search_response(RIDERS_A, total=2)
        )
        resp = self.client.get("/api/ops/riders")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "pagination" in body

    def test_get_single_rider(self):
        """GET /ops/riders/{id} returns rider with assigned shipments."""
        self.mock_es.search = MagicMock(
            side_effect=[
                _es_search_response([RIDERS_A[0]], total=1),
                _es_search_response(SHIPMENTS_A[:1], total=1),
            ]
        )
        resp = self.client.get("/api/ops/riders/RDR-A-1")
        assert resp.status_code == 200

    def test_list_events_returns_envelope(self):
        self.mock_es.search = MagicMock(
            return_value=_es_search_response(EVENTS_A, total=2)
        )
        resp = self.client.get("/api/ops/events")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "pagination" in body


class TestTenantIsolation:
    """
    Verify cross-tenant isolation: every ES query includes the tenant_id filter.

    Validates: Requirements 9.1-9.8
    """

    def _extract_tenant_filter(self, mock_es) -> str | None:
        """Extract the tenant_id from the filter clause of the last ES search call."""
        if not mock_es.search.call_args:
            return None
        call_body = mock_es.search.call_args.kwargs.get("body", {})
        filters = call_body.get("query", {}).get("bool", {}).get("filter", [])
        for f in filters:
            if "term" in f and "tenant_id" in f["term"]:
                return f["term"]["tenant_id"]
        return None

    def test_shipments_query_scoped_to_tenant_a(self):
        client, mock_es = _build_app(TENANT_A)
        mock_es.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments")
        assert self._extract_tenant_filter(mock_es) == TENANT_A

    def test_shipments_query_scoped_to_tenant_b(self):
        client, mock_es = _build_app(TENANT_B)
        mock_es.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/shipments")
        assert self._extract_tenant_filter(mock_es) == TENANT_B

    def test_riders_query_scoped_to_tenant(self):
        client, mock_es = _build_app(TENANT_A)
        mock_es.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/riders")
        assert self._extract_tenant_filter(mock_es) == TENANT_A

    def test_events_query_scoped_to_tenant(self):
        client, mock_es = _build_app(TENANT_A)
        mock_es.search = MagicMock(return_value=_es_search_response([]))
        client.get("/api/ops/events")
        assert self._extract_tenant_filter(mock_es) == TENANT_A

    def test_tenant_a_cannot_see_tenant_b_data(self):
        """Tenant A's query never includes tenant B's ID in the filter."""
        client, mock_es = _build_app(TENANT_A)
        mock_es.search = MagicMock(
            return_value=_es_search_response(SHIPMENTS_A, total=3)
        )
        resp = client.get("/api/ops/shipments")
        body = resp.json()
        for item in body["data"]:
            assert item.get("tenant_id") == TENANT_A
        assert self._extract_tenant_filter(mock_es) == TENANT_A


class TestFilterCombinations:
    """
    Test filter combinations and pagination.

    Validates: Requirements 8.1-8.6, 10.1, 10.5, 10.6
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client, self.mock_es = _build_app(TENANT_A)

    def test_status_filter(self):
        self.mock_es.search = MagicMock(return_value=_es_search_response([]))
        self.client.get("/api/ops/shipments?status=delivered")
        call_body = self.mock_es.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        assert any(f.get("term", {}).get("status") == "delivered" for f in inner_must)

    def test_rider_id_filter(self):
        self.mock_es.search = MagicMock(return_value=_es_search_response([]))
        self.client.get("/api/ops/shipments?rider_id=RDR-A-1")
        call_body = self.mock_es.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        assert any(f.get("term", {}).get("rider_id") == "RDR-A-1" for f in inner_must)

    def test_date_range_filter(self):
        self.mock_es.search = MagicMock(return_value=_es_search_response([]))
        self.client.get("/api/ops/shipments?start_date=2025-01-01&end_date=2025-01-31")
        call_body = self.mock_es.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        range_filters = [f for f in inner_must if "range" in f]
        assert len(range_filters) == 1

    def test_combined_status_and_rider_filter(self):
        """Combining status + rider_id produces both filters in the query."""
        self.mock_es.search = MagicMock(return_value=_es_search_response([]))
        self.client.get("/api/ops/shipments?status=in_transit&rider_id=RDR-A-1")
        call_body = self.mock_es.search.call_args.kwargs["body"]
        must_clauses = call_body["query"]["bool"]["must"]
        inner_must = must_clauses[0]["bool"]["must"]
        has_status = any(f.get("term", {}).get("status") == "in_transit" for f in inner_must)
        has_rider = any(f.get("term", {}).get("rider_id") == "RDR-A-1" for f in inner_must)
        assert has_status and has_rider

    def test_invalid_status_returns_400(self):
        resp = self.client.get("/api/ops/shipments?status=nonexistent")
        assert resp.status_code == 400

    def test_pagination_total_pages_calculation(self):
        self.mock_es.search = MagicMock(
            return_value=_es_search_response([SHIPMENTS_A[0]], total=50)
        )
        resp = self.client.get("/api/ops/shipments?page=3&size=10")
        body = resp.json()
        assert body["pagination"]["total_pages"] == math.ceil(50 / 10)
        assert body["pagination"]["page"] == 3

    def test_sort_order(self):
        self.mock_es.search = MagicMock(return_value=_es_search_response([]))
        self.client.get("/api/ops/shipments?sort_by=created_at&sort_order=asc")
        call_body = self.mock_es.search.call_args.kwargs["body"]
        assert call_body["sort"] == [{"created_at": {"order": "asc"}}]


class TestFilteredEndpoints:
    """
    Test SLA breaches, failures, and utilization endpoints.

    Validates: Requirements 10.2-10.4
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client, self.mock_es = _build_app(TENANT_A)

    def test_sla_breaches_endpoint(self):
        self.mock_es.search = MagicMock(return_value=_es_search_response([], total=0))
        resp = self.client.get("/api/ops/shipments/sla-breaches")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body

    def test_shipment_failures_endpoint(self):
        self.mock_es.search = MagicMock(return_value=_es_search_response([], total=0))
        resp = self.client.get("/api/ops/shipments/failures")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body

    def test_rider_utilization_endpoint(self):
        self.mock_es.search = MagicMock(return_value=_es_search_response([], total=0))
        resp = self.client.get("/api/ops/riders/utilization")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
