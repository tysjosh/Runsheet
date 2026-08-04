"""Integration tests for InvoiceService dual-write into Postgres.

Verifies the gated bridge mirrors invoice generate / finalize / payment / void
into the Postgres source-of-truth + outbox, that invoice numbers are allocated
from the per-tenant Postgres counter (monotonic, gap-free under rollback), and
that everything is a strict no-op when the flag is off.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from commerce.services.invoice_service import InvoiceService
from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.models import InvoiceCounterORM, InvoiceORM, OutboxEventORM
from persistence.repositories import (
    AccountRepository,
    CustomerRepository,
    InvoiceRepository,
)

TENANT = "demo-tenant"


def _make_es():
    """ES mock: index/update are no-ops; search returns empty aggregations."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {"max_seq": {"value": None}},
    })
    return es


@pytest.fixture
def dual_write_on(monkeypatch):
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_dual_write_postgres is True
    yield
    clear_settings_cache()


async def _seed_parents(customer_id="cust_1", account_id="acct_1"):
    """Mirror parent customer + account so the invoice FK is satisfiable."""
    customers = CustomerRepository()
    accounts = AccountRepository()
    async with session_scope() as session:
        await customers.create(
            session, customer_id=customer_id, tenant_id=TENANT, display_name="Acme",
        )
        await accounts.create(
            session, account_id=account_id, tenant_id=TENANT, customer_id=customer_id,
            display_name="Acme — Net 30", credit_limit_cents=1_000_00,
        )


def _line_items(prefix="line"):
    """Fresh line items with a unique line_id (PK is global)."""
    from uuid import uuid4
    return [{
        "line_id": f"{prefix}_{uuid4()}", "product_code": "DSL",
        "quantity_gallons": 100.0, "unit_price_cents": 350,
        "unit_price_micros": 3_500_000, "subtotal_cents": 35000,
    }]


async def test_generate_from_order_mirrors_invoice(engine, dual_write_on):
    await _seed_parents()
    es = _make_es()
    service = InvoiceService(es)

    doc = await service.generate_from_order(
        tenant_id=TENANT, order_id="ORD-1", customer_id="cust_1",
        account_id="acct_1", line_items=_line_items(), tax_cents=500,
    )

    repo = InvoiceRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, doc["invoice_id"])
        assert row is not None
        assert row.status == "draft"
        assert row.subtotal_cents == 35000
        assert row.tax_cents == 500
        assert row.total_cents == 35500
        assert len(row.line_items) == 1
        assert row.line_items[0].product_code == "DSL"
        assert row.line_items[0].unit_price_micros == 3_500_000
        assert row.invoice_number is None  # not numbered until finalize


async def test_fractional_cent_price_survives_postgres_round_trip(
    engine, dual_write_on
):
    from uuid import uuid4

    await _seed_parents()
    service = InvoiceService(_make_es())
    doc = await service.generate_from_order(
        tenant_id=TENANT,
        order_id="W1736698",
        customer_id="cust_1",
        account_id="acct_1",
        line_items=[
            {
                "line_id": f"line_{uuid4()}",
                "product_code": "DIESEL_2",
                "quantity_gallons": 7_000.0,
                "unit_price_cents": 297,
                "unit_price_micros": 2_966_000,
                "subtotal_cents": 2_076_200,
            }
        ],
    )

    assert doc["subtotal_cents"] == 2_076_200
    async with session_scope() as session:
        row = await InvoiceRepository().get(
            session, TENANT, doc["invoice_id"]
        )
        assert row.line_items[0].unit_price_cents == 297
        assert row.line_items[0].unit_price_micros == 2_966_000
        assert row.line_items[0].subtotal_cents == 2_076_200


async def test_generate_mirrors_pod_actuals_to_authoritative_invoice(
    engine, dual_write_on
):
    await _seed_parents()
    service = InvoiceService(_make_es())
    delivery_result = {
        "pod_id": "pod-actual-1",
        "actual_gallons": 100.0,
        "actual_gallons_source": "manual",
        "delivered_at": "2026-07-29T14:42:18-04:00",
        "recipient_name": "Morgan Lee",
        "geotag": {"lat": 40.4167, "lon": -86.8753},
    }

    doc = await service.generate_from_order(
        tenant_id=TENANT,
        order_id="ORD-POD-1",
        customer_id="cust_1",
        account_id="acct_1",
        line_items=_line_items(),
        delivery_result=delivery_result,
    )

    async with session_scope() as session:
        row = await InvoiceRepository().get(
            session, TENANT, doc["invoice_id"]
        )
        assert row is not None
        assert row.pod_id == "pod-actual-1"
        assert row.delivery_result["actual_gallons"] == 100.0
        assert row.delivered_at is not None
        assert row.delivered_at.isoformat().startswith("2026-07-29T14:42:18")


async def test_generate_skips_when_parents_not_mirrored(engine, dual_write_on):
    """Without mirrored parents the invoice mirror skips (no FK violation)."""
    es = _make_es()
    service = InvoiceService(es)

    doc = await service.generate_from_order(
        tenant_id=TENANT, order_id="ORD-1", customer_id="cust_1",
        account_id="acct_1", line_items=_line_items(),
    )

    repo = InvoiceRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, doc["invoice_id"])
        assert row is None
        invoice_rows = await session.scalar(select(func.count()).select_from(InvoiceORM))
    assert invoice_rows == 0


async def test_finalize_allocates_monotonic_invoice_number(engine, dual_write_on):
    await _seed_parents()
    es = _make_es()
    service = InvoiceService(es)

    # Generate two invoices, then finalize both.
    doc1 = await service.generate_from_order(
        tenant_id=TENANT, order_id="ORD-1", customer_id="cust_1",
        account_id="acct_1", line_items=_line_items(),
    )
    doc2 = await service.generate_from_order(
        tenant_id=TENANT, order_id="ORD-2", customer_id="cust_1",
        account_id="acct_1", line_items=_line_items(),
    )

    # service.get() reads the projection from ES; return the generated docs.
    def _es_get(invoice_id, source):
        async def _search(index, query, size=10, **kwargs):
            if index == "invoices_current":
                return {"hits": {"hits": [{"_source": source}]}}
            return {"hits": {"hits": [], "total": {"value": 0}},
                    "aggregations": {"max_seq": {"value": 1}}}
        return _search

    es.search_documents = AsyncMock(side_effect=_es_get(doc1["invoice_id"], doc1))
    finalized1 = await service.finalize_draft(tenant_id=TENANT, invoice_id=doc1["invoice_id"])
    es.search_documents = AsyncMock(side_effect=_es_get(doc2["invoice_id"], doc2))
    finalized2 = await service.finalize_draft(tenant_id=TENANT, invoice_id=doc2["invoice_id"])

    assert finalized1["invoice_number"] == "INV-000001"
    assert finalized2["invoice_number"] == "INV-000002"

    repo = InvoiceRepository()
    async with session_scope() as session:
        row1 = await repo.get(session, TENANT, doc1["invoice_id"])
        row2 = await repo.get(session, TENANT, doc2["invoice_id"])
        assert row1.status == "open"
        assert row1.invoice_number == "INV-000001"
        assert row2.invoice_number == "INV-000002"
        # Counter advanced to next_seq=3.
        counter = await session.get(InvoiceCounterORM, TENANT)
        assert counter.next_seq == 3


async def test_invoice_number_unique_per_tenant_enforced(engine, dual_write_on):
    """The DB rejects a second invoice claiming an already-used number."""
    from sqlalchemy.exc import IntegrityError

    await _seed_parents()
    repo = InvoiceRepository()
    async with session_scope() as session:
        await repo.create(
            session, invoice_id="inv_a", tenant_id=TENANT, customer_id="cust_1",
            account_id="acct_1", invoice_number="INV-000001", line_items=_line_items(),
        )
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await repo.create(
                session, invoice_id="inv_b", tenant_id=TENANT, customer_id="cust_1",
                account_id="acct_1", invoice_number="INV-000001",
                line_items=_line_items(),
            )


async def test_apply_payment_mirrors_status(engine, dual_write_on):
    await _seed_parents()
    es = _make_es()
    service = InvoiceService(es)
    doc = await service.generate_from_order(
        tenant_id=TENANT, order_id="ORD-1", customer_id="cust_1",
        account_id="acct_1", line_items=_line_items(),
    )
    # Put invoice into 'open' so payment is allowed.
    doc["status"] = "open"

    async def _search(index, query, size=10, **kwargs):
        if index == "invoices_current":
            return {"hits": {"hits": [{"_source": doc}]}}
        return {"hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"max_seq": {"value": 1}}}
    es.search_documents = AsyncMock(side_effect=_search)

    await service.apply_payment(
        tenant_id=TENANT, invoice_id=doc["invoice_id"], amount_cents=10000,
    )

    repo = InvoiceRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, doc["invoice_id"])
        assert row.status == "partial"
        assert row.amount_paid_cents == 10000
        assert row.remaining_cents == 25000


async def test_void_mirrors_status(engine, dual_write_on):
    await _seed_parents()
    es = _make_es()
    service = InvoiceService(es)
    doc = await service.generate_from_order(
        tenant_id=TENANT, order_id="ORD-1", customer_id="cust_1",
        account_id="acct_1", line_items=_line_items(),
    )
    doc["status"] = "open"

    async def _search(index, query, size=10, **kwargs):
        if index == "invoices_current":
            return {"hits": {"hits": [{"_source": doc}]}}
        return {"hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"max_seq": {"value": 1}}}
    es.search_documents = AsyncMock(side_effect=_search)

    await service.void(
        tenant_id=TENANT, invoice_id=doc["invoice_id"], reason="customer_request",
        actor="admin@runsheet.com",
    )

    repo = InvoiceRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, doc["invoice_id"])
        assert row.status == "void"
        assert row.void_reason == "customer_request"
        assert row.voided_at is not None


async def test_dual_write_off_does_not_touch_postgres(engine, monkeypatch):
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "false")
    clear_settings_cache()

    es = _make_es()
    service = InvoiceService(es)
    await service.generate_from_order(
        tenant_id=TENANT, order_id="ORD-1", customer_id="cust_1",
        account_id="acct_1", line_items=_line_items(),
    )

    async with session_scope() as session:
        invoice_rows = await session.scalar(select(func.count()).select_from(InvoiceORM))
        event_rows = await session.scalar(select(func.count()).select_from(OutboxEventORM))
    assert invoice_rows == 0
    assert event_rows == 0


async def test_mark_overdue_mirrors_status_to_postgres(engine, dual_write_on, monkeypatch):
    """mark_overdue must mirror status=overdue to PG (regression).

    Previously it wrote the status to ES only, so the PG row stayed open and the
    PG-backed overdue sweep + invoice get re-marked the same invoice forever.
    """
    # Read from PG too, so service.get() (inside mark_overdue) sees the seeded
    # PG invoice and the idempotency short-circuit can engage on the 2nd call.
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()

    await _seed_parents()
    repo = InvoiceRepository()
    async with session_scope() as session:
        await repo.create(
            session, invoice_id="INV-OD", tenant_id=TENANT, customer_id="cust_1",
            account_id="acct_1", line_items=_line_items(), status="open",
            total_cents=35000, remaining_cents=35000,
        )
        await repo.set_fields(session, TENANT, "INV-OD", status="open",
                              due_date="2026-01-01")

    es = _make_es()
    service = InvoiceService(es)
    result = await service.mark_overdue(tenant_id=TENANT, invoice_id="INV-OD")
    assert result["status"] == "overdue"

    async with session_scope() as session:
        row = await repo.get(session, TENANT, "INV-OD")
        assert row.status == "overdue"  # mirrored to PG, not just ES

    # Idempotent: a second call sees overdue in PG and returns without a new event.
    again = await service.mark_overdue(tenant_id=TENANT, invoice_id="INV-OD")
    assert again["status"] == "overdue"
    clear_settings_cache()


def test_invoices_current_mapping_declares_last_applied_seq():
    """The strict invoices_current mapping must declare _last_applied_seq.

    _update_projection stamps this field on every status transition; if the
    strict mapping omits it the whole projection write is rejected and the
    transition silently fails (the bug that made overdue invoices re-mark
    every cycle).
    """
    from commerce.services.commerce_es_mappings import INVOICES_CURRENT_MAPPING

    props = INVOICES_CURRENT_MAPPING["mappings"]["properties"]
    assert INVOICES_CURRENT_MAPPING["mappings"]["dynamic"] == "strict"
    assert "_last_applied_seq" in props
    assert props["_last_applied_seq"]["type"] == "long"
