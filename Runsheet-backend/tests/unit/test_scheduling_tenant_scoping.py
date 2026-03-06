"""
Unit tests for tenant scoping in the scheduling module.

Tests cover:
- All query methods include tenant_id filter in ES queries
- Requests without valid tenant_id return 403
- tenant_id from query params is ignored (JWT is authoritative)
- Job creation sets tenant_id from JWT context, not from request body

Requirements: 8.1-8.5
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt

# ---------------------------------------------------------------------------
# Patch ElasticsearchService singleton BEFORE any scheduling imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from scheduling.models import (
    CargoItem,
    CreateJob,
    JobType,
    JobStatus,
)
from scheduling.services.job_service import JobService
from scheduling.services.cargo_service import CargoService
from scheduling.services.delay_detection_service import DelayDetectionService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_A = "tenant-alpha"
TENANT_B = "tenant-beta"
JWT_SECRET = "test-jwt-secret"
JWT_ALGORITHM = "HS256"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_es_mock() -> MagicMock:
    """Return a mock ElasticsearchService with default async methods."""
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    es.update_document = AsyncMock(return_value={"result": "updated"})
    return es


def _make_job_service(es_mock: MagicMock) -> JobService:
    """Create a JobService with mocked dependencies."""
    with patch("scheduling.services.job_service.get_settings") as mock_settings:
        settings_obj = MagicMock()
        settings_obj.scheduling_default_eta_hours = 4
        mock_settings.return_value = settings_obj
        svc = JobService(es_service=es_mock, redis_url=None)
    svc._id_gen = MagicMock()
    svc._id_gen.next_id = AsyncMock(return_value="JOB_1")
    return svc


def _make_cargo_service(es_mock: MagicMock) -> CargoService:
    """Create a CargoService with mocked dependencies."""
    return CargoService(es_service=es_mock)


def _make_delay_service(es_mock: MagicMock) -> DelayDetectionService:
    """Create a DelayDetectionService with mocked dependencies."""
    return DelayDetectionService(es_service=es_mock, ws_manager=None)


def _make_token(claims: dict) -> str:
    """Create a signed JWT with the given claims."""
    return jwt.encode(claims, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _extract_tenant_from_query(es_mock: MagicMock) -> str | None:
    """Extract the tenant_id term filter from the last search_documents call."""
    call_args = es_mock.search_documents.call_args
    if call_args is None:
        return None
    query_body = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("body", {})
    must_clauses = query_body.get("query", {}).get("bool", {}).get("must", [])
    for clause in must_clauses:
        if "term" in clause and "tenant_id" in clause["term"]:
            return clause["term"]["tenant_id"]
    return None


def _valid_cargo_payload() -> CreateJob:
    """Return a valid cargo_transport CreateJob."""
    return CreateJob(
        job_type=JobType.CARGO_TRANSPORT,
        origin="Port Harcourt",
        destination="Lagos",
        scheduled_time="2026-03-12T10:00:00Z",
        cargo_manifest=[
            CargoItem(description="Steel pipes", weight_kg=500.0),
        ],
    )


# ---------------------------------------------------------------------------
# Test: All query methods include tenant_id filter
# Validates: Requirements 8.1, 8.3
# ---------------------------------------------------------------------------


class TestQueryMethodsTenantFilter:
    """Verify every query method injects a tenant_id term filter into ES queries."""

    @pytest.mark.asyncio
    async def test_get_job_includes_tenant_filter(self):
        """get_job query must include tenant_id term filter."""
        es = _make_es_mock()
        job_response = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "job_id": "JOB_1",
                            "job_type": "cargo_transport",
                            "status": "scheduled",
                            "tenant_id": TENANT_A,
                            "origin": "A",
                            "destination": "B",
                            "scheduled_time": "2026-03-12T10:00:00Z",
                            "created_at": "2026-03-12T09:00:00Z",
                            "updated_at": "2026-03-12T09:00:00Z",
                            "priority": "normal",
                            "delayed": False,
                        }
                    }
                ],
                "total": {"value": 1},
            }
        }
        events_response = {
            "hits": {"hits": [], "total": {"value": 0}}
        }
        es.search_documents = AsyncMock(
            side_effect=[job_response, events_response]
        )
        svc = _make_job_service(es)
        await svc.get_job("JOB_1", TENANT_A)

        # Both calls (job + events) should include tenant_id filter
        assert es.search_documents.call_count == 2
        for call in es.search_documents.call_args_list:
            query_body = call[0][1]
            must_clauses = query_body["query"]["bool"]["must"]
            tenant_filters = [
                c for c in must_clauses
                if "term" in c and "tenant_id" in c["term"]
            ]
            assert len(tenant_filters) == 1
            assert tenant_filters[0]["term"]["tenant_id"] == TENANT_A

    @pytest.mark.asyncio
    async def test_list_jobs_includes_tenant_filter(self):
        """list_jobs query must include tenant_id term filter."""
        es = _make_es_mock()
        svc = _make_job_service(es)
        await svc.list_jobs(tenant_id=TENANT_A)

        assert _extract_tenant_from_query(es) == TENANT_A

    @pytest.mark.asyncio
    async def test_get_active_jobs_includes_tenant_filter(self):
        """get_active_jobs query must include tenant_id term filter."""
        es = _make_es_mock()
        svc = _make_job_service(es)
        await svc.get_active_jobs(TENANT_A)

        assert _extract_tenant_from_query(es) == TENANT_A

    @pytest.mark.asyncio
    async def test_get_delayed_jobs_includes_tenant_filter(self):
        """get_delayed_jobs query must include tenant_id term filter."""
        es = _make_es_mock()
        svc = _make_job_service(es)
        await svc.get_delayed_jobs(TENANT_A)

        assert _extract_tenant_from_query(es) == TENANT_A

    @pytest.mark.asyncio
    async def test_get_job_events_includes_tenant_filter(self):
        """get_job_events query must include tenant_id term filter."""
        es = _make_es_mock()
        svc = _make_job_service(es)
        await svc.get_job_events("JOB_1", TENANT_A)

        assert _extract_tenant_from_query(es) == TENANT_A

    @pytest.mark.asyncio
    async def test_cargo_get_manifest_includes_tenant_filter(self):
        """CargoService.get_cargo_manifest query must include tenant_id term filter."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "job_id": "JOB_1",
                                "job_type": "cargo_transport",
                                "status": "scheduled",
                                "tenant_id": TENANT_A,
                                "cargo_manifest": [],
                            }
                        }
                    ],
                    "total": {"value": 1},
                }
            }
        )
        svc = _make_cargo_service(es)
        await svc.get_cargo_manifest("JOB_1", TENANT_A)

        assert _extract_tenant_from_query(es) == TENANT_A

    @pytest.mark.asyncio
    async def test_cargo_search_includes_tenant_filter(self):
        """CargoService.search_cargo query must include tenant_id term filter."""
        es = _make_es_mock()
        svc = _make_cargo_service(es)
        await svc.search_cargo(tenant_id=TENANT_A, container_number="CNT-001")

        assert _extract_tenant_from_query(es) == TENANT_A

    @pytest.mark.asyncio
    async def test_delay_get_eta_includes_tenant_filter(self):
        """DelayDetectionService.get_eta query must include tenant_id term filter."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "job_id": "JOB_1",
                                "estimated_arrival": "2026-03-12T14:00:00Z",
                                "delayed": False,
                                "delay_duration_minutes": None,
                                "status": "in_progress",
                                "scheduled_time": "2026-03-12T10:00:00Z",
                            }
                        }
                    ],
                    "total": {"value": 1},
                }
            }
        )
        svc = _make_delay_service(es)
        await svc.get_eta("JOB_1", TENANT_A)

        assert _extract_tenant_from_query(es) == TENANT_A

    @pytest.mark.asyncio
    async def test_delay_metrics_includes_tenant_filter(self):
        """DelayDetectionService.get_delay_metrics query must include tenant_id term filter."""
        es = _make_es_mock()
        es.search_documents = AsyncMock(
            return_value={
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {
                    "avg_delay": {"value": None},
                    "delays_by_job_type": {"buckets": []},
                },
            }
        )
        svc = _make_delay_service(es)
        await svc.get_delay_metrics(TENANT_A)

        assert _extract_tenant_from_query(es) == TENANT_A


# ---------------------------------------------------------------------------
# Test: Requests without valid tenant_id return 403
# Validates: Requirement 8.2
# ---------------------------------------------------------------------------

_SETTINGS_PATCH = patch(
    "ops.middleware.tenant_guard.get_settings",
    return_value=MagicMock(jwt_secret=JWT_SECRET, jwt_algorithm=JWT_ALGORITHM),
)


def _build_scheduling_app(es_mock: MagicMock) -> tuple[FastAPI, TestClient]:
    """Build a minimal FastAPI app with the scheduling router and tenant guard."""
    from scheduling.api.endpoints import router as scheduling_router, configure_scheduling_api

    app = FastAPI()

    job_svc = _make_job_service(es_mock)
    cargo_svc = _make_cargo_service(es_mock)
    delay_svc = _make_delay_service(es_mock)

    configure_scheduling_api(
        job_service=job_svc,
        cargo_service=cargo_svc,
        delay_service=delay_svc,
    )

    app.include_router(scheduling_router)

    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_dict(),
        )

    client = TestClient(app)
    return app, client


class TestMissingTenantReturns403:
    """Requests without a valid JWT tenant_id are rejected with 403."""

    def test_no_auth_header_returns_403(self):
        """Request without Authorization header returns 403."""
        es = _make_es_mock()
        _, client = _build_scheduling_app(es)

        with _SETTINGS_PATCH:
            resp = client.get("/scheduling/jobs")

        assert resp.status_code == 403

    def test_invalid_jwt_returns_403(self):
        """Request with an invalid JWT returns 403."""
        es = _make_es_mock()
        _, client = _build_scheduling_app(es)

        with _SETTINGS_PATCH:
            resp = client.get(
                "/scheduling/jobs",
                headers={"Authorization": "Bearer invalid-token"},
            )

        assert resp.status_code == 403

    def test_jwt_missing_tenant_id_returns_403(self):
        """JWT without tenant_id claim returns 403."""
        es = _make_es_mock()
        _, client = _build_scheduling_app(es)
        token = _make_token({"sub": "user-1"})

        with _SETTINGS_PATCH:
            resp = client.get(
                "/scheduling/jobs",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 403

    def test_post_job_without_auth_returns_403(self):
        """POST /scheduling/jobs without auth returns 403."""
        es = _make_es_mock()
        _, client = _build_scheduling_app(es)

        with _SETTINGS_PATCH:
            resp = client.post(
                "/scheduling/jobs",
                json={
                    "job_type": "passenger_transport",
                    "origin": "A",
                    "destination": "B",
                    "scheduled_time": "2026-03-12T10:00:00Z",
                },
            )

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test: tenant_id from query params is ignored
# Validates: Requirement 8.4
# ---------------------------------------------------------------------------


class TestQueryParamTenantIgnored:
    """tenant_id in query params must be ignored; JWT claim is authoritative."""

    def test_query_param_tenant_id_ignored_on_list_jobs(self):
        """tenant_id query param does not override JWT tenant_id."""
        es = _make_es_mock()
        _, client = _build_scheduling_app(es)
        token = _make_token({"tenant_id": TENANT_A, "sub": "user-1"})

        with _SETTINGS_PATCH:
            resp = client.get(
                "/scheduling/jobs?tenant_id=spoofed-tenant",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        # Verify the ES query used the JWT tenant, not the spoofed one
        assert _extract_tenant_from_query(es) == TENANT_A

    def test_query_param_tenant_id_ignored_on_active_jobs(self):
        """tenant_id query param does not override JWT on active jobs endpoint."""
        es = _make_es_mock()
        _, client = _build_scheduling_app(es)
        token = _make_token({"tenant_id": TENANT_A, "sub": "user-1"})

        with _SETTINGS_PATCH:
            resp = client.get(
                "/scheduling/jobs/active?tenant_id=spoofed-tenant",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        assert _extract_tenant_from_query(es) == TENANT_A

    def test_query_param_tenant_id_ignored_on_delayed_jobs(self):
        """tenant_id query param does not override JWT on delayed jobs endpoint."""
        es = _make_es_mock()
        _, client = _build_scheduling_app(es)
        token = _make_token({"tenant_id": TENANT_A, "sub": "user-1"})

        with _SETTINGS_PATCH:
            resp = client.get(
                "/scheduling/jobs/delayed?tenant_id=spoofed-tenant",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        assert _extract_tenant_from_query(es) == TENANT_A


# ---------------------------------------------------------------------------
# Test: Job creation sets tenant_id from JWT context
# Validates: Requirement 8.5
# ---------------------------------------------------------------------------


class TestJobCreationTenantFromJWT:
    """Job creation must set tenant_id from the JWT context, not from the request body."""

    @pytest.mark.asyncio
    async def test_create_job_uses_jwt_tenant_id(self):
        """The indexed document must have tenant_id from the parameter, not the payload."""
        es = _make_es_mock()
        svc = _make_job_service(es)
        payload = _valid_cargo_payload()

        job = await svc.create_job(payload, tenant_id=TENANT_A, actor_id="user-1")

        assert job.tenant_id == TENANT_A

        # Verify the document indexed into ES has the correct tenant_id
        index_call = es.index_document.call_args
        indexed_doc = index_call[0][2]  # third positional arg is the document
        assert indexed_doc["tenant_id"] == TENANT_A

    @pytest.mark.asyncio
    async def test_create_job_ignores_body_tenant_id(self):
        """Even if a tenant_id were somehow in the payload, the service uses the parameter."""
        es = _make_es_mock()
        svc = _make_job_service(es)
        payload = _valid_cargo_payload()

        # Call with TENANT_A as the JWT-derived tenant
        job = await svc.create_job(payload, tenant_id=TENANT_A, actor_id="user-1")

        # The job must have TENANT_A, not anything from the payload
        assert job.tenant_id == TENANT_A

        # Verify the indexed document
        index_call = es.index_document.call_args
        indexed_doc = index_call[0][2]
        assert indexed_doc["tenant_id"] == TENANT_A

    def test_api_create_job_passes_jwt_tenant_to_service(self):
        """The API endpoint passes tenant.tenant_id from JWT to the service."""
        es = _make_es_mock()
        _, client = _build_scheduling_app(es)
        token = _make_token({"tenant_id": TENANT_A, "sub": "user-1"})

        with _SETTINGS_PATCH:
            resp = client.post(
                "/scheduling/jobs",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "job_type": "cargo_transport",
                    "origin": "Port Harcourt",
                    "destination": "Lagos",
                    "scheduled_time": "2026-03-12T10:00:00Z",
                    "cargo_manifest": [
                        {"description": "Steel pipes", "weight_kg": 500.0}
                    ],
                },
            )

        assert resp.status_code == 200 or resp.status_code == 201
        # Verify the indexed document used the JWT tenant_id
        index_call = es.index_document.call_args
        indexed_doc = index_call[0][2]
        assert indexed_doc["tenant_id"] == TENANT_A
