"""Driver hours CSV export (OI-20, owner decision 2026-10-07).

``GET /api/compliance/hos-records/daily-summary/export``: admin only, same
safeguards as the data-export v1 exports (tenant isolation, 50k cap, rate
limit, audit line, formula escaping, no driver PII).
"""

from __future__ import annotations

import csv
import io
import logging
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from compliance.api import driver_records_endpoints as dre
from driver.services.driver_es_mappings import DUTY_STATUS_EVENTS_INDEX
from errors.handlers import register_exception_handlers
from middleware.rate_limiter import _custom_rate_limit_handler, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.csv_export import MAX_EXPORT_ROWS
from tests.unit._fake_doc_store import FakeDocStore

A, B = "tenant-a", "tenant-b"
URL = "/api/compliance/hos-records/daily-summary/export"
RANGE = "start_date=2026-10-01&end_date=2026-10-01"
HEADER = [
    "date", "driver_id", "active_minutes", "on_break_minutes",
    "off_duty_minutes", "inactive_minutes", "event_count",
    "first_event_at", "last_event_at", "basis",
]


class Ctx:
    roles = ["admin"]


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def store():
    s = FakeDocStore()
    dre.configure_driver_records_api(es_service=s)
    yield s
    dre.configure_driver_records_api(es_service=None)


@pytest.fixture
def client(store):
    app = FastAPI()
    app.state.limiter = limiter
    register_exception_handlers(app)
    app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)
    app.include_router(dre.router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=A, user_id="u1", has_pii_access=False, roles=list(Ctx.roles),
    )
    Ctx.roles = ["admin"]
    return TestClient(app, raise_server_exceptions=False)


def _event(event_id, ts, status, *, tenant=A, driver="drv-1"):
    return {
        "event_id": event_id, "tenant_id": tenant, "driver_id": driver,
        "new_status": status, "event_timestamp": ts,
        "previous_status": None, "actor_id": driver, "source": "driver",
    }


def _rows(resp) -> List[List[str]]:
    assert resp.status_code == 200, resp.text
    assert resp.content[:3] == b"\xef\xbb\xbf"
    return list(csv.reader(io.StringIO(resp.content[3:].decode("utf-8"))))


def test_admin_gets_csv_with_bom_and_header(store, client):
    store.seed(DUTY_STATUS_EVENTS_INDEX, [
        _event("e1", "2026-10-01T08:00:00+00:00", "active"),
        _event("e2", "2026-10-01T10:00:00+00:00", "off_duty"),
    ])
    resp = client.get(f"{URL}?{RANGE}")
    assert resp.headers["content-type"].startswith("text/csv")
    assert 'filename="driver_hours_tenant-a_' in resp.headers["content-disposition"]
    rows = _rows(resp)
    assert rows[0] == HEADER
    [row] = rows[1:]
    record = dict(zip(HEADER, row))
    assert record["date"] == "2026-10-01"
    assert record["driver_id"] == "drv-1"
    assert float(record["active_minutes"]) == 120.0
    # 10:00 → end of the range day (the range end is before "now").
    assert float(record["off_duty_minutes"]) == 840.0
    assert record["event_count"] == "2"
    assert record["basis"] == "runsheet_app_duty_status_advisory_not_eld"


@pytest.mark.parametrize("roles", [["dispatcher"], ["driver"]])
def test_dispatcher_and_driver_get_403(store, client, roles):
    Ctx.roles = roles
    resp = client.get(f"{URL}?{RANGE}")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"


def test_foreign_tenant_events_are_not_counted(store, client):
    store.seed(DUTY_STATUS_EVENTS_INDEX, [
        _event("a1", "2026-10-01T08:00:00+00:00", "active"),
        _event("b1", "2026-10-01T08:00:00+00:00", "active", tenant=B, driver="drv-b"),
    ])
    rows = _rows(client.get(f"{URL}?{RANGE}&tenant_id={B}"))
    assert [r[1] for r in rows[1:]] == ["drv-1"]
    # The query carries the tenant filter, not just the post-read re-check.
    assert any(
        {"term": {"tenant_id": A}} in q["query"]["bool"].get("filter", [])
        for q in store.queries
    )


def test_mislabelled_event_is_dropped_by_the_row_check(store, client):
    store.seed(DUTY_STATUS_EVENTS_INDEX, [
        _event("a1", "2026-10-01T08:00:00+00:00", "active"),
    ])

    original = store.search_documents

    async def leaky(index, query, size=100, request_timeout=10):
        resp = await original(index, query, size, request_timeout)
        for hit in resp["hits"]["hits"]:
            hit["_source"]["tenant_id"] = B
        return resp

    store.search_documents = leaky
    rows = _rows(client.get(f"{URL}?{RANGE}"))
    assert rows[1:] == []


@pytest.mark.parametrize("query", [
    "",
    "start_date=2026-10-01",
    "end_date=2026-10-01",
    "start_date=2026-09-01&end_date=2026-10-15",
])
def test_missing_or_over_31_day_range_is_422(store, client, query):
    resp = client.get(f"{URL}?{query}")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_31_days_is_allowed(store, client):
    assert client.get(f"{URL}?start_date=2026-10-01&end_date=2026-10-31").status_code == 200


def test_more_than_50000_events_is_413(store, client):
    store.docs[DUTY_STATUS_EVENTS_INDEX] = [
        _event(f"e{i:06d}", "2026-10-01T08:00:00+00:00", "active")
        for i in range(MAX_EXPORT_ROWS + 1)
    ]
    resp = client.get(f"{URL}?{RANGE}")
    assert resp.status_code == 413
    assert resp.json()["error_code"] == "EXPORT_TOO_LARGE"
    # Rejected on the first page's total, without reading every event.
    assert len(store.queries) == 1


def test_formula_driver_id_is_escaped(store, client):
    store.seed(DUTY_STATUS_EVENTS_INDEX, [
        _event("e1", "2026-10-01T08:00:00+00:00", "active", driver="=HYPERLINK(1)"),
    ])
    rows = _rows(client.get(f"{URL}?{RANGE}"))
    assert rows[1][1] == "'=HYPERLINK(1)"


def test_audit_line_has_filters_and_no_row_data(store, client, caplog):
    store.seed(DUTY_STATUS_EVENTS_INDEX, [
        _event("e1", "2026-10-01T08:00:00+00:00", "active", driver="drv-secret"),
    ])
    caplog.set_level(logging.INFO, logger="services.csv_export")
    _rows(client.get(f"{URL}?{RANGE}&driver_id=drv-secret"))
    records = [r for r in caplog.records if r.getMessage() == "data_export"]
    assert len(records) == 1
    data = records[0].extra_data
    assert data["export_type"] == "driver_hours"
    assert data["outcome"] == "completed"
    assert data["row_count"] == 1
    assert data["filters"] == {
        "driver_id": "drv-secret", "start_date": "2026-10-01", "end_date": "2026-10-01",
    }
    assert set(data) == {
        "event", "export_type", "tenant_id", "user_id", "filters",
        "row_count", "outcome", "duration_ms", "request_id",
    }


def test_driver_filter_narrows_the_query(store, client):
    store.seed(DUTY_STATUS_EVENTS_INDEX, [
        _event("e1", "2026-10-01T08:00:00+00:00", "active", driver="drv-1"),
        _event("e2", "2026-10-01T08:00:00+00:00", "active", driver="drv-2"),
    ])
    rows = _rows(client.get(f"{URL}?{RANGE}&driver_id=drv-2"))
    assert [r[1] for r in rows[1:]] == ["drv-2"]
