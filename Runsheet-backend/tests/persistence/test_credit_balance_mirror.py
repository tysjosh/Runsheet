"""The account credit balance and its synthetic payment reach Postgres (OI-13, OI-42).

``PaymentService._accrue_credit_balance`` (overpayment) and
``InvoiceService._drain_credit_balance`` (credit applied to a draft invoice)
both change ``accounts.credit_balance_cents``. Under
``COMMERCE_READ_FROM_POSTGRES`` the account GET reads that row, so both paths
mirror the new balance. The drain also creates a synthetic ``account_credit``
payment, which used to be written to the document store only.

These tests drive the real services against in-memory SQLite with dual-write
and read-cutover on, following ``test_credit_service_dual_write.py``.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from commerce.services.account_service import AccountService
from commerce.services.invoice_service import InvoiceService
from commerce.services.payment_service import PaymentService
from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.repositories import (
    AccountRepository,
    CustomerRepository,
    InvoiceRepository,
    PaymentRepository,
)

TENANT = "demo-tenant"
ACCOUNT = "acct_credit_bal_1"
INVOICE = "inv_credit_bal_1"


class _Store:
    """Document-store stand-in that keeps the account doc the services read."""

    def __init__(self, credit_balance_cents: int = 0) -> None:
        self.account: Dict[str, Any] = {
            "account_id": ACCOUNT,
            "tenant_id": TENANT,
            "credit_balance_cents": credit_balance_cents,
        }
        self.indexed: list = []
        self.index_document = AsyncMock(side_effect=self._index)
        self.update_document = AsyncMock(side_effect=self._update)
        self.search_documents = AsyncMock(side_effect=self._search)

    async def _index(self, index, doc_id, document):
        self.indexed.append((index, doc_id, dict(document)))
        return {"result": "created"}

    async def _update(self, index, doc_id, partial):
        if index == "accounts_current" and doc_id == ACCOUNT:
            self.account.update(partial)
        return {"result": "updated"}

    async def _search(self, index, query, size=10, **kwargs):
        if index == "accounts_current":
            return {"hits": {"hits": [{"_source": dict(self.account)}]}}
        return {"hits": {"hits": [], "total": {"value": 0}}, "aggregations": {}}

    def payments(self):
        return [doc for index, _, doc in self.indexed if index == "payments_current"]


@pytest.fixture
def pg_reads_on(monkeypatch):
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_read_from_postgres is True
    yield
    clear_settings_cache()


async def _seed(*, credit_balance_cents: int = 0, invoice_total_cents: int = 500):
    async with session_scope() as session:
        await CustomerRepository().create(
            session, customer_id="cust_cb", tenant_id=TENANT, display_name="Acme",
        )
        await AccountRepository().create(
            session, account_id=ACCOUNT, tenant_id=TENANT, customer_id="cust_cb",
            display_name="Acme Net 30", credit_limit_cents=1_000_00,
        )
        if credit_balance_cents:
            await AccountRepository().set_fields(
                session, TENANT, ACCOUNT, credit_balance_cents=credit_balance_cents
            )
        await InvoiceRepository().create(
            session, invoice_id=INVOICE, tenant_id=TENANT, customer_id="cust_cb",
            account_id=ACCOUNT, status="draft",
            line_items=[{
                "line_id": "li_cb_1", "product_code": "DIESEL_2",
                "quantity_gallons": 100, "unit_price_cents": invoice_total_cents // 100,
                "subtotal_cents": invoice_total_cents,
            }],
            subtotal_cents=invoice_total_cents, total_cents=invoice_total_cents,
        )


async def _account_row():
    async with session_scope() as session:
        return await AccountRepository().get(session, TENANT, ACCOUNT)


def _draft_invoice(total_cents: int = 500) -> Dict[str, Any]:
    return {
        "invoice_id": INVOICE,
        "tenant_id": TENANT,
        "account_id": ACCOUNT,
        "status": "draft",
        "total_cents": total_cents,
        "amount_paid_cents": 0,
        "remaining_cents": total_cents,
    }


async def test_overpayment_accrual_reaches_the_accounts_row(engine, pg_reads_on):
    """OI-13: the accrued balance is on the row the account GET reads."""
    await _seed()
    store = _Store(credit_balance_cents=0)

    await PaymentService(store)._accrue_credit_balance(
        tenant_id=TENANT, account_id=ACCOUNT, excess_cents=300,
        payment_id="pay_over_1", invoice_id=INVOICE,
    )

    assert (await _account_row()).credit_balance_cents == 300
    got = await AccountService(store).get(TENANT, ACCOUNT)
    assert got["credit_balance_cents"] == 300


async def test_draft_invoice_drain_reaches_the_accounts_row(engine, pg_reads_on):
    """OI-13: draining into a draft invoice lowers the Postgres balance."""
    await _seed(credit_balance_cents=300)
    store = _Store(credit_balance_cents=300)

    await InvoiceService(store)._drain_credit_balance(
        tenant_id=TENANT, invoice=_draft_invoice(total_cents=200)
    )

    assert (await _account_row()).credit_balance_cents == 100
    got = await AccountService(store).get(TENANT, ACCOUNT)
    assert got["credit_balance_cents"] == 100


async def test_drain_mirrors_the_synthetic_payment(engine, pg_reads_on):
    """OI-42: the synthetic account_credit payment exists in PG ``payments``."""
    await _seed(credit_balance_cents=300)
    store = _Store(credit_balance_cents=300)

    await InvoiceService(store)._drain_credit_balance(
        tenant_id=TENANT, invoice=_draft_invoice(total_cents=500)
    )

    [payment_doc] = store.payments()
    async with session_scope() as session:
        row = await PaymentRepository().get(session, TENANT, payment_doc["payment_id"])
    assert row is not None
    assert row.source == "account_credit"
    assert row.method == "credit_balance"
    assert row.amount_cents == 300
    assert row.invoice_id == INVOICE
    assert (await _account_row()).credit_balance_cents == 0
