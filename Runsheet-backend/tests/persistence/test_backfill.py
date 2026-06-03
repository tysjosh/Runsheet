"""Tests for the one-time ES → Postgres backfill (Phase 3).

Uses a fake synchronous ES client that serves canned ``*_current`` docs via the
scroll API, and asserts the backfill inserts rows in dependency order, is
idempotent on re-run, and honors --dry-run.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select

from persistence.backfill import backfill
from persistence.database import session_scope
from persistence.models import (
    AccountORM,
    CustomerORM,
    InvoiceORM,
    PaymentORM,
)

TENANT = "demo-tenant"

_DOCS = {
    "customers_current": [
        {"customer_id": "cust_1", "tenant_id": TENANT, "display_name": "Acme",
         "status": "active", "external_refs": {}, "metadata": {},
         "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"},
    ],
    "accounts_current": [
        {"account_id": "acct_1", "tenant_id": TENANT, "customer_id": "cust_1",
         "display_name": "Acme — Net 30", "status": "active",
         "credit_limit_cents": 100000, "open_balance_cents": 0,
         "available_credit_cents": 100000, "credit_balance_cents": 0,
         "credit_state": "ok", "net_terms_days": 30, "tier": "default",
         "payment_method_preference": "invoice", "external_refs": {},
         "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"},
    ],
    "invoices_current": [
        {"invoice_id": "inv_1", "tenant_id": TENANT, "customer_id": "cust_1",
         "account_id": "acct_1", "order_id": "ORD-1", "invoice_number": "INV-000001",
         "status": "open", "total_cents": 35000, "amount_paid_cents": 0,
         "remaining_cents": 35000, "tax_cents": 0, "subtotal_cents": 35000,
         "line_items": [{"line_id": "line_1", "product_code": "DSL",
                         "quantity_gallons": 100.0, "unit_price_cents": 350,
                         "subtotal_cents": 35000}],
         "due_date": "2026-02-01", "issued_at": "2026-01-01T00:00:00+00:00",
         "external_refs": {}, "created_at": "2026-01-01T00:00:00+00:00",
         "updated_at": "2026-01-01T00:00:00+00:00"},
    ],
    "payments_current": [
        {"payment_id": "pay_1", "tenant_id": TENANT, "invoice_id": "inv_1",
         "account_id": "acct_1", "amount_cents": 10000, "source": "stripe",
         "method": "card", "external_id": "ch_1", "status": "applied",
         "applied_at": "2026-01-02T00:00:00+00:00"},
    ],
}


class _FakeES:
    """Minimal stand-in for the ES client's scroll API."""

    def __init__(self, docs):
        self._docs = docs
        self.indices = MagicMock()
        self.indices.exists = MagicMock(return_value=True)

    def search(self, index, query, size, scroll):
        hits = [{"_source": d} for d in self._docs.get(index, [])]
        return {"_scroll_id": f"sc_{index}", "hits": {"hits": hits}}

    def scroll(self, scroll_id, scroll):
        # All docs returned on the first page; subsequent scroll is empty.
        return {"_scroll_id": scroll_id, "hits": {"hits": []}}


@pytest.fixture
def patched_es(monkeypatch):
    fake = _FakeES(_DOCS)
    import persistence.backfill as bf
    # elasticsearch_service is imported lazily inside backfill(); patch the
    # singleton's client attribute.
    from services.elasticsearch_service import elasticsearch_service
    monkeypatch.setattr(elasticsearch_service, "client", fake, raising=False)
    return fake


async def test_backfill_inserts_all_rows(engine, patched_es):
    counts = await backfill(TENANT)
    assert counts["customers"] == 1
    assert counts["accounts"] == 1
    assert counts["invoices"] == 1
    assert counts["payments"] == 1

    async with session_scope() as session:
        assert await session.get(CustomerORM, "cust_1") is not None
        assert await session.get(AccountORM, "acct_1") is not None
        inv = await session.get(InvoiceORM, "inv_1")
        assert inv is not None
        assert inv.invoice_number == "INV-000001"
        assert await session.get(PaymentORM, "pay_1") is not None


async def test_backfill_is_idempotent(engine, patched_es):
    first = await backfill(TENANT)
    assert first["customers"] == 1
    # Second run: everything already present -> zero inserts across all aggregates.
    second = await backfill(TENANT)
    assert all(v == 0 for v in second.values())

    async with session_scope() as session:
        customer_count = await session.scalar(select(func.count()).select_from(CustomerORM))
    assert customer_count == 1


async def test_backfill_dry_run_writes_nothing(engine, patched_es):
    counts = await backfill(TENANT, dry_run=True)
    assert counts["customers"] == 1
    assert counts["payments"] == 1

    async with session_scope() as session:
        customer_count = await session.scalar(select(func.count()).select_from(CustomerORM))
        payment_count = await session.scalar(select(func.count()).select_from(PaymentORM))
    assert customer_count == 0
    assert payment_count == 0
