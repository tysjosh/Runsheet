"""Dispatcher read API for driver messages and exceptions (G1, D13).

``GET /api/scheduling/jobs/{job_id}/driver-activity`` and
``GET /api/ops/drivers/{driver_id}/activity`` give admins and dispatchers a
read of what drivers posted to the existing ``job_messages`` and
``driver_exceptions`` stores. Before them there was no read path at all.

These tests drive the real routers and the real ``DriverActivityService``
over a recording search fake. Tenant isolation and the filters against the
real query translator are covered in
``tests/postgres/test_driver_activity_service.py``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errors.exceptions import resource_not_found
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "t1"


def _message(mid: str, ts: str, *, job_id="JOB_1", sender_id="DRV-1", tenant=TENANT):
    return {
        "message_id": mid,
        "job_id": job_id,
        "sender_id": sender_id,
        "sender_role": "driver",
        "body": f"msg {mid}",
        "timestamp": ts,
        "tenant_id": tenant,
    }


def _exception(eid: str, ts: str, *, job_id="JOB_1", driver_id="DRV-1", tenant=TENANT):
    return {
        "exception_id": eid,
        "job_id": job_id,
        "order_id": None,
        "driver_id": driver_id,
        "exception_type": "road_closure",
        "severity": "high",
        "note": f"note {eid}",
        "timestamp": ts,
        "tenant_id": tenant,
    }


class _RecordingES:
    """search_documents returns the seeded docs per index and records queries."""

    def __init__(self, by_index: Dict[str, List[dict]]):
        self.by_index = by_index
        self.calls: List[tuple] = []

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 100, **kw):
        self.calls.append((index, query, size))
        docs = sorted(
            self.by_index.get(index, []), key=lambda d: d["timestamp"], reverse=True
        )
        limit = int(query.get("size", size))
        return {
            "hits": {
                "total": {"value": len(docs), "relation": "eq"},
                "hits": [{"_id": "x", "_source": d} for d in docs[:limit]],
            }
        }


class _FakeDriverRepo:
    def __init__(self, known: Dict[str, str]):
        self._known = known  # driver_id -> tenant_id

    async def get(self, tenant_id: str, driver_id: str):
        if self._known.get(driver_id) == tenant_id:
            return MagicMock(driver_id=driver_id, tenant_id=tenant_id)
        return None


def _make_app(es: _RecordingES, *, known_jobs=("JOB_1",), known_drivers=None) -> FastAPI:
    from driver.services.driver_activity_service import DriverActivityService
    from errors.handlers import register_exception_handlers
    from fuel.api import driver_endpoints as fuel_driver_endpoints
    from scheduling.api import endpoints as scheduling_endpoints

    job_service = MagicMock()

    async def _get_job_doc(job_id, tenant_id):
        if job_id in known_jobs and tenant_id == TENANT:
            return {"job_id": job_id, "tenant_id": tenant_id}
        raise resource_not_found(f"Job '{job_id}' not found", details={"job_id": job_id})

    job_service._get_job_doc = AsyncMock(side_effect=_get_job_doc)
    scheduling_endpoints.configure_scheduling_api(
        job_service=job_service, cargo_service=MagicMock(), delay_service=MagicMock()
    )
    fuel_driver_endpoints.configure_driver_endpoints(
        driver_repository=_FakeDriverRepo(known_drivers or {"DRV-1": TENANT})
    )
    service = DriverActivityService(es)
    scheduling_endpoints.set_driver_activity_service(service)
    fuel_driver_endpoints.set_driver_activity_service(service)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(scheduling_endpoints.router)
    app.include_router(fuel_driver_endpoints.router)
    install_test_auth(app)
    return app


def _seeded_es() -> _RecordingES:
    return _RecordingES(
        {
            "job_messages": [
                _message("m1", "2026-10-01T10:00:00+00:00"),
                _message("m2", "2026-10-01T12:00:00+00:00"),
            ],
            "driver_exceptions": [
                _exception("e1", "2026-10-01T11:00:00+00:00"),
            ],
        }
    )


_JOB_URL = "/api/scheduling/jobs/JOB_1/driver-activity"
_DRIVER_URL = "/api/ops/drivers/DRV-1/activity"


# ---------------------------------------------------------------------------
# Role gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [_JOB_URL, _DRIVER_URL])
def test_driver_role_is_403(url):
    es = _seeded_es()
    client = TestClient(_make_app(es))

    resp = client.get(
        url, headers=auth_headers(TENANT, roles=["driver"], driver_id="DRV-1")
    )

    assert resp.status_code == 403
    assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"
    assert es.calls == []


@pytest.mark.parametrize("role", ["admin", "dispatcher"])
@pytest.mark.parametrize("url", [_JOB_URL, _DRIVER_URL])
def test_admin_and_dispatcher_get_200(url, role):
    client = TestClient(_make_app(_seeded_es()))

    resp = client.get(url, headers=auth_headers(TENANT, roles=[role]))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["id"] for row in body["data"]] == ["m2", "e1", "m1"]
    assert body["pagination"]["total"] == 3
    assert "request_id" in body


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


def test_rows_are_normalized():
    client = TestClient(_make_app(_seeded_es()))

    rows = client.get(_JOB_URL, headers=auth_headers(TENANT)).json()["data"]

    by_id = {row["id"]: row for row in rows}
    assert by_id["m2"] == {
        "id": "m2",
        "type": "message",
        "timestamp": "2026-10-01T12:00:00+00:00",
        "driver_id": "DRV-1",
        "sender_role": "driver",
        "job_id": "JOB_1",
        "order_id": None,
        "text": "msg m2",
    }
    assert by_id["e1"] == {
        "id": "e1",
        "type": "exception",
        "timestamp": "2026-10-01T11:00:00+00:00",
        "driver_id": "DRV-1",
        "job_id": "JOB_1",
        "order_id": None,
        "text": "note e1",
        "exception_type": "road_closure",
        "severity": "high",
    }


# ---------------------------------------------------------------------------
# 404s
# ---------------------------------------------------------------------------


def test_unknown_or_other_tenant_job_is_404():
    es = _seeded_es()
    client = TestClient(_make_app(es))

    resp = client.get(
        "/api/scheduling/jobs/JOB_OTHER/driver-activity", headers=auth_headers(TENANT)
    )

    assert resp.status_code == 404
    assert es.calls == []


def test_unknown_or_other_tenant_driver_is_404():
    es = _seeded_es()
    client = TestClient(_make_app(es, known_drivers={"DRV-B": "t2"}))

    resp = client.get("/api/ops/drivers/DRV-B/activity", headers=auth_headers(TENANT))

    assert resp.status_code == 404
    assert es.calls == []


# ---------------------------------------------------------------------------
# Query params
# ---------------------------------------------------------------------------


def test_type_filter_queries_only_that_store():
    es = _seeded_es()
    client = TestClient(_make_app(es))

    resp = client.get(_JOB_URL + "?type=exception", headers=auth_headers(TENANT))

    assert resp.status_code == 200
    assert [row["id"] for row in resp.json()["data"]] == ["e1"]
    assert [index for index, _, _ in es.calls] == ["driver_exceptions"]


def test_date_range_is_sent_as_a_utc_range():
    es = _seeded_es()
    client = TestClient(_make_app(es))

    resp = client.get(
        _JOB_URL,
        params={"start_date": "2026-10-01T10:30:00Z", "end_date": "2026-10-01T11:30:00Z"},
        headers=auth_headers(TENANT),
    )

    assert resp.status_code == 200
    for _, query, _ in es.calls:
        text = json.dumps(query)
        assert '"gte": "2026-10-01T10:30:00+00:00"' in text
        assert '"lte": "2026-10-01T11:30:00+00:00"' in text


def test_every_query_is_tenant_filtered_and_job_scoped():
    es = _seeded_es()
    TestClient(_make_app(es)).get(_JOB_URL, headers=auth_headers(TENANT))

    assert {index for index, _, _ in es.calls} == {"job_messages", "driver_exceptions"}
    for _, query, _ in es.calls:
        text = json.dumps(query)
        assert '{"term": {"tenant_id": "t1"}}' in text
        assert '{"term": {"job_id": "JOB_1"}}' in text


def test_driver_query_matches_driver_id_or_sender_id():
    es = _seeded_es()
    TestClient(_make_app(es)).get(_DRIVER_URL, headers=auth_headers(TENANT))

    queries = {index: json.dumps(query) for index, query, _ in es.calls}
    assert '{"term": {"sender_id": "DRV-1"}}' in queries["job_messages"]
    assert '{"term": {"driver_id": "DRV-1"}}' in queries["job_messages"]
    assert '{"term": {"driver_id": "DRV-1"}}' in queries["driver_exceptions"]


def test_rows_from_another_tenant_are_dropped():
    """A backend that ignored the filter still can't leak another tenant's rows."""
    es = _RecordingES(
        {
            "job_messages": [
                _message("m1", "2026-10-01T10:00:00+00:00"),
                _message("mB", "2026-10-01T13:00:00+00:00", tenant="t2"),
            ],
            "driver_exceptions": [],
        }
    )
    rows = TestClient(_make_app(es)).get(_JOB_URL, headers=auth_headers(TENANT)).json()["data"]

    assert [row["id"] for row in rows] == ["m1"]


def test_pagination_slices_the_merged_timeline():
    es = _seeded_es()
    client = TestClient(_make_app(es))

    page1 = client.get(_JOB_URL + "?page=1&size=2", headers=auth_headers(TENANT)).json()
    page2 = client.get(_JOB_URL + "?page=2&size=2", headers=auth_headers(TENANT)).json()

    assert [row["id"] for row in page1["data"]] == ["m2", "e1"]
    assert [row["id"] for row in page2["data"]] == ["m1"]
    assert page1["pagination"] == {"page": 1, "size": 2, "total": 3, "total_pages": 2}
    assert page2["has_next"] is False
    # Each store is asked for page*size rows so the merge is correct.
    assert {size for _, _, size in es.calls} == {2, 4}


@pytest.mark.parametrize(
    "query",
    [
        "type=pod",
        "start_date=notadate",
        "end_date=yesterday",
        "page=0",
        "size=0",
        "size=101",
    ],
)
@pytest.mark.parametrize("url", [_JOB_URL, _DRIVER_URL])
def test_bad_params_are_422(url, query):
    es = _seeded_es()
    resp = TestClient(_make_app(es)).get(f"{url}?{query}", headers=auth_headers(TENANT))

    assert resp.status_code == 422
    assert es.calls == []


def test_page_past_the_window_is_422():
    es = _seeded_es()
    resp = TestClient(_make_app(es)).get(
        _JOB_URL + "?page=11&size=100", headers=auth_headers(TENANT)
    )

    assert resp.status_code == 422
    assert es.calls == []
