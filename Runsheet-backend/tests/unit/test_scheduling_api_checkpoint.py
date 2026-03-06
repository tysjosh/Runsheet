"""
Checkpoint 7: Verify API layer.

Focused verification tests for the scheduling API layer Definition of Done:
- All endpoints return correct JSON envelope {data, pagination, request_id}
- Tenant isolation: query with tenant_id=A returns zero documents belonging to tenant_id=B
- Filter combinations (job_type + status + date range) return correct subsets
- Pagination: total_pages matches ceil(total / size)
- Rate limiting: 101st request within 1 minute returns 429 (verified structurally)
- Invalid filter values return 400 with structured error
- POST /scheduling/jobs returns 201 with job_id
- PATCH /scheduling/jobs/{id}/assign with busy asset returns 409

Most criteria are already covered by test_scheduling_api_endpoints.py and
test_scheduling_tenant_scoping.py. This file adds the remaining gaps:
1. PATCH assign with busy asset → 409
2. Cross-tenant isolation at the API level (tenant A sees zero tenant B docs)
3. Invalid filter values return 400 with structured error body
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
from errors.codes import ErrorCode
from scheduling.api.endpoints import router as scheduling_router, configure_scheduling_api
from scheduling.services.cargo_service import CargoService
from scheduling.services.delay_detection_service import DelayDetectionService
from scheduling.services.job_service import JobService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JWT_SECRET = "test-jwt-secret"
JWT_ALGORITHM = "HS256"
TENANT_A = "tenant-alpha"
TENANT_B = "tenant-beta"

_SETTINGS_PATCH = patch(
    "ops.middleware.tenant_guard.get_settings",
    return_value=MagicMock(jwt_secret=JWT_SECRET, jwt_algorithm=JWT_ALGORITHM),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(tenant_id: str, sub: str = "user-1") -> str:
    return jwt.encode(
        {"tenant_id": tenant_id, "sub": sub}, JWT_SECRET, algorithm=JWT_ALGORITHM
    )


def _auth_headers(tenant_id: str = TENANT_A) -> dict:
    return {"Authorization": f"Bearer {_make_token(tenant_id)}"}


def _make_es_mock() -> MagicMock:
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    es.update_document = AsyncMock(return_value={"result": "updated"})
    return es


def _make_job_service(es_mock: MagicMock) -> JobService:
    with patch("scheduling.services.job_service.get_settings") as mock_settings:
        settings_obj = MagicMock()
        settings_obj.scheduling_default_eta_hours = 4
        mock_settings.return_value = settings_obj
        svc = JobService(es_service=es_mock, redis_url=None)
    svc._id_gen = MagicMock()
    svc._id_gen.next_id = AsyncMock(return_value="JOB_1")
    return svc


def _make_cargo_service(es_mock: MagicMock) -> CargoService:
    return CargoService(es_service=es_mock)


def _make_delay_service(es_mock: MagicMock) -> DelayDetectionService:
    return DelayDetectionService(es_service=es_mock, ws_manager=None)


def _build_app(es_mock: MagicMock) -> tuple[FastAPI, TestClient]:
    app = FastAPI()
    job_svc = _make_job_service(es_mock)
    cargo_svc = _make_cargo_service(es_mock)
    delay_svc = _make_delay_service(es_mock)
    configure_scheduling_api(
        job_service=job_svc, cargo_service=cargo_svc, delay_service=delay_svc
    )
    app.include_router(scheduling_router)

    @app.exception_handler(AppException)
    async def _handler(request: Request, exc: AppException):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    client = TestClient(app)
    return app, client


def _job_hit(job_id: str = "JOB_1", tenant_id: str = TENANT_A, **overrides) -> dict:
    doc = {
        "job_id": job_id,
        "job_type": "cargo_transport",
        "status": "scheduled",
        "tenant_id": tenant_id,
        "origin": "Port Harcourt",
        "destination": "Lagos",
        "scheduled_time": "2026-03-12T10:00:00Z",
        "created_at": "2026-03-12T09:00:00Z",
        "updated_at": "2026-03-12T09:00:00Z",
        "priority": "normal",
        "delayed": False,
        "asset_assigned": None,
        "estimated_arrival": None,
        "started_at": None,
        "completed_at": None,
        "created_by": "user-1",
        "delay_duration_minutes": None,
        "failure_reason": None,
        "notes": None,
        "cargo_manifest": [
            {
                "item_id": "ITEM_1",
                "description": "Steel pipes",
                "weight_kg": 500.0,
                "container_number": None,
                "seal_number": None,
                "item_status": "pending",
            }
        ],
    }
    doc.update(overrides)
    return {"_source": doc}


def _es_response(hits: list[dict], total: int | None = None) -> dict:
    return {
        "hits": {
            "hits": hits,
            "total": {"value": total if total is not None else len(hits)},
        }
    }


# ---------------------------------------------------------------------------
# Test: PATCH /scheduling/jobs/{id}/assign with busy asset returns 409
# Validates: Checkpoint DoD item 8
# ---------------------------------------------------------------------------


class TestAssignBusyAssetReturns409:
    """Assigning an asset that is already active on another job returns 409."""

    def test_assign_busy_asset_returns_409(self):
        """PATCH /scheduling/jobs/{id}/assign with a busy asset returns 409."""
        es = _make_es_mock()

        # Call sequence:
        # 1. _get_job_doc: find the job (status=scheduled)
        # 2. _verify_asset_compatible: find the asset in trucks index
        # 3. _check_asset_availability: find conflicting active job → 409
        job_doc_response = _es_response(
            [_job_hit("JOB_1", status="scheduled")]
        )
        asset_response = _es_response(
            [{"_source": {"truck_id": "TRUCK_001", "asset_type": "vehicle"}}]
        )
        conflict_response = _es_response(
            [_job_hit("JOB_99", status="in_progress", asset_assigned="TRUCK_001")],
            total=1,
        )

        es.search_documents = AsyncMock(
            side_effect=[job_doc_response, asset_response, conflict_response]
        )

        _, client = _build_app(es)

        with _SETTINGS_PATCH:
            resp = client.patch(
                "/scheduling/jobs/JOB_1/assign",
                headers=_auth_headers(),
                json={"asset_id": "TRUCK_001"},
            )

        assert resp.status_code == 409
        body = resp.json()
        assert "message" in body
        assert "TRUCK_001" in body["message"]

    def test_assign_available_asset_returns_200(self):
        """PATCH /scheduling/jobs/{id}/assign with an available asset succeeds."""
        es = _make_es_mock()

        # Call sequence:
        # 1. _get_job_doc: find the job (status=scheduled)
        # 2. _verify_asset_compatible: find the asset
        # 3. _check_asset_availability: no conflicts
        # 4. update_document: update job
        # 5. index_document: append event
        job_doc_response = _es_response(
            [_job_hit("JOB_1", status="scheduled")]
        )
        asset_response = _es_response(
            [{"_source": {"truck_id": "TRUCK_002", "asset_type": "vehicle"}}]
        )
        no_conflict_response = _es_response([], total=0)

        es.search_documents = AsyncMock(
            side_effect=[job_doc_response, asset_response, no_conflict_response]
        )

        _, client = _build_app(es)

        with _SETTINGS_PATCH:
            resp = client.patch(
                "/scheduling/jobs/JOB_1/assign",
                headers=_auth_headers(),
                json={"asset_id": "TRUCK_002"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "request_id" in body
        assert body["data"]["status"] == "assigned"
        assert body["data"]["asset_assigned"] == "TRUCK_002"


# ---------------------------------------------------------------------------
# Test: Tenant isolation at API level
# Validates: Checkpoint DoD item 2
# ---------------------------------------------------------------------------


class TestTenantIsolationAtAPILevel:
    """Query with tenant_id=A returns zero documents belonging to tenant_id=B."""

    def test_tenant_a_query_returns_zero_tenant_b_docs(self):
        """GET /scheduling/jobs as tenant A returns only tenant A docs."""
        es = _make_es_mock()
        # ES returns only tenant A docs (the service filters by tenant)
        es.search_documents = AsyncMock(
            return_value=_es_response(
                [_job_hit("JOB_1", tenant_id=TENANT_A)], total=1
            )
        )
        _, client = _build_app(es)

        with _SETTINGS_PATCH:
            resp = client.get("/scheduling/jobs", headers=_auth_headers(TENANT_A))

        assert resp.status_code == 200
        body = resp.json()
        # Verify all returned docs belong to tenant A
        for job in body["data"]:
            assert job["tenant_id"] == TENANT_A

        # Verify the ES query included tenant_id=TENANT_A filter
        call_args = es.search_documents.call_args
        query_body = call_args[0][1]
        must_clauses = query_body["query"]["bool"]["must"]
        tenant_filters = [
            c["term"]["tenant_id"]
            for c in must_clauses
            if "term" in c and "tenant_id" in c["term"]
        ]
        assert TENANT_A in tenant_filters

    def test_tenant_b_cannot_see_tenant_a_jobs(self):
        """GET /scheduling/jobs as tenant B with empty result confirms isolation."""
        es = _make_es_mock()
        # ES returns empty for tenant B (no tenant A docs leak)
        es.search_documents = AsyncMock(
            return_value=_es_response([], total=0)
        )
        _, client = _build_app(es)

        with _SETTINGS_PATCH:
            resp = client.get("/scheduling/jobs", headers=_auth_headers(TENANT_B))

        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        assert body["pagination"]["total"] == 0

        # Verify the ES query used tenant B's id
        call_args = es.search_documents.call_args
        query_body = call_args[0][1]
        must_clauses = query_body["query"]["bool"]["must"]
        tenant_filters = [
            c["term"]["tenant_id"]
            for c in must_clauses
            if "term" in c and "tenant_id" in c["term"]
        ]
        assert TENANT_B in tenant_filters


# ---------------------------------------------------------------------------
# Test: Invalid filter values return 400 with structured error
# Validates: Checkpoint DoD item 6
# ---------------------------------------------------------------------------


class TestInvalidFilterValuesReturn400:
    """Invalid filter values return 400 with structured error body."""

    def test_invalid_job_type_filter_returns_400(self):
        """GET /scheduling/jobs?job_type=invalid returns 400."""
        es = _make_es_mock()
        _, client = _build_app(es)

        with _SETTINGS_PATCH:
            resp = client.get(
                "/scheduling/jobs?job_type=nonexistent_type",
                headers=_auth_headers(),
            )

        assert resp.status_code == 400
        body = resp.json()
        assert "error_code" in body
        assert "message" in body

    def test_invalid_status_filter_returns_400(self):
        """GET /scheduling/jobs?status=invalid returns 400."""
        es = _make_es_mock()
        _, client = _build_app(es)

        with _SETTINGS_PATCH:
            resp = client.get(
                "/scheduling/jobs?status=nonexistent_status",
                headers=_auth_headers(),
            )

        assert resp.status_code == 400
        body = resp.json()
        assert "error_code" in body
        assert "message" in body

    def test_invalid_sort_order_returns_400(self):
        """GET /scheduling/jobs?sort_order=invalid returns 400."""
        es = _make_es_mock()
        _, client = _build_app(es)

        with _SETTINGS_PATCH:
            resp = client.get(
                "/scheduling/jobs?sort_order=sideways",
                headers=_auth_headers(),
            )

        assert resp.status_code == 400
        body = resp.json()
        assert "error_code" in body
        assert "message" in body

    def test_invalid_metrics_bucket_returns_400(self):
        """GET /scheduling/metrics/jobs?bucket=weekly returns 400."""
        es = _make_es_mock()
        _, client = _build_app(es)

        with _SETTINGS_PATCH:
            resp = client.get(
                "/scheduling/metrics/jobs?bucket=weekly",
                headers=_auth_headers(),
            )

        assert resp.status_code == 400
        body = resp.json()
        assert "error_code" in body
        assert "message" in body
