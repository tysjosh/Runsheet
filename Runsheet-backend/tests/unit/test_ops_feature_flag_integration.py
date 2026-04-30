"""
Unit tests for feature flag integration in ops API endpoints.

Verifies that all tenant-scoped ops endpoints return 404 when the
Ops Intelligence Layer is disabled for the requesting tenant.

Validates: Requirement 27.3
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Patch the ElasticsearchService singleton before ops imports
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ops.api.endpoints import router, configure_ops_api, require_ops_enabled
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from ops.services.feature_flags import FeatureFlagService
from ops.services.ops_es_service import OpsElasticsearchService


def _make_tenant() -> TenantContext:
    return TenantContext(tenant_id="tenant-1", user_id="user-1", has_pii_access=False)


def _es_search_response(hits=None, total=0):
    return {
        "hits": {
            "hits": [{"_source": h} for h in (hits or [])],
            "total": {"value": total},
        }
    }


# Endpoints that always return 200 with empty data when enabled
LIST_ENDPOINTS = [
    ("GET", "/api/ops/shipments"),
    ("GET", "/api/ops/shipments/sla-breaches"),
    ("GET", "/api/ops/shipments/failures"),
    ("GET", "/api/ops/riders"),
    ("GET", "/api/ops/riders/utilization"),
    ("GET", "/api/ops/events"),
    ("GET", "/api/ops/metrics/shipments"),
    ("GET", "/api/ops/metrics/sla"),
    ("GET", "/api/ops/metrics/riders"),
    ("GET", "/api/ops/metrics/failures"),
]

# Single-resource endpoints that return 404 when resource not found (even when enabled)
SINGLE_RESOURCE_ENDPOINTS = [
    ("GET", "/api/ops/shipments/SHP-001"),
    ("GET", "/api/ops/riders/RDR-001"),
]

# All tenant-scoped endpoints combined
TENANT_SCOPED_ENDPOINTS = LIST_ENDPOINTS + SINGLE_RESOURCE_ENDPOINTS


@pytest.fixture()
def mock_ff_service():
    """A mock FeatureFlagService."""
    svc = AsyncMock(spec=FeatureFlagService)
    svc.is_enabled = AsyncMock(return_value=True)
    return svc


@pytest.fixture()
def mock_es_client():
    client = MagicMock()
    client.search = MagicMock(return_value=_es_search_response())
    return client


def _build_app(mock_es_client, mock_ff_service):
    """Build a FastAPI app with ops router, mocked ES and feature flag service."""
    test_app = FastAPI()

    mock_ops_es = MagicMock(spec=OpsElasticsearchService)
    mock_ops_es.client = mock_es_client

    configure_ops_api(
        ops_es_service=mock_ops_es,
        feature_flag_service=mock_ff_service,
    )

    async def _override_tenant():
        return _make_tenant()

    test_app.dependency_overrides[get_tenant_context] = _override_tenant

    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = "req-test-ff"
            return await call_next(request)

    test_app.add_middleware(FakeRequestID)
    test_app.include_router(router)
    return test_app


class TestFeatureFlagDisabledReturns404:
    """When the feature flag is disabled, all tenant-scoped ops endpoints return 404."""

    @pytest.fixture(autouse=True)
    def setup(self, mock_es_client, mock_ff_service):
        mock_ff_service.is_enabled = AsyncMock(return_value=False)
        self.app = _build_app(mock_es_client, mock_ff_service)
        self.client = TestClient(self.app)

    @pytest.mark.parametrize("method,path", TENANT_SCOPED_ENDPOINTS)
    def test_returns_404_when_disabled(self, method, path):
        resp = self.client.request(method, path)
        assert resp.status_code == 404, f"{method} {path} returned {resp.status_code}"
        body = resp.json()
        assert body["detail"]["error_code"] == "TENANT_DISABLED"

    @pytest.mark.parametrize("method,path", TENANT_SCOPED_ENDPOINTS)
    def test_feature_flag_checked_with_tenant_id(self, method, path, mock_ff_service):
        self.client.request(method, path)
        mock_ff_service.is_enabled.assert_called_with("tenant-1")


class TestFeatureFlagEnabledAllowsAccess:
    """When the feature flag is enabled, list endpoints proceed normally."""

    @pytest.fixture(autouse=True)
    def setup(self, mock_es_client, mock_ff_service):
        mock_ff_service.is_enabled = AsyncMock(return_value=True)
        self.app = _build_app(mock_es_client, mock_ff_service)
        self.client = TestClient(self.app)

    @pytest.mark.parametrize("method,path", LIST_ENDPOINTS)
    def test_returns_200_when_enabled(self, method, path):
        resp = self.client.request(method, path)
        assert resp.status_code == 200, f"{method} {path} returned {resp.status_code}"

    @pytest.mark.parametrize("method,path", SINGLE_RESOURCE_ENDPOINTS)
    def test_single_resource_not_blocked_by_feature_flag(self, method, path):
        """Single-resource endpoints may return 404 for missing resource, but NOT TENANT_DISABLED."""
        resp = self.client.request(method, path)
        # 404 is expected because mock ES returns empty results (resource not found)
        # but the detail should NOT be TENANT_DISABLED
        if resp.status_code == 404:
            body = resp.json()
            detail = body.get("detail", "")
            if isinstance(detail, dict):
                assert detail.get("error_code") != "TENANT_DISABLED"


class TestFeatureFlagNotConfigured:
    """When no feature flag service is configured, endpoints work normally."""

    @pytest.fixture(autouse=True)
    def setup(self, mock_es_client):
        self.app = _build_app(mock_es_client, mock_ff_service=None)
        self.client = TestClient(self.app)

    @pytest.mark.parametrize("method,path", LIST_ENDPOINTS)
    def test_returns_200_without_ff_service(self, method, path):
        resp = self.client.request(method, path)
        assert resp.status_code == 200, f"{method} {path} returned {resp.status_code}"
