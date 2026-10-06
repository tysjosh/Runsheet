"""Endpoint tests for the five CSV exports (data-export design §10).

Tenant isolation, role allow/deny, CSV-injection escaping, the row cap, PII
exclusion, date filters, route shadowing, the per-user rate limit and the
OpenAPI parameter surface, through FastAPI ``TestClient`` with in-memory
fakes behind each export's real handler.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from commerce.api import invoice_endpoints
from compliance.api import ifta_endpoints
from config.settings import get_settings
from errors.handlers import register_exception_handlers
from fuel.api import fuel_ops_endpoints, order_endpoints
from fuel.order_models import FuelOrder
from middleware.rate_limiter import _custom_rate_limit_handler, limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from scheduling.api import endpoints as scheduling_endpoints
from services import csv_export
from tests.unit._fake_doc_store import FakeDocStore

A, B = "tenant-a", "tenant-b"
PHONE, EMAIL = "555-0100", "jane@example.com"
PII_SUBSTRINGS = ("customer_phone", "customer_email", "phone", "email", "license", "cdl")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def order_model(order_id: str, tenant: str, created_at: str, **extra) -> FuelOrder:
    doc = dict(
        order_id=order_id, tenant_id=tenant, customer_id=f"CUST-{tenant}",
        customer_name="Acme", customer_phone=PHONE, customer_email=EMAIL,
        ship_to_address="1 Main St", ship_to_lat=1.0, ship_to_lon=1.0,
        product_code="DIESEL_2", gallons_requested=100.0, call_type="will_call",
        intake_channel="dispatcher", intake_channel_id="x",
        source_schema_version="1", trace_id="tr", created_at=created_at,
        updated_at=created_at, last_event_timestamp=created_at,
    )
    doc.update(extra)
    return FuelOrder(**doc)


class FakeOrderRepo:
    def __init__(self) -> None:
        self.orders: List[FuelOrder] = []
        self.calls: List[Dict[str, Any]] = []

    async def search(self, tenant_id, *, size, keyset=False, after=None, with_total=True, **filters):
        self.calls.append(dict(filters, tenant_id=tenant_id, keyset=keyset))
        assert keyset is True
        rows = sorted(
            (o for o in self.orders if o.tenant_id == tenant_id),
            key=lambda o: o.order_id,
        )
        rows.sort(key=lambda o: o.created_at, reverse=True)
        if after is not None:
            idx = next(i for i, o in enumerate(rows) if o.order_id == after[1])
            rows = rows[idx + 1:]
        page = rows[:size]
        return {
            "orders": page,
            "total": len([o for o in self.orders if o.tenant_id == tenant_id]),
            "raw_count": len(page),
            "last_key": (page[-1].created_at.isoformat(), page[-1].order_id) if page else None,
        }


class FakeJobService:
    def __init__(self) -> None:
        self.jobs: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []

    async def list_jobs(self, tenant_id, *, size, keyset=False, after=None, with_total=True, **filters):
        self.calls.append(dict(filters, tenant_id=tenant_id))
        assert keyset is True
        rows = sorted((j for j in self.jobs if j["tenant_id"] == tenant_id),
                      key=lambda j: (j["scheduled_time"], j["job_id"]))
        if after is not None:
            idx = next(i for i, j in enumerate(rows) if j["job_id"] == after[1])
            rows = rows[idx + 1:]
        page = rows[:size]
        total = len([j for j in self.jobs if j["tenant_id"] == tenant_id])
        return {
            "data": page,
            "pagination": {"page": 1, "size": size, "total": total, "total_pages": 1},
            "raw_count": len(page),
            "last_key": (page[-1]["scheduled_time"], page[-1]["job_id"]) if page else None,
        }


def job_doc(job_id: str, tenant: str, **extra) -> Dict[str, Any]:
    doc = {"job_id": job_id, "tenant_id": tenant, "scheduled_time": "2026-10-01T08:00:00Z",
           "status": "scheduled", "job_type": "fuel_delivery", "notes": "ok",
           "driver_phone": PHONE}
    doc.update(extra)
    return doc


class FakeInvoiceService:
    def __init__(self) -> None:
        self.invoices: List[Dict[str, Any]] = []

    def _filter(self, tenant_id, status=None, customer_id=None, account_id=None,
                order_id=None, qbo_push_state=None, created_from=None,
                created_before=None, created_until=None):
        out = []
        for inv in self.invoices:
            created = datetime.fromisoformat(inv["created_at"])
            if inv["tenant_id"] != tenant_id:
                continue
            if status and inv["status"] != status:
                continue
            if customer_id and inv["customer_id"] != customer_id:
                continue
            if qbo_push_state and inv.get("qbo_push_state") != qbo_push_state:
                continue
            if created_from and created < created_from:
                continue
            if created_before and created >= created_before:
                continue
            if created_until and created > created_until:
                continue
            out.append(inv)
        out.sort(key=lambda i: i["invoice_id"])
        out.sort(key=lambda i: i["created_at"], reverse=True)
        return out

    async def list(self, *, tenant_id, cursor=None, limit=50, **filters):
        rows = self._filter(tenant_id, **filters)
        if cursor:
            idx = next(i for i, r in enumerate(rows) if r["invoice_id"] == cursor)
            rows = rows[idx + 1:]
        page = rows[:limit]
        return {"items": page, "next_cursor": page[-1]["invoice_id"] if len(page) == limit else None, "limit": limit}

    async def count(self, *, tenant_id, **filters):
        return len(self._filter(tenant_id, **filters))


def invoice_doc(invoice_id: str, tenant: str, created_at: str = "2026-10-01T12:00:00+00:00", **extra):
    doc = {"invoice_id": invoice_id, "tenant_id": tenant, "invoice_number": f"N-{invoice_id}",
           "created_at": created_at, "status": "open", "customer_id": "CUST-1",
           "account_id": "ACC-1", "order_id": "ORD-1", "total_cents": 1000,
           "qbo_push_state": "pending",
           "line_items": [{"quantity_gallons": 10.0}, {"quantity_gallons": 5.5}]}
    doc.update(extra)
    return doc


def recon_doc(rid: str, tenant: str, generated_at: str = "2026-10-01T12:00:00.000001Z", **extra):
    doc = {"reconciliation_id": rid, "tenant_id": tenant, "order_id": f"O-{rid}",
           "plan_id": "P", "pod_id": "POD", "ordered_gallons": 100.0,
           "loaded_gallons": 100.0, "delivered_gallons": 99.0,
           "variance_load_vs_order_pct": 0.0, "variance_delivered_vs_loaded_pct": 1.0,
           "variance_invoiced_vs_delivered_pct": None, "alert_flags": [],
           "generated_at": generated_at, "created_at": generated_at, "updated_at": generated_at}
    doc.update(extra)
    return doc


class FakeIFTA:
    def __init__(self) -> None:
        self.reports: Dict[str, Any] = {}

    async def generate_quarterly_report(self, tenant_id, quarter):
        trucks, incomplete = self.reports.get(tenant_id, ([], []))
        return SimpleNamespace(quarter=quarter, trucks=trucks, incomplete_trucks=incomplete,
                               truck_count=len(trucks))


def ifta_truck(truck_id: str, *jurisdictions: str):
    return SimpleNamespace(truck_id=truck_id, jurisdictions=[
        SimpleNamespace(jurisdiction=j, total_miles=100.0, taxable_miles=90.0,
                        tax_paid_gallons=10.0, net_taxable_gallons=5.0,
                        tax_rate=0.2, tax_due=-1.5)
        for j in jurisdictions
    ])


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class World:
    def __init__(self) -> None:
        self.ctx = TenantContext(tenant_id=A, user_id="u1", has_pii_access=False, roles=["admin"])
        self.orders = FakeOrderRepo()
        self.jobs = FakeJobService()
        self.invoices = FakeInvoiceService()
        self.recon = FakeDocStore()
        self.ifta = FakeIFTA()

    def as_(self, *roles: str, user: str = "u1", tenant: str = A) -> "World":
        self.ctx = TenantContext(tenant_id=tenant, user_id=user, has_pii_access=False, roles=list(roles))
        return self


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(order_endpoints, "_order_repository", w.orders)
    monkeypatch.setattr(scheduling_endpoints, "_job_service", w.jobs)
    monkeypatch.setattr(invoice_endpoints, "_invoice_service", w.invoices)
    monkeypatch.setattr(fuel_ops_endpoints, "_es_service", w.recon)
    monkeypatch.setattr(ifta_endpoints, "_ifta_reporter", w.ifta)
    return w


def _app(world: World) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    register_exception_handlers(app)
    app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)
    for r in (order_endpoints.router, scheduling_endpoints.router,
              fuel_ops_endpoints.mvp_router, invoice_endpoints.router,
              ifta_endpoints.router):
        app.include_router(r)
    app.dependency_overrides[get_tenant_context] = lambda: world.ctx
    return app


@pytest.fixture
def client(world):
    return TestClient(_app(world), raise_server_exceptions=False)


def _rows(resp) -> List[List[str]]:
    assert resp.status_code == 200, resp.text
    assert resp.content[:3] == b"\xef\xbb\xbf"
    return list(csv.reader(io.StringIO(resp.content[3:].decode("utf-8"))))


EXPORTS = {
    "ifta": "/api/compliance/ifta/report/export?quarter=2026-Q3",
    "orders": "/api/orders/export",
    "jobs": "/api/scheduling/jobs/export",
    "reconciliation": "/api/fuel/mvp/reconciliation/export",
    "invoices": "/api/commerce/invoices/export",
}
ID_COLUMN = {"ifta": "truck_id", "orders": "order_id", "jobs": "job_id",
             "reconciliation": "reconciliation_id", "invoices": "invoice_id"}


def seed_all(w: World) -> None:
    w.orders.orders += [order_model("A-ORD-1", A, "2026-10-01T00:00:00Z"),
                        order_model("B-ORD-1", B, "2026-10-01T00:00:00Z")]
    w.jobs.jobs += [job_doc("A-JOB-1", A), job_doc("B-JOB-1", B)]
    w.invoices.invoices += [invoice_doc("A-INV-1", A), invoice_doc("B-INV-1", B)]
    w.recon.seed(fuel_ops_endpoints.MVP_RECONCILIATION_INDEX,
                 [recon_doc("A-REC-1", A), recon_doc("B-REC-1", B)])
    w.ifta.reports[A] = ([ifta_truck("A-TRK-1", "TX")], [])
    w.ifta.reports[B] = ([ifta_truck("B-TRK-1", "OK")], [])


def _ids(resp, export: str) -> List[str]:
    rows = _rows(resp)
    col = rows[0].index(ID_COLUMN[export])
    return [r[col] for r in rows[1:]]


# ---------------------------------------------------------------------------
# Common behavior across the five exports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("export", list(EXPORTS))
def test_tenant_isolation_and_tenant_id_param_ignored(world, client, export):
    seed_all(world)
    url = EXPORTS[export]
    sep = "&" if "?" in url else "?"
    for target in (url, f"{url}{sep}tenant_id={B}"):
        ids = _ids(client.get(target), export)
        assert ids and all(i.startswith("A-") for i in ids), ids


@pytest.mark.parametrize("export", list(EXPORTS))
def test_headers_and_filename(world, client, export):
    seed_all(world)
    resp = client.get(EXPORTS[export])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert resp.headers["content-disposition"] == f'attachment; filename="{export}_{A}_{today}.csv"'
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("export", list(EXPORTS))
@pytest.mark.parametrize("roles,expected", [
    (("admin",), 200), (("dispatcher",), 200), (("driver",), 403), ((), 403),
    (("platform_admin",), 403),
])
def test_roles(world, client, export, roles, expected):
    seed_all(world)
    world.as_(*roles)
    if export == "invoices" and roles == ("dispatcher",):
        expected = 403
    resp = client.get(EXPORTS[export])
    assert resp.status_code == expected, resp.text
    if expected == 403:
        assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"


def test_invoice_dispatcher_403_names_admin(world, client):
    world.as_("dispatcher")
    resp = client.get(EXPORTS["invoices"])
    assert resp.status_code == 403
    body = resp.json()
    assert body["error_code"] == "INSUFFICIENT_ROLE"
    assert body["details"]["required_roles"] == ["admin"]


@pytest.mark.parametrize("roles", [("admin",), ("dispatcher",)])
def test_invoice_flag_off_is_404(world, client, monkeypatch, roles):
    monkeypatch.setattr(
        invoice_endpoints, "get_settings",
        lambda: SimpleNamespace(commerce_backbone_enabled=True, commerce_invoicing_enabled=False),
    )
    world.as_(*roles)
    assert client.get(EXPORTS["invoices"]).status_code == 404


def test_reconciliation_driver_gets_403_though_list_allows(world, client):
    seed_all(world)
    world.as_("driver")
    assert client.get("/api/fuel/mvp/reconciliation").status_code == 200
    assert client.get(EXPORTS["reconciliation"]).status_code == 403


@pytest.mark.parametrize("export,column,payload,expected", [
    ("orders", "customer_name", "=HYPERLINK(\"http://x\")", "'=HYPERLINK(\"http://x\")"),
    ("jobs", "notes", "+cmd", "'+cmd"),
    ("reconciliation", "alert_flags", ["@x"], "'@x"),
    ("invoices", "customer_id", "-1+1", "'-1+1"),
    ("ifta", "truck_id", "=1", "'=1"),
])
def test_csv_injection_escaped(world, client, export, column, payload, expected):
    if export == "orders":
        world.orders.orders.append(order_model("O1", A, "2026-10-01T00:00:00Z", customer_name=payload))
    elif export == "jobs":
        world.jobs.jobs.append(job_doc("J1", A, notes=payload))
    elif export == "reconciliation":
        world.recon.seed(fuel_ops_endpoints.MVP_RECONCILIATION_INDEX, [recon_doc("R1", A, alert_flags=payload)])
    elif export == "invoices":
        world.invoices.invoices.append(invoice_doc("I1", A, customer_id=payload))
    else:
        world.ifta.reports[A] = ([ifta_truck(payload, "TX")], [])
    rows = _rows(client.get(EXPORTS[export]))
    assert rows[1][rows[0].index(column)] == expected


@pytest.mark.parametrize("export", list(EXPORTS))
def test_row_cap_returns_413(world, client, monkeypatch, export):
    monkeypatch.setattr(csv_export, "MAX_EXPORT_ROWS", 2)
    for i in range(3):
        world.orders.orders.append(order_model(f"O{i}", A, f"2026-10-0{i + 1}T00:00:00Z"))
        world.jobs.jobs.append(job_doc(f"J{i}", A))
        world.invoices.invoices.append(invoice_doc(f"I{i}", A))
        world.recon.seed(fuel_ops_endpoints.MVP_RECONCILIATION_INDEX, [recon_doc(f"R{i}", A)])
    world.ifta.reports[A] = ([ifta_truck("T1", "TX", "OK", "NM")], [])
    resp = client.get(EXPORTS[export])
    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert body["error_code"] == "EXPORT_TOO_LARGE"
    assert body["details"] == {"row_count": 3, "max_rows": 2}


@pytest.mark.parametrize("export", list(EXPORTS))
def test_pii_columns_and_values_absent(world, client, export):
    seed_all(world)
    resp = client.get(EXPORTS[export])
    rows = _rows(resp)
    header = ",".join(rows[0]).lower()
    for sub in PII_SUBSTRINGS:
        assert sub not in header, (export, sub)
    body = resp.content.decode("utf-8")
    assert PHONE not in body and EMAIL not in body


def test_orders_include_customer_name_and_address(world, client):
    seed_all(world)
    rows = _rows(client.get(EXPORTS["orders"]))
    assert {"customer_name", "ship_to_address"} <= set(rows[0])


@pytest.mark.parametrize("export", list(EXPORTS))
def test_rate_limit_per_user(world, client, export):
    seed_all(world)
    limit = get_settings().export_rate_limit
    statuses = [client.get(EXPORTS[export]).status_code for _ in range(limit + 1)]
    assert statuses[:limit] == [200] * limit
    assert statuses[-1] == 429
    last = client.get(EXPORTS[export])
    assert last.status_code == 429
    assert last.json()["error_code"] == "RATE_LIMITED"
    assert "retry-after" in {k.lower() for k in last.headers}
    world.as_("admin", user="u2")
    assert client.get(EXPORTS[export]).status_code == 200


@pytest.mark.parametrize("export", list(EXPORTS))
def test_paging_params_not_in_openapi(world, export):
    spec = _app(world).openapi()
    path = EXPORTS[export].split("?")[0]
    names = {p["name"] for p in spec["paths"][path]["get"].get("parameters", [])}
    assert not names & {"page", "size", "limit", "cursor", "tenant_id"}


@pytest.mark.parametrize("export", ["orders", "jobs", "invoices"])
def test_export_route_not_shadowed_by_id_route(world, client, export):
    seed_all(world)
    resp = client.get(EXPORTS[export])
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")


# ---------------------------------------------------------------------------
# Per-export specifics
# ---------------------------------------------------------------------------


def test_ifta_missing_quarter_422_malformed_400(world, client):
    assert client.get("/api/compliance/ifta/report/export").status_code == 422
    assert client.get("/api/compliance/ifta/report/export?quarter=2026Q3").status_code == 400


def test_ifta_report_failure_is_500(world, client, monkeypatch):
    async def boom(tenant_id, quarter):
        raise RuntimeError("geotab down")
    monkeypatch.setattr(world.ifta, "generate_quarterly_report", boom)
    resp = client.get(EXPORTS["ifta"])
    assert resp.status_code == 500
    assert resp.json()["error_code"] == "ifta.report_failed"


def test_ifta_rows_and_incomplete_trucks(world, client):
    world.ifta.reports[A] = (
        [ifta_truck("T1", "TX", "OK")],
        [SimpleNamespace(truck_id="T9", reason="missing Geotab data")],
    )
    rows = _rows(client.get(EXPORTS["ifta"]))
    assert rows[0] == ["quarter", "truck_id", "jurisdiction", "total_miles", "taxable_miles",
                       "tax_paid_gallons", "net_taxable_gallons", "tax_rate", "tax_due",
                       "ifta_data_incomplete", "incomplete_reason"]
    assert rows[1][:3] == ["2026-Q3", "T1", "TX"]
    assert rows[1][8] == "-1.5"  # negative numbers are never prefixed
    assert rows[1][9] == "false"
    assert rows[3][1] == "T9" and rows[3][9] == "true" and rows[3][10] == "missing Geotab data"


def test_orders_paging_spans_pages(world, client):
    n = csv_export.EXPORT_PAGE_SIZE + 5
    for i in range(n):
        world.orders.orders.append(order_model(f"O{i:04d}", A, "2026-10-01T00:00:00Z"))
    ids = _ids(client.get(EXPORTS["orders"]), "orders")
    assert ids == [f"O{i:04d}" for i in range(n)]


def test_orders_delivery_columns(world, client):
    world.orders.orders.append(order_model(
        "O1", A, "2026-10-01T00:00:00Z", status="delivered",
        delivery_result={
            "pod_id": "POD1", "actual_gallons": 98.5, "actual_gallons_source": "meter",
            "delivered_at": "2026-10-02T10:00:00Z", "recipient_name": "Sam",
            "geotag": {"lat": 1.0, "lon": 1.0},
        },
    ))
    rows = _rows(client.get(EXPORTS["orders"]))
    assert rows[1][rows[0].index("delivered_gallons")] == "98.5"
    assert rows[1][rows[0].index("delivered_at")].startswith("2026-10-02T10:00:00")
    assert "recipient_name" not in rows[0] and "Sam" not in ",".join(rows[1])


def test_orders_unsupported_sort_422(world, client):
    resp = client.get("/api/orders/export?sort=updated_at:desc")
    assert resp.status_code == 422
    assert resp.json()["details"] == {"field": "sort", "reason": "not_supported_for_export"}
    assert client.get("/api/orders/export?sort=created_at:asc").status_code == 200


def test_orders_filters_pass_through(world, client):
    client.get("/api/orders/export?status=placed&start_date=2026-10-01&end_date=2026-10-31&q=acme")
    call = world.orders.calls[-1]
    assert call["status"] == "placed"
    assert call["start_date"] == "2026-10-01" and call["end_date"] == "2026-10-31"
    assert call["q"] == "acme"
    assert call["tenant_id"] == A


def test_orders_invalid_date_422_like_list(world, client):
    assert client.get("/api/orders/export?start_date=nope").status_code == 422


def test_jobs_unsupported_sort_422_and_pass_through(world, client):
    resp = client.get("/api/scheduling/jobs/export?sort_by=priority")
    assert resp.status_code == 422
    assert resp.json()["details"] == {"field": "sort_by", "reason": "not_supported_for_export"}
    client.get("/api/scheduling/jobs/export?start_date=2026-10-01&end_date=2026-10-31&status=scheduled")
    call = world.jobs.calls[-1]
    assert call["start_date"] == "2026-10-01" and call["end_date"] == "2026-10-31"
    assert call["status"] == "scheduled" and call["sort_by"] == "scheduled_time"


def test_reconciliation_date_range_and_inclusive_end(world, client):
    idx = fuel_ops_endpoints.MVP_RECONCILIATION_INDEX
    world.recon.seed(idx, [
        recon_doc("R-SEP", A, "2026-09-30T23:59:59.999999Z"),
        recon_doc("R-OCT1", A, "2026-10-01T00:00:00.000001Z"),
        recon_doc("R-OCT31", A, "2026-10-31T23:59:59.999999Z"),
        recon_doc("R-NOV", A, "2026-11-01T00:00:00.000001Z"),
    ])
    ids = _ids(client.get(f"{EXPORTS['reconciliation']}?start_date=2026-10-01&end_date=2026-10-31"),
               "reconciliation")
    assert sorted(ids) == ["R-OCT1", "R-OCT31"]


def test_reconciliation_datetime_end_inclusive_at_exact_value(world, client):
    world.recon.seed(fuel_ops_endpoints.MVP_RECONCILIATION_INDEX,
                     [recon_doc("R1", A, "2026-10-01T12:00:00Z")])
    ids = _ids(client.get(f"{EXPORTS['reconciliation']}?end_date=2026-10-01T12:00:00Z"),
               "reconciliation")
    assert ids == ["R1"]


@pytest.mark.parametrize("export", ["reconciliation", "invoices"])
@pytest.mark.parametrize("qs", ["start_date=bogus", "end_date=2026-02-30",
                                "start_date=2026-10-02&end_date=2026-10-01"])
def test_typed_date_params_422(world, client, export, qs):
    resp = client.get(f"{EXPORTS[export]}?{qs}")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_reconciliation_min_variance_pushed_down(world, client):
    world.recon.seed(fuel_ops_endpoints.MVP_RECONCILIATION_INDEX, [
        recon_doc("R-LOW", A, variance_delivered_vs_loaded_pct=0.5),
        recon_doc("R-HIGH", A, variance_load_vs_order_pct=5.0),
        recon_doc("R-INV", A, variance_delivered_vs_loaded_pct=0.0,
                  variance_invoiced_vs_delivered_pct=3.0),
    ])
    ids = _ids(client.get(f"{EXPORTS['reconciliation']}?min_variance_pct=2"), "reconciliation")
    assert sorted(ids) == ["R-HIGH", "R-INV"]


def test_reconciliation_drops_invalid_and_mislabelled_rows(world, client):
    world.recon.seed(fuel_ops_endpoints.MVP_RECONCILIATION_INDEX, [
        recon_doc("R1", A), recon_doc("R2", A, ordered_gallons=-1.0), recon_doc("R3", A),
    ])
    ids = _ids(client.get(EXPORTS["reconciliation"]), "reconciliation")
    assert sorted(ids) == ["R1", "R3"]


def test_invoices_date_range_and_total_gallons(world, client):
    world.invoices.invoices += [
        invoice_doc("I-SEP", A, "2026-09-30T23:59:59+00:00"),
        invoice_doc("I-OCT", A, "2026-10-31T23:59:59+00:00"),
        invoice_doc("I-NOV", A, "2026-11-01T00:00:00+00:00"),
    ]
    rows = _rows(client.get(f"{EXPORTS['invoices']}?start_date=2026-10-01&end_date=2026-10-31"))
    ids = [r[rows[0].index("invoice_id")] for r in rows[1:]]
    assert ids == ["I-OCT"]
    assert rows[1][rows[0].index("total_gallons")] == "15.5"


def test_invoices_qbo_push_state_filters(world, client):
    world.invoices.invoices += [
        invoice_doc("I1", A, qbo_push_state="dead_letter"), invoice_doc("I2", A),
    ]
    ids = _ids(client.get(f"{EXPORTS['invoices']}?qbo_push_state=dead_letter"), "invoices")
    assert ids == ["I1"]
