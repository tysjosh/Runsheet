"""Data-export keyset paging through the real document store (design §2, §10).

Builds the orders, jobs and reconciliation export sources exactly as the
handlers do (``keyset=True`` adapters, ``KeysetSource``), from ``after=None``,
with ``page_size=2``, over a ``PostgresDocumentStore``. Nothing here builds a
``search_after`` by hand.
"""
from __future__ import annotations

import pytest

from config.settings import clear_settings_cache, get_settings
from services.csv_export import KeysetSource

A, B = "tenant-a", "tenant-b"


@pytest.fixture(autouse=True)
def _doc_store_read_path(monkeypatch):
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "false")
    clear_settings_cache()
    assert get_settings().commerce_read_from_postgres is False
    yield
    clear_settings_cache()


def _order(order_id, tenant, created_at, **extra):
    doc = dict(
        order_id=order_id, tenant_id=tenant, customer_id="CUST-1",
        customer_name="Acme", ship_to_address="1 Main St",
        ship_to_lat=1.0, ship_to_lon=1.0, product_code="DIESEL_2",
        gallons_requested=100.0, call_type="will_call",
        intake_channel="dispatcher", intake_channel_id="x",
        source_schema_version="1", trace_id="tr", created_at=created_at,
        updated_at=created_at, last_event_timestamp=created_at,
    )
    doc.update(extra)
    return doc


async def _seed(store, index, docs, id_field):
    for doc in docs:
        await store.index_document(index, doc[id_field], dict(doc))


class _Recording:
    """Wraps the store to record every page's ``last_key``."""

    def __init__(self, fetch):
        self.fetch = fetch
        self.keys = []

    async def __call__(self, after, size, with_total):
        page = await self.fetch(after, size, with_total)
        if page.last_key is not None:
            self.keys.append(page.last_key)
        return page


async def _collect(src):
    return [r async for page in src.pages() for r in page]


def _orders_fixture():
    tie = "2026-10-02T00:00:00Z"
    return [
        _order("ORD-1", A, "2026-10-01T00:00:00Z"),
        _order("ORD-2", A, tie), _order("ORD-3", A, tie), _order("ORD-4", A, tie),
        _order("ORD-5", A, "2026-10-03T00:00:00Z"),
        _order("ORD-B1", B, tie), _order("ORD-B2", B, "2026-10-09T00:00:00Z"),
    ]


@pytest.mark.parametrize("sort,expected", [
    (None, ["ORD-5", "ORD-2", "ORD-3", "ORD-4", "ORD-1"]),
    ("created_at:asc", ["ORD-1", "ORD-2", "ORD-3", "ORD-4", "ORD-5"]),
])
async def test_orders_export_source_doc_store(store, index_name, sort, expected):
    from fuel.api.order_endpoints import _orders_export_fetch
    from fuel.order_repository import FuelOrderRepository

    await _seed(store, index_name, _orders_fixture(), "order_id")
    repo = FuelOrderRepository(store, orders_index=index_name)
    rec = _Recording(_orders_export_fetch(repo, A, {"sort": sort}))
    src = KeysetSource(rec, page_size=2)
    assert await src.count() == 5
    rows = await _collect(src)
    assert [r["order_id"] for r in rows] == expected
    assert rec.keys and all(len(k) == 2 for k in rec.keys)


async def test_orders_export_validation_drop_does_not_stop_paging(store, index_name):
    from fuel.api.order_endpoints import _orders_export_fetch
    from fuel.order_repository import FuelOrderRepository

    docs = [_order(f"ORD-{i}", A, f"2026-10-0{i}T00:00:00Z") for i in range(1, 6)]
    docs[3]["call_type"] = "bogus"  # ORD-4: 2nd row in DESC order fails validation
    await _seed(store, index_name, docs, "order_id")
    repo = FuelOrderRepository(store, orders_index=index_name)
    rows = await _collect(KeysetSource(_orders_export_fetch(repo, A, {"sort": None}), page_size=2))
    assert [r["order_id"] for r in rows] == ["ORD-5", "ORD-3", "ORD-2", "ORD-1"]


@pytest.mark.parametrize("order", ["asc", "desc"])
async def test_jobs_export_source_doc_store_ties(store, index_name, monkeypatch, order):
    from scheduling.api.endpoints import _jobs_export_fetch
    from scheduling.services import job_service as job_service_module
    from scheduling.services.job_service import JobService

    monkeypatch.setattr(job_service_module, "JOBS_CURRENT_INDEX", index_name)
    tie = "2026-10-02T08:00:00Z"
    jobs = [{"job_id": f"JOB-{i}", "tenant_id": A, "scheduled_time": tie,
             "created_at": tie, "status": "scheduled", "job_type": "fuel_delivery"}
            for i in range(1, 6)]
    jobs.append({"job_id": "JOB-B", "tenant_id": B, "scheduled_time": tie,
                 "created_at": tie, "status": "scheduled", "job_type": "fuel_delivery"})
    await _seed(store, index_name, jobs, "job_id")
    svc = JobService.__new__(JobService)
    svc._es = store
    rec = _Recording(_jobs_export_fetch(svc, A, {"sort_by": "scheduled_time", "sort_order": order}))
    src = KeysetSource(rec, page_size=2)
    assert await src.count() == 5
    rows = await _collect(src)
    assert [r["job_id"] for r in rows] == [f"JOB-{i}" for i in range(1, 6)]
    assert all(len(k) == 2 for k in rec.keys)


def _recon(rid, tenant, generated_at, **extra):
    doc = {"reconciliation_id": rid, "tenant_id": tenant, "order_id": f"O-{rid}",
           "plan_id": "P", "pod_id": "POD", "ordered_gallons": 100.0,
           "loaded_gallons": 100.0, "delivered_gallons": 99.0,
           "variance_load_vs_order_pct": 0.0, "variance_delivered_vs_loaded_pct": 0.0,
           "variance_invoiced_vs_delivered_pct": None, "alert_flags": [],
           "generated_at": generated_at, "created_at": generated_at, "updated_at": generated_at}
    doc.update(extra)
    return doc


def _recon_fixture():
    return [
        _recon("R1", A, "2026-10-01T00:00:00.000001Z", variance_load_vs_order_pct=5.0),
        _recon("R2", A, "2026-10-02T00:00:00Z", variance_delivered_vs_loaded_pct=1.0),
        _recon("R3", A, "2026-10-03T00:00:00.5Z", variance_invoiced_vs_delivered_pct=2.5),
        _recon("R4", A, "2026-10-03T00:00:00.5Z", variance_invoiced_vs_delivered_pct=None),
        _recon("R5", A, "2026-10-04T00:00:00Z", variance_delivered_vs_loaded_pct=2.0),
        _recon("RB", B, "2026-10-04T00:00:00Z", variance_load_vs_order_pct=9.0),
    ]


def _list_post_hoc(docs, tenant, threshold):
    """The list endpoint's post-hoc min_variance_pct filter."""
    out = []
    for d in docs:
        if d["tenant_id"] != tenant:
            continue
        vals = (d["variance_load_vs_order_pct"], d["variance_delivered_vs_loaded_pct"],
                d["variance_invoiced_vs_delivered_pct"])
        if any(v is not None and abs(float(v)) >= threshold for v in vals):
            out.append(d["reconciliation_id"])
    return sorted(out)


@pytest.mark.parametrize("threshold", [0.0, 1.0, 2.0, 2.5, 10.0])
async def test_reconciliation_min_variance_push_down_parity(store, index_name, monkeypatch, threshold):
    from fuel.api import fuel_ops_endpoints as foe
    from services.date_range import parse_date_range

    monkeypatch.setattr(foe, "MVP_RECONCILIATION_INDEX", index_name)
    docs = _recon_fixture()
    await _seed(store, index_name, docs, "reconciliation_id")
    q = foe._reconciliation_export_query(A, None, None, None, threshold, parse_date_range(None, None))
    src = KeysetSource(foe._reconciliation_export_fetch(store, A, q), page_size=2)
    rows = await _collect(src)
    expected = _list_post_hoc(docs, A, threshold)
    assert sorted(r["reconciliation_id"] for r in rows) == expected
    assert await src.count() == len(expected)


async def test_reconciliation_export_order_and_validation_drop(store, index_name, monkeypatch):
    from fuel.api import fuel_ops_endpoints as foe
    from services.date_range import parse_date_range

    monkeypatch.setattr(foe, "MVP_RECONCILIATION_INDEX", index_name)
    docs = [_recon(f"R{i}", A, f"2026-10-0{i}T00:00:00.000001Z") for i in range(1, 6)]
    docs[3]["ordered_gallons"] = -1.0  # R4 is 2nd in DESC order and fails validation
    await _seed(store, index_name, docs, "reconciliation_id")
    q = foe._reconciliation_export_query(A, None, None, None, None, parse_date_range(None, None))
    rec = _Recording(foe._reconciliation_export_fetch(store, A, q))
    rows = await _collect(KeysetSource(rec, page_size=2))
    assert [r["reconciliation_id"] for r in rows] == ["R5", "R3", "R2", "R1"]
    assert all(len(k) == 2 for k in rec.keys)


async def test_reconciliation_date_bounds_against_both_stored_forms(store, index_name, monkeypatch):
    """Bounds compare correctly whether pydantic wrote ``SSZ`` or ``SS.ffffffZ``."""
    from fuel.api import fuel_ops_endpoints as foe
    from services.date_range import parse_date_range

    monkeypatch.setattr(foe, "MVP_RECONCILIATION_INDEX", index_name)
    docs = [
        _recon("SEP", A, "2026-09-30T23:59:59.999999Z"),
        _recon("OCT1", A, "2026-10-01T00:00:00Z"),
        _recon("OCT1u", A, "2026-10-01T00:00:00.000001Z"),
        _recon("OCT31", A, "2026-10-31T23:59:59.999999Z"),
        _recon("NOV", A, "2026-11-01T00:00:00Z"),
        _recon("NOVu", A, "2026-11-01T00:00:00.000001Z"),
    ]
    await _seed(store, index_name, docs, "reconciliation_id")
    q = foe._reconciliation_export_query(
        A, None, None, None, None, parse_date_range("2026-10-01", "2026-10-31"),
    )
    rows = await _collect(KeysetSource(foe._reconciliation_export_fetch(store, A, q), page_size=2))
    assert sorted(r["reconciliation_id"] for r in rows) == ["OCT1", "OCT1u", "OCT31"]
