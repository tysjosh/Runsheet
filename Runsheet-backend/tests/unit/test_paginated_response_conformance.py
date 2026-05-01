"""
Parametrized test verifying all list endpoints return PaginatedResponse-conforming JSON.

During the 60-day deprecation window, responses include both old and new fields.
This test validates the presence of the new unified PaginatedResponse fields
(items, total, page, page_size, has_next) on every paginated list endpoint.

Validates: Requirements 4.4, Correctness Property P10
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from schemas.common import PaginatedResponse


# ---------------------------------------------------------------------------
# Patch heavy modules before importing endpoint routers
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _es_search_response(hits: list[dict], total: int | None = None) -> dict:
    """Build a minimal ES search response."""
    return {
        "hits": {
            "hits": [{"_source": h} for h in hits],
            "total": {"value": total if total is not None else len(hits)},
        },
    }


UNIFIED_FIELDS = {"items", "total", "page", "page_size", "has_next"}
DEPRECATED_FIELDS = {"data", "pagination"}


# ---------------------------------------------------------------------------
# Ops router fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def ops_mock_es_client():
    client = MagicMock()
    client.search = MagicMock(return_value=_es_search_response([]))
    return client


@pytest.fixture()
def ops_client(ops_mock_es_client):
    from ops.api.endpoints import router as ops_router, configure_ops_api
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context
    from ops.services.ops_es_service import OpsElasticsearchService

    app = FastAPI()
    mock_ops_es = MagicMock(spec=OpsElasticsearchService)
    mock_ops_es.client = ops_mock_es_client
    configure_ops_api(ops_es_service=mock_ops_es)

    async def _override_tenant():
        return TenantContext(tenant_id="t1", user_id="u1", has_pii_access=False)

    app.dependency_overrides[get_tenant_context] = _override_tenant

    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = "req-test"
            return await call_next(request)

    app.add_middleware(FakeRequestID)
    app.include_router(ops_router)
    return TestClient(app)


OPS_PAGINATED_PATHS = [
    "/api/ops/shipments",
    "/api/ops/shipments/sla-breaches",
    "/api/ops/shipments/failures",
    "/api/ops/riders",
    "/api/ops/riders/utilization",
    "/api/ops/events",
]


@pytest.mark.parametrize("path", OPS_PAGINATED_PATHS)
def test_ops_paginated_response_conformance(ops_client, path):
    """Ops paginated endpoints return PaginatedResponse-conforming JSON."""
    resp = ops_client.get(path)
    assert resp.status_code == 200
    body = resp.json()

    # New unified fields must be present
    assert UNIFIED_FIELDS.issubset(set(body.keys())), (
        f"Missing unified fields in {path}: {UNIFIED_FIELDS - set(body.keys())}"
    )
    # Deprecated fields still present during deprecation window
    assert DEPRECATED_FIELDS.issubset(set(body.keys())), (
        f"Missing deprecated fields in {path}: {DEPRECATED_FIELDS - set(body.keys())}"
    )
    # Validate types
    assert isinstance(body["items"], list)
    assert isinstance(body["total"], int)
    assert isinstance(body["page"], int)
    assert isinstance(body["page_size"], int)
    assert isinstance(body["has_next"], bool)
    # items and data should be the same list
    assert body["items"] == body["data"]
    # Validate PaginatedResponse model can parse the unified fields
    PaginatedResponse.model_validate({
        "items": body["items"],
        "total": body["total"],
        "page": body["page"],
        "page_size": body["page_size"],
        "has_next": body["has_next"],
    })


# ---------------------------------------------------------------------------
# Fuel router fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def fuel_client():
    from fuel.api.endpoints import router as fuel_router, configure_fuel_api
    from fuel.services.fuel_service import FuelService
    from fuel.models import PaginatedResponse as FuelPaginatedResponse, PaginationMeta, FuelStation
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context

    app = FastAPI()

    mock_fuel_svc = MagicMock(spec=FuelService)
    mock_fuel_svc.list_stations = AsyncMock(
        return_value=FuelPaginatedResponse[FuelStation](
            data=[],
            pagination=PaginationMeta.compute(page=1, size=50, total=0),
            request_id="req-test",
        )
    )
    configure_fuel_api(fuel_service=mock_fuel_svc)

    async def _override_tenant():
        return TenantContext(tenant_id="t1", user_id="u1", has_pii_access=False)

    app.dependency_overrides[get_tenant_context] = _override_tenant

    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = "req-test"
            return await call_next(request)

    app.add_middleware(FakeRequestID)
    app.include_router(fuel_router)
    return TestClient(app)


def test_fuel_stations_paginated_response_conformance(fuel_client):
    """Fuel stations endpoint returns PaginatedResponse-conforming JSON."""
    resp = fuel_client.get("/api/fuel/stations")
    assert resp.status_code == 200
    body = resp.json()

    assert UNIFIED_FIELDS.issubset(set(body.keys())), (
        f"Missing unified fields: {UNIFIED_FIELDS - set(body.keys())}"
    )
    assert DEPRECATED_FIELDS.issubset(set(body.keys()))
    assert isinstance(body["items"], list)
    assert isinstance(body["total"], int)
    assert isinstance(body["has_next"], bool)
    PaginatedResponse.model_validate({
        "items": body["items"],
        "total": body["total"],
        "page": body["page"],
        "page_size": body["page_size"],
        "has_next": body["has_next"],
    })


# ---------------------------------------------------------------------------
# Scheduling router fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def scheduling_mock_es():
    mock_es = MagicMock()
    mock_es.search_documents = AsyncMock(return_value=_es_search_response([]))
    mock_es.get_document = AsyncMock(return_value={})
    return mock_es


@pytest.fixture()
def scheduling_client(scheduling_mock_es):
    from scheduling.api.endpoints import router as sched_router, configure_scheduling_api
    from scheduling.services.job_service import JobService
    from scheduling.services.cargo_service import CargoService
    from scheduling.services.delay_detection_service import DelayDetectionService
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context

    app = FastAPI()

    mock_job_svc = MagicMock(spec=JobService)
    mock_job_svc._es = scheduling_mock_es
    mock_job_svc.list_jobs = AsyncMock(return_value={
        "data": [],
        "pagination": {"page": 1, "size": 20, "total": 0, "total_pages": 0},
    })
    mock_job_svc.get_active_jobs = AsyncMock(return_value=[])
    mock_job_svc.get_delayed_jobs = AsyncMock(return_value=[])
    mock_job_svc.get_job_events = AsyncMock(return_value=[])

    mock_cargo_svc = MagicMock(spec=CargoService)
    mock_cargo_svc.search_cargo = AsyncMock(return_value={
        "data": [],
        "pagination": {"page": 1, "size": 20, "total": 0, "total_pages": 0},
    })

    mock_delay_svc = MagicMock(spec=DelayDetectionService)

    configure_scheduling_api(
        job_service=mock_job_svc,
        cargo_service=mock_cargo_svc,
        delay_service=mock_delay_svc,
    )

    async def _override_tenant():
        return TenantContext(tenant_id="t1", user_id="u1", has_pii_access=False)

    app.dependency_overrides[get_tenant_context] = _override_tenant

    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = "req-test"
            return await call_next(request)

    app.add_middleware(FakeRequestID)
    app.include_router(sched_router)
    return TestClient(app)


SCHEDULING_PAGINATED_PATHS = [
    "/api/scheduling/jobs",
    "/api/scheduling/jobs/active",
    "/api/scheduling/jobs/delayed",
    "/api/scheduling/cargo/search",
]


@pytest.mark.parametrize("path", SCHEDULING_PAGINATED_PATHS)
def test_scheduling_paginated_response_conformance(scheduling_client, path):
    """Scheduling paginated endpoints return PaginatedResponse-conforming JSON."""
    resp = scheduling_client.get(path)
    assert resp.status_code == 200
    body = resp.json()

    assert UNIFIED_FIELDS.issubset(set(body.keys())), (
        f"Missing unified fields in {path}: {UNIFIED_FIELDS - set(body.keys())}"
    )
    assert DEPRECATED_FIELDS.issubset(set(body.keys()))
    assert isinstance(body["items"], list)
    assert isinstance(body["total"], int)
    assert isinstance(body["has_next"], bool)
    PaginatedResponse.model_validate({
        "items": body["items"],
        "total": body["total"],
        "page": body["page"],
        "page_size": body["page_size"],
        "has_next": body["has_next"],
    })


def test_scheduling_job_events_paginated_response_conformance(scheduling_client):
    """Scheduling job events endpoint returns PaginatedResponse-conforming JSON."""
    resp = scheduling_client.get("/api/scheduling/jobs/JOB-001/events")
    assert resp.status_code == 200
    body = resp.json()

    assert UNIFIED_FIELDS.issubset(set(body.keys()))
    assert DEPRECATED_FIELDS.issubset(set(body.keys()))
    PaginatedResponse.model_validate({
        "items": body["items"],
        "total": body["total"],
        "page": body["page"],
        "page_size": body["page_size"],
        "has_next": body["has_next"],
    })


# ---------------------------------------------------------------------------
# Agent router fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def agent_client():
    from agent_endpoints import router as agent_router, configure_agent_endpoints

    app = FastAPI()

    mock_approval = MagicMock()
    mock_approval.list_pending = AsyncMock(return_value={
        "data": [],
        "pagination": {"page": 1, "size": 20, "total": 0, "total_pages": 0},
    })

    mock_activity = MagicMock()
    mock_activity.query = AsyncMock(return_value={
        "data": [],
        "pagination": {"page": 1, "size": 50, "total": 0, "total_pages": 0},
    })

    mock_memory = MagicMock()
    mock_memory.list_memories = AsyncMock(return_value={
        "data": [],
        "pagination": {"page": 1, "size": 20, "total": 0, "total_pages": 0},
    })

    mock_feedback = MagicMock()
    mock_feedback.list_feedback = AsyncMock(return_value={
        "data": [],
        "pagination": {"page": 1, "size": 20, "total": 0, "total_pages": 0},
    })

    mock_autonomy = MagicMock()

    configure_agent_endpoints(
        approval_queue_service=mock_approval,
        activity_log_service=mock_activity,
        autonomy_config_service=mock_autonomy,
        memory_service=mock_memory,
        feedback_service=mock_feedback,
    )

    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = "req-test"
            return await call_next(request)

    app.add_middleware(FakeRequestID)
    app.include_router(agent_router)
    return TestClient(app)


AGENT_PAGINATED_PATHS = [
    "/api/agent/approvals",
    "/api/agent/activity",
    "/api/agent/memory",
    "/api/agent/feedback",
]


@pytest.mark.parametrize("path", AGENT_PAGINATED_PATHS)
def test_agent_paginated_response_conformance(agent_client, path):
    """Agent paginated endpoints return PaginatedResponse-conforming JSON."""
    resp = agent_client.get(path)
    assert resp.status_code == 200
    body = resp.json()

    assert UNIFIED_FIELDS.issubset(set(body.keys())), (
        f"Missing unified fields in {path}: {UNIFIED_FIELDS - set(body.keys())}"
    )
    assert DEPRECATED_FIELDS.issubset(set(body.keys()))
    assert isinstance(body["items"], list)
    assert isinstance(body["total"], int)
    assert isinstance(body["has_next"], bool)
    PaginatedResponse.model_validate({
        "items": body["items"],
        "total": body["total"],
        "page": body["page"],
        "page_size": body["page_size"],
        "has_next": body["has_next"],
    })
