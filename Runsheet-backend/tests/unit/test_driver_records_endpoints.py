"""G3: admin-only tenant-wide HOS and DVIR lists (data-export design §5)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from compliance.api import driver_records_endpoints as dre
from driver.services.driver_es_mappings import (
    DUTY_STATUS_EVENTS_INDEX,
    VEHICLE_INSPECTIONS_INDEX,
)
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.unit._fake_doc_store import FakeDocStore

A, B = "tenant-a", "tenant-b"

ROUTES = {
    "hos": ("/api/compliance/hos-records", DUTY_STATUS_EVENTS_INDEX, "event_id", "event_timestamp"),
    "dvir": ("/api/compliance/inspections", VEHICLE_INSPECTIONS_INDEX, "inspection_id", "inspection_timestamp"),
}


class Ctx:
    roles = ["admin"]


@pytest.fixture
def store():
    s = FakeDocStore()
    dre.configure_driver_records_api(es_service=s)
    yield s
    dre.configure_driver_records_api(es_service=None)


@pytest.fixture
def client(store):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(dre.router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=A, user_id="u1", has_pii_access=False, roles=list(Ctx.roles),
    )
    Ctx.roles = ["admin"]
    return TestClient(app, raise_server_exceptions=False)


def _seed(store, kind, n=3, tenant=A, driver="drv-1", day="2026-10-0"):
    _, index, id_field, ts_field = ROUTES[kind]
    store.seed(index, [
        {id_field: f"{tenant}-{driver}-{i}", "tenant_id": tenant, "driver_id": driver,
         ts_field: f"{day}{i + 1}T12:00:00+00:00"}
        for i in range(n)
    ])


@pytest.mark.parametrize("kind", list(ROUTES))
def test_admin_200_with_paging_metadata(store, client, kind):
    _seed(store, kind, n=3)
    resp = client.get(f"{ROUTES[kind][0]}?size=2&page=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3 and body["page"] == 1 and body["page_size"] == 2
    assert len(body["items"]) == 2
    assert all("tenant_id" not in item for item in body["items"])
    # Newest first.
    ts = ROUTES[kind][3]
    assert body["items"][0][ts] > body["items"][1][ts]


@pytest.mark.parametrize("kind", list(ROUTES))
@pytest.mark.parametrize("roles", [["dispatcher"], ["driver"], [], ["platform_admin"]])
def test_non_admin_403(store, client, kind, roles):
    Ctx.roles = roles
    resp = client.get(ROUTES[kind][0])
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"


@pytest.mark.parametrize("kind", list(ROUTES))
def test_driver_and_date_filters(store, client, kind):
    _seed(store, kind, n=3, driver="drv-1")
    _seed(store, kind, n=3, driver="drv-2")
    resp = client.get(f"{ROUTES[kind][0]}?driver_id=drv-2&start_date=2026-10-02&end_date=2026-10-02")
    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["total"] == 1
    assert body["items"][0]["driver_id"] == "drv-2"
    assert body["items"][0][ROUTES[kind][3]].startswith("2026-10-02")


@pytest.mark.parametrize("kind", list(ROUTES))
@pytest.mark.parametrize("qs,status", [
    ("start_date=nope", 422), ("start_date=2026-10-05&end_date=2026-10-01", 422),
    ("size=201", 422), ("size=0", 422), ("page=0", 422),
    ("page=51&size=200", 422),
])
def test_validation(store, client, kind, qs, status):
    resp = client.get(f"{ROUTES[kind][0]}?{qs}")
    assert resp.status_code == status, resp.text


def test_window_exceeded_details(store, client):
    resp = client.get("/api/compliance/hos-records?page=51&size=200")
    assert resp.json()["details"] == {"field": "page", "reason": "window_exceeded"}
    assert client.get("/api/compliance/hos-records?page=50&size=200").status_code == 200


@pytest.mark.parametrize("kind", list(ROUTES))
def test_tenant_isolation(store, client, kind):
    _seed(store, kind, n=2, tenant=A)
    _seed(store, kind, n=2, tenant=B)
    resp = client.get(f"{ROUTES[kind][0]}?tenant_id={B}")
    ids = [i[ROUTES[kind][2]] for i in resp.json()["items"]]
    assert ids and all(i.startswith(A) for i in ids)
    # The query itself carries the caller's tenant.
    assert {"term": {"tenant_id": A}} in store.queries[-1]["query"]["bool"]["filter"]


class LeakyStore(FakeDocStore):
    """Ignores the tenant filter, to prove the per-row re-check."""

    async def search_documents(self, index, query, size=100, request_timeout=10):
        inner = dict(query)
        inner["query"] = query["query"]["bool"]["must"][0]
        return await super().search_documents(index, inner, size)


@pytest.mark.parametrize("kind", list(ROUTES))
def test_mislabelled_row_dropped(client, kind):
    leaky = LeakyStore()
    dre.configure_driver_records_api(es_service=leaky)
    _seed(leaky, kind, n=1, tenant=A)
    _seed(leaky, kind, n=1, tenant=B)
    ids = [i[ROUTES[kind][2]] for i in client.get(ROUTES[kind][0]).json()["items"]]
    assert ids == [f"{A}-drv-1-0"]
