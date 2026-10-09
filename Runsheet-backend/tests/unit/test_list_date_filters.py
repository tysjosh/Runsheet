"""G1/G2: typed start_date/end_date on the reconciliation and invoice lists.

Document-store read path. The invoice Postgres read path is covered in
``tests/persistence/test_invoice_date_filters.py``.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from commerce.api import invoice_endpoints
from commerce.services.invoice_service import InvoiceService
from errors.handlers import register_exception_handlers
from fuel.api import fuel_ops_endpoints
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.unit._fake_doc_store import FakeDocStore
from tests.unit.test_data_export_endpoints import invoice_doc, recon_doc

A, B = "tenant-a", "tenant-b"
RECON_INDEX = fuel_ops_endpoints.MVP_RECONCILIATION_INDEX


@pytest.fixture
def store(monkeypatch):
    s = FakeDocStore()
    monkeypatch.setattr(fuel_ops_endpoints, "_es_service", s)
    monkeypatch.setattr(invoice_endpoints, "_invoice_service", InvoiceService(s))
    return s


@pytest.fixture
def client(store):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(fuel_ops_endpoints.mvp_router)
    app.include_router(invoice_endpoints.router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=A, user_id="u1", has_pii_access=False, roles=["admin"],
    )
    return TestClient(app, raise_server_exceptions=False)


def _recon_fixture(store):
    store.seed(RECON_INDEX, [
        recon_doc("R-SEP", A, "2026-09-30T23:59:59.999999Z"),
        recon_doc("R-OCT1", A, "2026-10-01T00:00:00Z"),
        recon_doc("R-OCT15", A, "2026-10-15T08:30:00.123456Z"),
        recon_doc("R-OCT31", A, "2026-10-31T23:59:59.999999Z"),
        recon_doc("R-NOV", A, "2026-11-01T00:00:00.000001Z"),
        recon_doc("R-B", B, "2026-10-15T00:00:00Z"),
    ])


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return [i["reconciliation_id"] for i in resp.json()["items"]]


# ---------------------------------------------------------------- G1

def test_g1_valid_range_whole_end_day(store, client):
    _recon_fixture(store)
    ids = _ids(client.get("/api/fuel/mvp/reconciliation?start_date=2026-10-01&end_date=2026-10-31"))
    assert sorted(ids) == ["R-OCT1", "R-OCT15", "R-OCT31"]


def test_g1_datetime_bounds(store, client):
    _recon_fixture(store)
    ids = _ids(client.get(
        "/api/fuel/mvp/reconciliation?start_date=2026-10-15T08:30:00Z&end_date=2026-10-31T00:00:00-05:00"
    ))
    assert ids == ["R-OCT15"]


@pytest.mark.parametrize("qs,field", [
    ("start_date=10/01/2026", "start_date"),
    ("end_date=2026-10-32", "end_date"),
    ("start_date=2026-11-01&end_date=2026-10-01", "start_date"),
])
def test_g1_invalid_or_reversed_422(store, client, qs, field):
    resp = client.get(f"/api/fuel/mvp/reconciliation?{qs}")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    assert body["details"]["field"] == field


def test_g1_omitted_is_unchanged(store, client):
    _recon_fixture(store)
    resp = client.get("/api/fuel/mvp/reconciliation?page=1&size=50")
    # The query the list sent before this change, verbatim.
    assert store.queries[-1] == {
        "query": {"bool": {"must": [{"term": {"tenant_id": A}}]}},
        "sort": [{"generated_at": {"order": "desc"}}],
        "from": 0,
        "size": 50,
    }
    body = resp.json()
    assert [i["reconciliation_id"] for i in body["items"]] == [
        "R-NOV", "R-OCT31", "R-OCT15", "R-OCT1", "R-SEP",
    ]
    assert body["total"] == 5 and body["page"] == 1


# ---------------------------------------------------------------- G2

def _invoice_fixture(store):
    from commerce.services.invoice_service import INVOICES_CURRENT_INDEX
    store.seed(INVOICES_CURRENT_INDEX, [
        invoice_doc("I-SEP", A, "2026-09-30T23:59:59.999999+00:00"),
        invoice_doc("I-OCT1", A, "2026-10-01T00:00:00+00:00", qbo_push_state="dead_letter"),
        invoice_doc("I-OCT31", A, "2026-10-31T23:59:59+00:00"),
        invoice_doc("I-NOV", A, "2026-11-01T00:00:00+00:00"),
        invoice_doc("I-B", B, "2026-10-15T00:00:00+00:00"),
    ])


def _inv_ids(resp):
    assert resp.status_code == 200, resp.text
    return [i["invoice_id"] for i in resp.json()["data"]]


def test_g2_valid_range(store, client):
    _invoice_fixture(store)
    ids = _inv_ids(client.get("/api/commerce/invoices?start_date=2026-10-01&end_date=2026-10-31"))
    assert ids == ["I-OCT31", "I-OCT1"]


@pytest.mark.parametrize("qs", [
    "start_date=yesterday", "end_date=2026-02-30T00:00:00",
    "start_date=2026-10-02&end_date=2026-10-01",
])
def test_g2_invalid_or_reversed_422(store, client, qs):
    resp = client.get(f"/api/commerce/invoices?{qs}")
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_g2_omitted_is_unchanged(store, client):
    _invoice_fixture(store)
    ids = _inv_ids(client.get("/api/commerce/invoices"))
    assert ids == ["I-NOV", "I-OCT31", "I-OCT1", "I-SEP"]
    q = store.queries[-1]
    assert q["query"]["bool"]["must"] == [{"bool": {"must": [{"match_all": {}}]}}]
    assert q["sort"] == [{"created_at": {"order": "desc"}}, {"invoice_id": {"order": "asc"}}]


def test_g2_qbo_push_state_now_filters(store, client):
    _invoice_fixture(store)
    assert _inv_ids(client.get("/api/commerce/invoices?qbo_push_state=dead_letter")) == ["I-OCT1"]


async def test_g2_count_matches_list_doc_path(store):
    _invoice_fixture(store)
    svc = InvoiceService(store)
    from datetime import datetime, timezone
    kwargs = dict(created_from=datetime(2026, 10, 1, tzinfo=timezone.utc),
                  created_before=datetime(2026, 11, 1, tzinfo=timezone.utc))
    listed = await svc.list(tenant_id=A, limit=200, **kwargs)
    assert await svc.count(tenant_id=A, **kwargs) == len(listed["items"]) == 2
    assert await svc.count(tenant_id=A) == 4
