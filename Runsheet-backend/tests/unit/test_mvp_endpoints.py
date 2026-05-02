"""
Unit tests for the Fuel Distribution MVP REST endpoints.

Tests cover:
- POST /api/fuel/mvp/plan/generate (Req 8.1)
- GET /api/fuel/mvp/plan/{plan_id} (Req 8.2)
- POST /api/fuel/mvp/plan/{plan_id}/replan (Req 8.3)
- GET /api/fuel/mvp/forecasts (Req 8.4)
- GET /api/fuel/mvp/priorities (Req 8.5)
- Paginated response format (Req 8.6)
- Service wiring via configure_mvp_endpoints()

Requirements: 8.1–8.6
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from Agents.support.mvp_endpoints import (
    configure_mvp_endpoints,
    router,
    GeneratePlanResponse,
    ReplanRequest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_pipeline():
    """Create a mock pipeline service."""
    pipeline = MagicMock()
    pipeline.run = AsyncMock(return_value="run_test_123")
    pipeline.get_status = AsyncMock(return_value={
        "run_id": "run_test_123",
        "tenant_id": "tenant-1",
        "state": "complete",
        "started_at": "2024-01-01T00:00:00+00:00",
        "completed_at": "2024-01-01T00:01:00+00:00",
        "failed_agent": None,
        "error_message": None,
    })
    return pipeline


def _make_mock_es():
    """Create a mock ES service."""
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={
        "hits": {
            "hits": [],
            "total": {"value": 0},
        }
    })
    return es


def _make_mock_replanning_agent():
    """Create a mock exception replanning agent."""
    agent = MagicMock()
    agent._on_signal = AsyncMock()
    agent.monitor_cycle = AsyncMock(return_value=([], []))
    return agent


def _create_test_app(pipeline=None, es_service=None, replanning_agent=None):
    """Create a FastAPI test app with MVP endpoints configured."""
    app = FastAPI()
    app.include_router(router)

    if pipeline is None:
        pipeline = _make_mock_pipeline()
    if es_service is None:
        es_service = _make_mock_es()

    configure_mvp_endpoints(
        pipeline=pipeline,
        es_service=es_service,
        exception_replanning_agent=replanning_agent,
    )

    return app, pipeline, es_service


# ---------------------------------------------------------------------------
# Tests: POST /api/fuel/mvp/plan/generate (Req 8.1)
# ---------------------------------------------------------------------------


class TestGeneratePlan:
    def test_returns_run_id_and_status(self):
        app, pipeline, _ = _create_test_app()
        client = TestClient(app)

        resp = client.post("/api/fuel/mvp/plan/generate?tenant_id=tenant-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == "run_test_123"
        assert data["status"] == "complete"

    def test_calls_pipeline_run(self):
        app, pipeline, _ = _create_test_app()
        client = TestClient(app)

        client.post("/api/fuel/mvp/plan/generate?tenant_id=tenant-1")
        pipeline.run.assert_called_once_with(tenant_id="tenant-1")

    def test_requires_tenant_id(self):
        app, _, _ = _create_test_app()
        client = TestClient(app)

        resp = client.post("/api/fuel/mvp/plan/generate")
        assert resp.status_code == 422  # Missing required query param

    def test_handles_pipeline_error(self):
        pipeline = _make_mock_pipeline()
        pipeline.run = AsyncMock(side_effect=RuntimeError("pipeline error"))
        app, _, _ = _create_test_app(pipeline=pipeline)
        client = TestClient(app)

        resp = client.post("/api/fuel/mvp/plan/generate?tenant_id=tenant-1")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Tests: GET /api/fuel/mvp/plan/{plan_id} (Req 8.2)
# ---------------------------------------------------------------------------


class TestGetPlan:
    def test_returns_plan_with_loading_and_route(self):
        es = _make_mock_es()
        loading_doc = {
            "plan_id": "plan-1",
            "truck_id": "truck-1",
            "assignments": [],
            "tenant_id": "tenant-1",
        }
        route_doc = {
            "route_id": "route-1",
            "plan_id": "plan-1",
            "truck_id": "truck-1",
            "stops": [],
            "tenant_id": "tenant-1",
        }

        # First call returns loading plan, second returns route
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [{"_source": loading_doc}]}},
                {"hits": {"hits": [{"_source": route_doc}]}},
            ]
        )

        app, _, _ = _create_test_app(es_service=es)
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/plan/plan-1?tenant_id=tenant-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_id"] == "plan-1"
        assert data["loading_plan"]["truck_id"] == "truck-1"
        assert data["route_plan"]["route_id"] == "route-1"

    def test_returns_empty_plan_when_not_found(self):
        app, _, _ = _create_test_app()
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/plan/nonexistent?tenant_id=tenant-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_id"] == "nonexistent"
        assert data["loading_plan"] is None
        assert data["route_plan"] is None

    def test_returns_plan_without_route(self):
        """Loading plan exists but no route yet."""
        es = _make_mock_es()
        loading_doc = {
            "plan_id": "plan-1",
            "truck_id": "truck-1",
            "tenant_id": "tenant-1",
        }
        es.search_documents = AsyncMock(
            side_effect=[
                {"hits": {"hits": [{"_source": loading_doc}]}},
                {"hits": {"hits": []}},
            ]
        )

        app, _, _ = _create_test_app(es_service=es)
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/plan/plan-1?tenant_id=tenant-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["loading_plan"] is not None
        assert data["route_plan"] is None


# ---------------------------------------------------------------------------
# Tests: POST /api/fuel/mvp/plan/{plan_id}/replan (Req 8.3)
# ---------------------------------------------------------------------------


class TestReplan:
    def test_triggers_replanning(self):
        replanning_agent = _make_mock_replanning_agent()
        app, _, _ = _create_test_app(replanning_agent=replanning_agent)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/mvp/plan/plan-1/replan?tenant_id=tenant-1",
            json={
                "disruption_type": "truck_breakdown",
                "description": "Truck broke down",
                "entity_id": "truck-1",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_id"] == "plan-1"
        assert data["status"] == "replan_triggered"
        assert data["disruption_type"] == "truck_breakdown"

    def test_returns_503_when_agent_not_available(self):
        app, _, _ = _create_test_app(replanning_agent=None)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/mvp/plan/plan-1/replan?tenant_id=tenant-1",
            json={"disruption_type": "delay"},
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Tests: GET /api/fuel/mvp/forecasts (Req 8.4)
# ---------------------------------------------------------------------------


class TestGetForecasts:
    def test_returns_paginated_forecasts(self):
        es = _make_mock_es()
        forecast_doc = {
            "forecast_id": "f1",
            "station_id": "s1",
            "fuel_grade": "AGO",
            "runout_risk_24h": 0.8,
            "tenant_id": "tenant-1",
        }
        es.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{"_source": forecast_doc}],
                "total": {"value": 1},
            }
        })

        app, _, _ = _create_test_app(es_service=es)
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/forecasts?tenant_id=tenant-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["station_id"] == "s1"
        assert data["page"] == 1
        assert data["page_size"] == 20

    def test_supports_station_id_filter(self):
        es = _make_mock_es()
        app, _, _ = _create_test_app(es_service=es)
        client = TestClient(app)

        client.get(
            "/api/fuel/mvp/forecasts?tenant_id=tenant-1&station_id=s1"
        )

        # Verify the ES query includes station_id filter
        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must_clauses = query["query"]["bool"]["must"]
        station_filter = [c for c in must_clauses if "station_id" in str(c)]
        assert len(station_filter) == 1

    def test_supports_fuel_grade_filter(self):
        es = _make_mock_es()
        app, _, _ = _create_test_app(es_service=es)
        client = TestClient(app)

        client.get(
            "/api/fuel/mvp/forecasts?tenant_id=tenant-1&fuel_grade=AGO"
        )

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        must_clauses = query["query"]["bool"]["must"]
        grade_filter = [c for c in must_clauses if "fuel_grade" in str(c)]
        assert len(grade_filter) == 1

    def test_supports_pagination(self):
        es = _make_mock_es()
        app, _, _ = _create_test_app(es_service=es)
        client = TestClient(app)

        resp = client.get(
            "/api/fuel/mvp/forecasts?tenant_id=tenant-1&page=2&size=10"
        )
        assert resp.status_code == 200

        call_args = es.search_documents.call_args
        query = call_args[0][1]
        assert query["from"] == 10  # (page-1) * size
        assert query["size"] == 10


# ---------------------------------------------------------------------------
# Tests: GET /api/fuel/mvp/priorities (Req 8.5)
# ---------------------------------------------------------------------------


class TestGetPriorities:
    def test_returns_paginated_priorities(self):
        es = _make_mock_es()
        priority_doc = {
            "priority_list_id": "pl1",
            "priorities": [],
            "tenant_id": "tenant-1",
        }
        es.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{"_source": priority_doc}],
                "total": {"value": 1},
            }
        })

        app, _, _ = _create_test_app(es_service=es)
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/priorities?tenant_id=tenant-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    def test_requires_tenant_id(self):
        app, _, _ = _create_test_app()
        client = TestClient(app)

        resp = client.get("/api/fuel/mvp/priorities")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: Response models
# ---------------------------------------------------------------------------


class TestResponseModels:
    def test_generate_plan_response(self):
        resp = GeneratePlanResponse(run_id="run-1", status="complete")
        assert resp.run_id == "run-1"
        assert resp.status == "complete"

    def test_replan_request_defaults(self):
        req = ReplanRequest()
        assert req.disruption_type == "delay"
        assert req.description == ""
        assert req.entity_id == ""

    def test_replan_request_custom(self):
        req = ReplanRequest(
            disruption_type="truck_breakdown",
            description="Engine failure",
            entity_id="truck-1",
        )
        assert req.disruption_type == "truck_breakdown"
        assert req.entity_id == "truck-1"
