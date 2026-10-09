"""Driver qualification expiry CSV export (OI-57, owner decision 2026-10-07).

``GET /api/compliance/drivers/export``: admin only, same safeguards as the
data-export v1 exports. No CDL number, phone or email (data-export FR4).
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from compliance.api import driver_endpoints
from compliance.services.driver_qualification_service import (
    DriverQualificationService,
)
from errors.handlers import register_exception_handlers
from middleware.rate_limiter import _custom_rate_limit_handler, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

A, B = "tenant-a", "tenant-b"
URL = "/api/compliance/drivers/export"
TODAY = date.today()
HEADER = [
    "driver_id", "full_name", "driver_status", "cdl_expiry_date",
    "medical_card_expiry_date", "hazmat_endorsement_expiry_date",
    "tanker_endorsement_expiry_date", "nearest_expiry_date",
    "nearest_expiry_type", "days_until_nearest_expiry", "overall_status",
]
PII_KEYS = ("cdl_number", "license_number", "phone", "email")


def _iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def _driver(driver_id, *, tenant=A, status="active", **overrides) -> Dict[str, Any]:
    doc = {
        "driver_id": driver_id,
        "tenant_id": tenant,
        "full_name": f"Name {driver_id}",
        "status": status,
        "cdl_number": "CDL-SECRET-123",
        "cdl_class": "A",
        "phone": "+1-555-0100",
        "email": "driver@example.com",
        "license_number": "LIC-SECRET-9",
        "cdl_expiry_date": _iso(400),
        "medical_card_expiry_date": _iso(200),
        "hazmat_endorsement_expiry_date": None,
        "tanker_endorsement_expiry_date": None,
    }
    doc.update(overrides)
    return doc


class _FakeDQService(DriverQualificationService):
    """Real summary logic, in-memory ``list`` with driver_id cursors."""

    def __init__(self, drivers: List[Dict[str, Any]]) -> None:
        super().__init__(es_service=None)
        self.drivers = drivers
        self.list_calls: List[Dict[str, Any]] = []

    async def list(self, tenant_id, *, cursor=None, limit=50, status=None):
        self.list_calls.append({"tenant_id": tenant_id, "cursor": cursor,
                                "limit": limit, "status": status})
        # Like a backend that ignores the tenant filter, so the endpoint's
        # own row check is what keeps tenant B out.
        rows = sorted(self.drivers, key=lambda d: d["driver_id"])
        if status:
            rows = [d for d in rows if d.get("status") == status]
        if cursor:
            rows = [d for d in rows if d["driver_id"] > cursor]
        page = rows[:limit]
        next_cursor = page[-1]["driver_id"] if len(page) == limit else None
        return {"items": [dict(d) for d in page], "next_cursor": next_cursor, "limit": limit}

    async def _get_for_summary(self, tenant_id: str, driver_id: str) -> Dict[str, Any]:
        return await self.get(tenant_id, driver_id)

    async def get(self, tenant_id: str, driver_id: str) -> Dict[str, Any]:
        for d in self.drivers:
            if d["driver_id"] == driver_id and d["tenant_id"] == tenant_id:
                return dict(d)
        from errors.exceptions import resource_not_found

        raise resource_not_found("Driver not found", details={"driver_id": driver_id})


class Ctx:
    roles = ["admin"]


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def svc():
    service = _FakeDQService([
        _driver("D-1"),
        _driver("D-2", medical_card_expiry_date=_iso(10)),
        _driver("D-3", status="suspended"),
        _driver("B-1", tenant=B),
    ])
    driver_endpoints.configure_driver_api(driver_service=service)
    yield service
    driver_endpoints._driver_service = None


@pytest.fixture
def client(svc):
    app = FastAPI()
    app.state.limiter = limiter
    register_exception_handlers(app)
    app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)
    app.include_router(driver_endpoints.router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=A, user_id="u1", has_pii_access=False, roles=list(Ctx.roles),
    )
    Ctx.roles = ["admin"]
    return TestClient(app, raise_server_exceptions=False)


def _rows(resp) -> List[List[str]]:
    assert resp.status_code == 200, resp.text
    assert resp.content[:3] == b"\xef\xbb\xbf"
    return list(csv.reader(io.StringIO(resp.content[3:].decode("utf-8"))))


def _records(resp) -> Dict[str, Dict[str, str]]:
    rows = _rows(resp)
    return {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}


def test_admin_gets_expected_columns_and_no_pii(svc, client):
    resp = client.get(URL)
    rows = _rows(resp)
    assert rows[0] == HEADER
    assert 'filename="driver_qualifications_tenant-a_' in resp.headers["content-disposition"]
    body = resp.content.decode("utf-8")
    for key in PII_KEYS:
        assert key not in rows[0]
    for value in ("CDL-SECRET-123", "LIC-SECRET-9", "+1-555-0100", "driver@example.com"):
        assert value not in body
    records = _records(resp)
    d1, d2, d3 = records["D-1"], records["D-2"], records["D-3"]
    assert d1["overall_status"] == "valid"
    assert d1["nearest_expiry_type"] == "medical_card"
    assert d1["nearest_expiry_date"] == _iso(200)
    assert d1["days_until_nearest_expiry"] == "200"
    assert d2["overall_status"] == "expiring"
    assert d2["days_until_nearest_expiry"] == "10"
    assert d3["overall_status"] == "expired"  # suspended → not road-legal
    assert d3["driver_status"] == "suspended"


async def test_overall_status_matches_the_qualification_summary(svc, client):
    records = _records(client.get(URL))
    for driver_id in ("D-1", "D-2", "D-3"):
        summary = await svc.get_qualification_summary(A, driver_id)
        assert records[driver_id]["overall_status"] == summary.overall_status


@pytest.mark.parametrize("roles", [["dispatcher"], ["driver"]])
def test_dispatcher_and_driver_get_403(svc, client, roles):
    Ctx.roles = roles
    resp = client.get(URL)
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"


def test_tenant_isolation(svc, client):
    records = _records(client.get(f"{URL}?tenant_id={B}"))
    assert set(records) == {"D-1", "D-2", "D-3"}
    assert all(call["tenant_id"] == A for call in svc.list_calls)


def test_status_filter_is_passed_through(svc, client):
    records = _records(client.get(f"{URL}?status=suspended"))
    assert set(records) == {"D-3"}
    assert all(call["status"] == "suspended" for call in svc.list_calls)


def test_export_does_not_bind_as_a_driver_id(svc, client):
    calls_before = len(svc.list_calls)
    assert client.get(URL).status_code == 200
    assert len(svc.list_calls) > calls_before  # the export handler ran
    resp = client.get("/api/compliance/drivers/D-1")
    assert resp.status_code == 200, resp.text
    assert "D-1" in resp.text


def test_formula_full_name_is_escaped(svc, client):
    svc.drivers.append(_driver("D-9", full_name='=HYPERLINK("http://x")'))
    records = _records(client.get(URL))
    assert records["D-9"]["full_name"] == "'=HYPERLINK(\"http://x\")"


def test_pages_through_more_than_one_service_page(svc, client):
    svc.drivers[:] = [_driver(f"D-{i:04d}") for i in range(450)]
    records = _records(client.get(URL))
    assert len(records) == 450
    assert [c["limit"] for c in svc.list_calls] == [200, 200, 200]


def test_row_cap_gives_413(svc, client, monkeypatch):
    monkeypatch.setattr(driver_endpoints, "MAX_EXPORT_ROWS", 2)
    resp = client.get(URL)
    assert resp.status_code == 413
    assert resp.json()["error_code"] == "EXPORT_TOO_LARGE"
