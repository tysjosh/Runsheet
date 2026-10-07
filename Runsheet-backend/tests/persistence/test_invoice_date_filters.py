"""G2 on the invoice Postgres read path: date range, qbo_push_state, count."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from commerce.services.invoice_service import InvoiceService
from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.repositories import InvoiceRepository

A, B = "tenant-a", "tenant-b"
UTC = timezone.utc


@pytest.fixture
def read_from_pg(monkeypatch):
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_read_from_postgres is True
    yield
    clear_settings_cache()


def _service():
    es = AsyncMock()
    es.search_documents = AsyncMock(side_effect=AssertionError("ES must not be read"))
    return InvoiceService(es)


async def _seed():
    rows = [
        ("I-SEP", A, datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC), "pending"),
        ("I-OCT1", A, datetime(2026, 10, 1, tzinfo=UTC), "dead_letter"),
        ("I-OCT31", A, datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC), "pending"),
        ("I-NOV", A, datetime(2026, 11, 1, tzinfo=UTC), "pending"),
        ("I-B", B, datetime(2026, 10, 15, tzinfo=UTC), "dead_letter"),
    ]
    repo = InvoiceRepository()
    async with session_scope() as session:
        for invoice_id, tenant, created, qbo in rows:
            row = await repo.create(
                session, invoice_id=invoice_id, tenant_id=tenant,
                customer_id="cust_1", account_id="acct_1",
                line_items=[{"line_id": f"l-{invoice_id}", "product_code": "DSL",
                             "quantity_gallons": 10.0, "unit_price_cents": 300,
                             "subtotal_cents": 3000}],
            )
            row.created_at = created
            row.qbo_push_state = qbo
        await session.flush()


async def test_pg_date_range_and_count(engine, read_from_pg):
    await _seed()
    svc = _service()
    kwargs = dict(created_from=datetime(2026, 10, 1, tzinfo=UTC),
                  created_before=datetime(2026, 11, 1, tzinfo=UTC))
    listed = await svc.list(tenant_id=A, limit=200, **kwargs)
    assert [i["invoice_id"] for i in listed["items"]] == ["I-OCT31", "I-OCT1"]
    assert await svc.count(tenant_id=A, **kwargs) == 2
    assert await svc.count(tenant_id=A) == 4


async def test_pg_inclusive_until(engine, read_from_pg):
    await _seed()
    listed = await _service().list(
        tenant_id=A, created_until=datetime(2026, 10, 1, tzinfo=UTC), limit=200,
    )
    assert [i["invoice_id"] for i in listed["items"]] == ["I-OCT1", "I-SEP"]


async def test_pg_qbo_push_state_filters(engine, read_from_pg):
    await _seed()
    svc = _service()
    listed = await svc.list(tenant_id=A, qbo_push_state="dead_letter", limit=200)
    assert [i["invoice_id"] for i in listed["items"]] == ["I-OCT1"]
    assert await svc.count(tenant_id=A, qbo_push_state="dead_letter") == 1


async def test_pg_omitted_filters_unchanged(engine, read_from_pg):
    await _seed()
    listed = await _service().list(tenant_id=A, limit=200)
    assert [i["invoice_id"] for i in listed["items"]] == ["I-NOV", "I-OCT31", "I-OCT1", "I-SEP"]


# --- updated_from (margin gap sweep): a filter on both read paths ----------

_UPDATED = {
    "I-SEP": datetime(2026, 10, 20, tzinfo=UTC),  # old invoice touched late (e.g. a payment)
    "I-OCT1": datetime(2026, 10, 1, tzinfo=UTC),
    "I-OCT31": datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC),
    "I-NOV": datetime(2026, 11, 1, tzinfo=UTC),
    "I-B": datetime(2026, 11, 2, tzinfo=UTC),
}


async def _seed_updated():
    from sqlalchemy import select

    from persistence.models import InvoiceORM

    await _seed()
    async with session_scope() as session:
        for row in (await session.execute(select(InvoiceORM))).scalars():
            row.updated_at = _UPDATED[row.invoice_id]
        await session.flush()


async def test_pg_updated_from_filters_and_keeps_created_at_order(engine, read_from_pg):
    await _seed_updated()
    svc = _service()
    bound = datetime(2026, 10, 15, tzinfo=UTC)
    listed = await svc.list(tenant_id=A, updated_from=bound, limit=200)
    # Filter on updated_at; order stays created_at desc (keyset unchanged).
    assert [i["invoice_id"] for i in listed["items"]] == ["I-NOV", "I-OCT31", "I-SEP"]
    assert await svc.count(tenant_id=A, updated_from=bound) == 3
    combined = await svc.list(
        tenant_id=A, updated_from=bound, created_from=datetime(2026, 10, 1, tzinfo=UTC), limit=200
    )
    assert [i["invoice_id"] for i in combined["items"]] == ["I-NOV", "I-OCT31"]
    page = await svc.list(tenant_id=A, updated_from=bound, limit=2)
    rest = await svc.list(tenant_id=A, updated_from=bound, limit=2, cursor=page["next_cursor"])
    assert [i["invoice_id"] for i in page["items"] + rest["items"]] == ["I-NOV", "I-OCT31", "I-SEP"]


async def test_es_updated_from_adds_a_range_clause():
    es = AsyncMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": [], "total": {"value": 0}}})
    svc = InvoiceService(es)
    bound = datetime(2026, 10, 15, tzinfo=UTC)
    await svc.list(tenant_id=A, updated_from=bound, limit=50)
    await svc.count(tenant_id=A, updated_from=bound)
    for call in es.search_documents.await_args_list:
        body = call.args[1]
        text = repr(body)
        assert "'updated_at': {'gte': '2026-10-15T00:00:00+00:00'}" in text
    list_body = es.search_documents.await_args_list[0].args[1]
    assert list_body["sort"] == [{"created_at": {"order": "desc"}}, {"invoice_id": {"order": "asc"}}]
    # Omitted -> no updated_at clause (unchanged behaviour).
    es.search_documents.reset_mock()
    await svc.list(tenant_id=A, limit=50)
    assert "updated_at" not in repr(es.search_documents.await_args_list[0].args[1])
