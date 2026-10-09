"""F11: a date-only ``end_date`` on the scheduling metrics endpoints covers
the whole (UTC) day.

Before the fix, ``start_date=2026-10-06&end_date=2026-10-06`` returned 0 jobs
on staging although JOB_33 was scheduled at 2026-10-06T13:00Z, because the
date-only end meant midnight at the start of the day.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from scheduling.api.endpoints import (
    _normalize_range,
    configure_scheduling_api,
    router as scheduling_router,
)
from scheduling.services.cargo_service import CargoService
from scheduling.services.delay_detection_service import DelayDetectionService
from scheduling.services.job_service import JobService
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "t1"

JOB_33 = {
    "job_id": "JOB_33", "tenant_id": TENANT, "job_type": "fuel_delivery",
    "status": "completed", "scheduled_time": "2026-10-06T13:00:00Z",
    "started_at": "2026-10-06T13:05:00Z", "completed_at": "2026-10-06T14:00:00Z",
    "asset_assigned": "TRUCK-01",
}
JOB_NEXT_DAY = {**JOB_33, "job_id": "JOB_34", "scheduled_time": "2026-10-07T00:00:00Z"}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fake_pg(jobs: List[Dict[str, Any]], calls: List[Dict[str, Any]]):
    """Read-cutover fetch that applies the range like the Postgres path."""

    async def _fetch(aggregate_type, tenant_id, *, range_field=None, range_gte=None,
                     range_lte=None, **kw):
        calls.append({"gte": range_gte, "lte": range_lte, **kw})
        out = []
        for j in jobs:
            t = _parse(j[range_field]) if range_field else None
            if range_gte and t < _parse(range_gte):
                continue
            if range_lte and t > _parse(range_lte):
                continue
            if kw.get("bool_filters", {}).get("delayed") and not j.get("delayed"):
                continue
            out.append(j)
        return out

    return patch(
        "commerce.services.commerce_persistence_bridge.read_hybrid_fetch_for_aggregation",
        _fetch,
    )


@pytest.fixture
def client():
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": [], "total": {"value": 0}}})
    with patch("scheduling.services.job_service.get_settings") as settings:
        settings.return_value = MagicMock(scheduling_default_eta_hours=4)
        job_svc = JobService(es_service=es, redis_url=None)
    configure_scheduling_api(
        job_service=job_svc,
        cargo_service=CargoService(es_service=es),
        delay_service=DelayDetectionService(es_service=es, ws_manager=None),
    )
    app = FastAPI()
    app.include_router(scheduling_router)

    @app.exception_handler(AppException)
    async def _handler(request: Request, exc: AppException):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    install_test_auth(app)
    c = TestClient(app)
    c.es = es
    return c


def _get(client, path, **params):
    return client.get(path, params=params, headers=auth_headers(TENANT, sub="user-1"))


def test_normalize_range_rules():
    assert _normalize_range("2026-10-06", "2026-10-06") == (
        "2026-10-06T00:00:00+00:00", "2026-10-06T23:59:59.999999+00:00",
    )
    assert _normalize_range("2026-10-06T05:00:00Z", "2026-10-06T06:00:00Z") == (
        "2026-10-06T05:00:00Z", "2026-10-06T06:00:00Z",
    )
    assert _normalize_range(None, None) == (None, None)


@pytest.mark.parametrize(
    "path", ["/api/scheduling/metrics/jobs", "/api/scheduling/metrics/completion"]
)
def test_same_day_range_includes_a_job_that_day(client, path):
    calls: List[Dict[str, Any]] = []
    with _fake_pg([JOB_33, JOB_NEXT_DAY], calls):
        resp = _get(client, path, start_date="2026-10-06", end_date="2026-10-06", bucket="daily")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert calls[0]["lte"] == "2026-10-06T23:59:59.999999+00:00"
    if path.endswith("/jobs"):
        assert body["end_date"] == "2026-10-06T23:59:59.999999+00:00"
        assert sum(b["total"] for b in body["data"]) == 1  # JOB_33, not JOB_34
    else:
        (row,) = body["data"]
        assert row["total"] == 1


def test_full_datetime_end_is_untouched(client):
    calls: List[Dict[str, Any]] = []
    with _fake_pg([JOB_33], calls):
        resp = _get(client, "/api/scheduling/metrics/jobs",
                    start_date="2026-10-06T00:00:00Z", end_date="2026-10-06T12:00:00Z")
    assert resp.status_code == 200
    assert calls[0]["lte"] == "2026-10-06T12:00:00Z"
    assert sum(b["total"] for b in resp.json()["data"]) == 0


def test_es_path_uses_the_inclusive_end(client):
    with patch(
        "commerce.services.commerce_persistence_bridge.read_hybrid_fetch_for_aggregation",
        AsyncMock(side_effect=lambda *a, **k: __import__(
            "commerce.services.commerce_persistence_bridge", fromlist=["_NOT_CUT_OVER"]
        )._NOT_CUT_OVER),
    ):
        resp = _get(client, "/api/scheduling/metrics/jobs",
                    start_date="2026-10-06", end_date="2026-10-06")
    assert resp.status_code == 200, resp.text
    query = client.es.search_documents.await_args.args[1]
    ranges = [c["range"] for c in query["query"]["bool"]["must"] if "range" in c]
    assert ranges == [{"scheduled_time": {
        "gte": "2026-10-06T00:00:00+00:00", "lte": "2026-10-06T23:59:59.999999+00:00",
    }}]


def test_delays_endpoint_passes_the_inclusive_end(client):
    calls: List[Dict[str, Any]] = []
    with _fake_pg([], calls):
        resp = _get(client, "/api/scheduling/metrics/delays",
                    start_date="2026-10-06", end_date="2026-10-06")
    assert resp.status_code == 200, resp.text
    assert calls[0]["lte"] == "2026-10-06T23:59:59.999999+00:00"
