"""Integration tests for PaymentService dual-write + authoritative dedupe.

Covers two modes:

1. Soak (commerce_dual_write_postgres on): ingest/reverse mirror the payment
   into Postgres + outbox best-effort; ES stays the read path.
2. Authoritative (commerce_payments_authoritative on): ingest inserts into
   Postgres FIRST and the unique (tenant, source, external_id) constraint
   REJECTS a re-delivered webhook even under concurrency — the service returns
   the existing payment idempotently instead of creating a duplicate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from commerce.services.payment_service import PaymentService
from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.models import PaymentORM, OutboxEventORM
from persistence.repositories import (
    AccountRepository,
    CustomerRepository,
    InvoiceRepository,
    PaymentRepository,
)

TENANT = "demo-tenant"


def _make_es():
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


@pytest.fixture
def payments_authoritative(monkeypatch):
    monkeypatch.setenv("COMMERCE_PAYMENTS_AUTHORITATIVE", "true")
    clear_settings_cache()
    assert get_settings().commerce_payments_authoritative is True
    yield
    clear_settings_cache()


async def _seed_invoice(invoice_id="inv_1", customer_id="cust_1", account_id="acct_1"):
    """Mirror customer + account + invoice so the payment FKs are satisfiable."""
    customers = CustomerRepository()
    accounts = AccountRepository()
    invoices = InvoiceRepository()
    async with session_scope() as session:
        await customers.create(
            session, customer_id=customer_id, tenant_id=TENANT, display_name="Acme",
        )
        await accounts.create(
            session, account_id=account_id, tenant_id=TENANT, customer_id=customer_id,
            display_name="Acme — Net 30", credit_limit_cents=1_000_00,
        )
        await invoices.create(
            session, invoice_id=invoice_id, tenant_id=TENANT, customer_id=customer_id,
            account_id=account_id, invoice_number="INV-000001",
            line_items=[{"line_id": "line_1", "product_code": "DSL",
                         "quantity_gallons": 100.0, "unit_price_cents": 350,
                         "subtotal_cents": 35000}],
            total_cents=35000, remaining_cents=35000,
        )


def _invoice_aware_es(invoice_doc):
    """ES mock whose invoice get returns invoice_doc (for apply_payment)."""
    es = _make_es()

    async def _search(index, query, size=10, **kwargs):
        if index == "invoices_current":
            return {"hits": {"hits": [{"_source": invoice_doc}]}}
        return {"hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"max_seq": {"value": 1}}}

    es.search_documents = AsyncMock(side_effect=_search)
    return es


# ---------------------------------------------------------------------------
# Soak dual-write
# ---------------------------------------------------------------------------


async def test_ingest_mirrors_payment(engine, dual_write_on):
    await _seed_invoice()
    invoice_doc = {"invoice_id": "inv_1", "remaining_cents": 35000,
                   "total_cents": 35000, "amount_paid_cents": 0, "status": "open"}
    es = _invoice_aware_es(invoice_doc)
    # No invoice_service wired -> ingest just records the payment.
    service = PaymentService(es)

    doc = await service.ingest(
        tenant_id=TENANT, invoice_id="inv_1", account_id="acct_1",
        amount_cents=35000, source="stripe", method="card", external_id="ch_1",
    )

    repo = PaymentRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, doc["payment_id"])
        assert row is not None
        assert row.amount_cents == 35000
        assert row.source == "stripe"
        assert row.external_id == "ch_1"


async def test_reverse_mirrors_status(engine, dual_write_on):
    await _seed_invoice()
    es = _make_es()
    service = PaymentService(es)
    doc = await service.ingest(
        tenant_id=TENANT, invoice_id="inv_1", account_id="acct_1",
        amount_cents=35000, source="stripe", method="card", external_id="ch_1",
    )

    # service.get(payment) reads ES; return the created payment.
    async def _search(index, query, size=10, **kwargs):
        if index == "payments_current":
            return {"hits": {"hits": [{"_source": doc}]}}
        return {"hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"max_seq": {"value": None}}}
    es.search_documents = AsyncMock(side_effect=_search)

    await service.reverse(tenant_id=TENANT, payment_id=doc["payment_id"])

    repo = PaymentRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, doc["payment_id"])
        assert row.status == "reversed"
        assert row.reversed_at is not None


# ---------------------------------------------------------------------------
# Authoritative dedupe (the correctness payoff)
# ---------------------------------------------------------------------------


async def test_authoritative_ingest_dedupes_redelivered_webhook(engine, payments_authoritative):
    await _seed_invoice()
    invoice_doc = {"invoice_id": "inv_1", "remaining_cents": 35000,
                   "total_cents": 35000, "amount_paid_cents": 0, "status": "open"}

    es1 = _invoice_aware_es(invoice_doc)
    service1 = PaymentService(es1)
    first = await service1.ingest(
        tenant_id=TENANT, invoice_id="inv_1", account_id="acct_1",
        amount_cents=35000, source="stripe", method="card", external_id="ch_dup",
    )

    # Re-deliver the SAME Stripe charge (different payment_id, same external_id).
    es2 = _invoice_aware_es(invoice_doc)
    service2 = PaymentService(es2)
    second = await service2.ingest(
        tenant_id=TENANT, invoice_id="inv_1", account_id="acct_1",
        amount_cents=35000, source="stripe", method="card", external_id="ch_dup",
    )

    # Idempotent: the second call returns the FIRST payment, and exactly one
    # payment row exists in the source-of-truth.
    assert second["payment_id"] == first["payment_id"]
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count()).select_from(PaymentORM)
            .where(PaymentORM.external_id == "ch_dup")
        )
    assert count == 1


async def test_authoritative_ingest_persists_when_parents_present(engine, payments_authoritative):
    await _seed_invoice()
    invoice_doc = {"invoice_id": "inv_1", "remaining_cents": 35000,
                   "total_cents": 35000, "amount_paid_cents": 0, "status": "open"}
    es = _invoice_aware_es(invoice_doc)
    service = PaymentService(es)

    doc = await service.ingest(
        tenant_id=TENANT, invoice_id="inv_1", account_id="acct_1",
        amount_cents=35000, source="qbo", method="ach", external_id="qbo_99",
    )

    repo = PaymentRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, doc["payment_id"])
        assert row is not None
        assert row.source == "qbo"


async def test_authoritative_falls_back_when_parents_missing(engine, payments_authoritative):
    """No mirrored invoice/account -> authoritative insert skips; ES still gets the write."""
    es = _make_es()
    service = PaymentService(es)

    doc = await service.ingest(
        tenant_id=TENANT, invoice_id="inv_missing", account_id="acct_missing",
        amount_cents=100, source="stripe", method="card", external_id="ch_x",
    )

    es.index_document.assert_awaited()  # ES write happened
    repo = PaymentRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, doc["payment_id"])
        assert row is None  # not inserted (parents missing)


async def test_dual_write_off_does_not_touch_postgres(engine, monkeypatch):
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "false")
    monkeypatch.setenv("COMMERCE_PAYMENTS_AUTHORITATIVE", "false")
    clear_settings_cache()

    es = _make_es()
    service = PaymentService(es)
    await service.ingest(
        tenant_id=TENANT, invoice_id="inv_1", account_id="acct_1",
        amount_cents=100, source="manual", method="check", external_id="chk_1",
    )

    async with session_scope() as session:
        payment_rows = await session.scalar(select(func.count()).select_from(PaymentORM))
        event_rows = await session.scalar(select(func.count()).select_from(OutboxEventORM))
    assert payment_rows == 0
    assert event_rows == 0
