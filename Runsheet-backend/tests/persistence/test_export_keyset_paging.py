"""Keyset paging for the data export (design §2, DD-4), relational read path.

SQLite-backed, like ``test_hybrid_read_cutover.py``. The document-store path
needs jsonb and is covered in ``tests/postgres/test_export_keyset_doc_store.py``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.read_repositories import HybridReadRepository
from persistence.repositories import CurrentStateRepository
from services.csv_export import KeysetSource

TENANT = "tenant-a"
OTHER = "tenant-b"


@pytest.fixture
def read_from_pg(monkeypatch):
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_read_from_postgres is True
    yield
    clear_settings_cache()


def order_doc(order_id: str, created_at: str, tenant: str = TENANT, **extra) -> dict:
    doc = dict(
        order_id=order_id, tenant_id=tenant, customer_id="CUST-1",
        customer_name="Acme", ship_to_address="1 Main St",
        ship_to_lat=1.0, ship_to_lon=1.0, product_code="DIESEL_2",
        gallons_requested=100.0, call_type="will_call",
        intake_channel="dispatcher", intake_channel_id="x",
        source_schema_version="1", trace_id="tr",
        created_at=created_at, updated_at=created_at,
        last_event_timestamp=created_at,
    )
    doc.update(extra)
    return doc


def job_doc(job_id: str, scheduled_time: str, tenant: str = TENANT) -> dict:
    return {
        "job_id": job_id, "tenant_id": tenant, "job_type": "fuel_delivery",
        "status": "scheduled", "scheduled_time": scheduled_time,
        "created_at": scheduled_time, "origin": "A", "destination": "B",
    }


async def _seed(aggregate: str, docs: list[dict], id_field: str) -> None:
    async with session_scope() as s:
        for doc in docs:
            await CurrentStateRepository(aggregate).upsert(s, doc=doc, doc_id=doc[id_field])


def _orders_fixture() -> list[dict]:
    tie = "2026-10-02T00:00:00Z"
    return [
        order_doc("ORD-1", "2026-10-01T00:00:00Z"),
        order_doc("ORD-2", tie), order_doc("ORD-3", tie), order_doc("ORD-4", tie),
        order_doc("ORD-5", "2026-10-03T00:00:00Z"),
        order_doc("ORD-6", "2026-10-04T00:00:00Z"),
        order_doc("ORD-7", "2026-10-05T00:00:00Z"),
        order_doc("ORD-B1", tie, tenant=OTHER),
        order_doc("ORD-B2", "2026-10-03T00:00:00Z", tenant=OTHER),
        order_doc("ORD-B3", "2026-10-09T00:00:00Z", tenant=OTHER),
    ]


async def _page_all(repo, order: str) -> list[str]:
    seen: list[str] = []
    after = None
    while True:
        async with session_scope() as s:
            res = await repo.search(
                s, TENANT, sort_field="created_at", sort_order=order,
                size=2, after=after, with_total=False,
            )
        seen.extend(d["order_id"] for d in res["items"])
        if res["raw_count"] < 2 or res["last_key"] is None:
            return seen
        assert len(res["last_key"]) == 2
        after = res["last_key"]


@pytest.mark.parametrize("order", ["desc", "asc"])
async def test_hybrid_keyset_pages_every_row_once_with_ties(engine, order):
    await _seed("fuel_order", _orders_fixture(), "order_id")
    repo = HybridReadRepository("fuel_order")
    seen = await _page_all(repo, order)
    expected_asc = ["ORD-1", "ORD-2", "ORD-3", "ORD-4", "ORD-5", "ORD-6", "ORD-7"]
    if order == "asc":
        assert seen == expected_asc
    else:
        # DESC on created_at, ties broken by pk ASC.
        assert seen == ["ORD-7", "ORD-6", "ORD-5", "ORD-2", "ORD-3", "ORD-4", "ORD-1"]
    assert len(seen) == len(set(seen)) == 7


async def test_hybrid_with_total_false_skips_count(engine):
    await _seed("fuel_order", _orders_fixture(), "order_id")
    async with session_scope() as s:
        res = await HybridReadRepository("fuel_order").search(s, TENANT, size=2, with_total=False)
    assert res["total"] is None
    assert res["raw_count"] == 2


async def test_hybrid_no_after_matches_offset_contract(engine):
    await _seed("fuel_order", _orders_fixture(), "order_id")
    repo = HybridReadRepository("fuel_order")
    async with session_scope() as s:
        p2 = await repo.search(s, TENANT, size=2, page=2)
    assert [d["order_id"] for d in p2["items"]] == ["ORD-5", "ORD-2"]
    assert p2["total"] == 7 and p2["page"] == 2 and p2["size"] == 2
    assert set(p2) == {"items", "total", "page", "size", "raw_count", "last_key"}


# ---------------------------------------------------------------------------
# FuelOrderRepository / JobService wrappers, hybrid path
# ---------------------------------------------------------------------------


def _orders_repo():
    from fuel.order_repository import FuelOrderRepository
    es = AsyncMock()
    es.search_documents = AsyncMock(side_effect=AssertionError("ES must not be read"))
    return FuelOrderRepository(es)


async def test_orders_after_without_keyset_raises():
    with pytest.raises(ValueError):
        await _orders_repo().search(TENANT, after=("x", "y"))


async def test_jobs_after_without_keyset_raises():
    from scheduling.services.job_service import JobService
    svc = JobService.__new__(JobService)
    with pytest.raises(ValueError):
        await svc.list_jobs(TENANT, after=("x", "y"))


async def test_orders_export_adapter_hybrid_drops_invalid_row(engine, read_from_pg):
    from fuel.api.order_endpoints import _orders_export_fetch
    docs = [order_doc(f"ORD-{i}", f"2026-10-0{i}T00:00:00Z") for i in range(1, 6)]
    docs[1]["call_type"] = "not-a-call-type"  # 2nd row fails FuelOrder validation
    await _seed("fuel_order", docs, "order_id")
    repo = _orders_repo()
    fetch = _orders_export_fetch(repo, TENANT, {"sort": None})
    src = KeysetSource(fetch, page_size=2)
    assert await src.count() == 5
    written = [r["order_id"] async for page in src.pages() for r in page]
    assert written == ["ORD-5", "ORD-4", "ORD-3", "ORD-1"]


async def test_jobs_export_adapter_hybrid_ties_asc(engine, read_from_pg):
    from scheduling.api.endpoints import _jobs_export_fetch
    from scheduling.services.job_service import JobService
    tie = "2026-10-02T08:00:00Z"
    docs = [job_doc(f"JOB-{i}", tie) for i in range(1, 6)] + [job_doc("JOB-B", tie, OTHER)]
    await _seed("job", docs, "job_id")
    svc = JobService.__new__(JobService)
    svc._es = AsyncMock()
    svc._es.search_documents = AsyncMock(side_effect=AssertionError("ES must not be read"))
    fetch = _jobs_export_fetch(svc, TENANT, {"sort_by": "scheduled_time", "sort_order": "asc"})
    src = KeysetSource(fetch, page_size=2)
    assert await src.count() == 5
    written = [r["job_id"] async for page in src.pages() for r in page]
    assert written == ["JOB-1", "JOB-2", "JOB-3", "JOB-4", "JOB-5"]
