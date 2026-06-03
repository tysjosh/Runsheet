"""Read-cutover tests: commerce services serve reads from Postgres.

With ``commerce_read_from_postgres`` on, get/list/find resolve from the
Postgres source-of-truth (byte-identical projections) and the ES client is
NOT queried for reads. Cursor pagination is contiguous and stable. With the
flag off, reads fall through to ES (legacy).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from commerce.services.account_service import AccountService
from commerce.services.customer_service import CustomerService
from commerce.services.invoice_service import InvoiceService
from commerce.services.payment_service import PaymentService
from config.settings import clear_settings_cache, get_settings
from errors.exceptions import AppException
from persistence.database import session_scope
from persistence.repositories import (
    AccountRepository,
    CustomerRepository,
    InvoiceRepository,
    PaymentRepository,
)

TENANT = "demo-tenant"


def _es_that_raises_on_read():
    """ES mock whose read path fails loudly, proving reads come from Postgres."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.search_documents = AsyncMock(
        side_effect=AssertionError("ES read path must not be used after cutover")
    )
    return es


@pytest.fixture
def read_from_pg(monkeypatch):
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_read_from_postgres is True
    yield
    clear_settings_cache()


async def _seed():
    """Seed a customer, account, two invoices, and a payment directly in PG."""
    customers = CustomerRepository()
    accounts = AccountRepository()
    invoices = InvoiceRepository()
    payments = PaymentRepository()
    async with session_scope() as session:
        await customers.create(
            session, customer_id="cust_1", tenant_id=TENANT,
            display_name="Acme Fuel", tax_id="ACME-1",
        )
        await accounts.create(
            session, account_id="acct_1", tenant_id=TENANT, customer_id="cust_1",
            display_name="Acme — Net 30", credit_limit_cents=1_000_00,
        )
        await invoices.create(
            session, invoice_id="inv_1", tenant_id=TENANT, customer_id="cust_1",
            account_id="acct_1", order_id="ORD-1", invoice_number="INV-000001",
            line_items=[{"line_id": "line_1", "product_code": "DSL",
                         "quantity_gallons": 100.0, "unit_price_cents": 350,
                         "subtotal_cents": 35000}],
            total_cents=35000, remaining_cents=35000,
        )
        await invoices.create(
            session, invoice_id="inv_2", tenant_id=TENANT, customer_id="cust_1",
            account_id="acct_1", order_id="ORD-2", invoice_number="INV-000002",
            line_items=[{"line_id": "line_2", "product_code": "DSL",
                         "quantity_gallons": 50.0, "unit_price_cents": 350,
                         "subtotal_cents": 17500}],
            total_cents=17500, remaining_cents=17500,
        )
        await payments.create(
            session, payment_id="pay_1", tenant_id=TENANT, invoice_id="inv_1",
            account_id="acct_1", amount_cents=10000, source="stripe",
            method="card", external_id="ch_1",
        )


async def test_customer_get_served_from_postgres(engine, read_from_pg):
    await _seed()
    service = CustomerService(_es_that_raises_on_read())
    doc = await service.get(TENANT, "cust_1")
    assert doc["customer_id"] == "cust_1"
    assert doc["display_name"] == "Acme Fuel"
    assert doc["tax_id"] == "ACME-1"


async def test_customer_get_missing_raises_not_found(engine, read_from_pg):
    await _seed()
    service = CustomerService(_es_that_raises_on_read())
    with pytest.raises(AppException):
        await service.get(TENANT, "cust_missing")


async def test_account_get_served_from_postgres(engine, read_from_pg):
    await _seed()
    # oldest_open_invoice_days is computed from invoices; that helper still
    # reads ES, so give it a benign empty response without tripping the guard.
    es = AsyncMock()
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [], "total": {"value": 0}},
        "aggregations": {"oldest_issued": {"value": None}},
    })
    service = AccountService(es)
    doc = await service.get(TENANT, "acct_1")
    assert doc["account_id"] == "acct_1"
    assert doc["credit_limit_cents"] == 1_000_00
    assert doc["available_credit_cents"] == 1_000_00


async def test_invoice_get_served_from_postgres(engine, read_from_pg):
    await _seed()
    service = InvoiceService(_es_that_raises_on_read())
    doc = await service.get(tenant_id=TENANT, invoice_id="inv_1")
    assert doc["invoice_id"] == "inv_1"
    assert doc["invoice_number"] == "INV-000001"
    assert doc["total_cents"] == 35000
    assert doc["line_items"][0]["product_code"] == "DSL"


async def test_invoice_list_filters_and_shape(engine, read_from_pg):
    await _seed()
    service = InvoiceService(_es_that_raises_on_read())
    result = await service.list(tenant_id=TENANT, account_id="acct_1")
    assert {i["invoice_id"] for i in result["items"]} == {"inv_1", "inv_2"}
    assert result["limit"] == 50
    # order_id filter
    by_order = await service.list(tenant_id=TENANT, order_id="ORD-2")
    assert len(by_order["items"]) == 1
    assert by_order["items"][0]["invoice_id"] == "inv_2"


async def test_invoice_list_pagination_is_contiguous(engine, read_from_pg):
    await _seed()
    service = InvoiceService(_es_that_raises_on_read())
    page1 = await service.list(tenant_id=TENANT, limit=1)
    assert len(page1["items"]) == 1
    assert page1["next_cursor"] is not None
    page2 = await service.list(tenant_id=TENANT, limit=1, cursor=page1["next_cursor"])
    assert len(page2["items"]) == 1
    # No overlap across pages.
    assert page1["items"][0]["invoice_id"] != page2["items"][0]["invoice_id"]


async def test_payment_get_and_list_served_from_postgres(engine, read_from_pg):
    await _seed()
    service = PaymentService(_es_that_raises_on_read())
    doc = await service.get(tenant_id=TENANT, payment_id="pay_1")
    assert doc["payment_id"] == "pay_1"
    assert doc["amount_cents"] == 10000
    listing = await service.list(tenant_id=TENANT, invoice_id="inv_1")
    assert len(listing["items"]) == 1
    assert listing["items"][0]["payment_id"] == "pay_1"


async def test_find_invoice_by_order_served_from_postgres(engine, read_from_pg):
    await _seed()
    service = InvoiceService(_es_that_raises_on_read())
    found = await service._find_invoice_by_order(TENANT, "ORD-1")
    assert found is not None
    assert found["invoice_id"] == "inv_1"
    missing = await service._find_invoice_by_order(TENANT, "ORD-missing")
    assert missing is None


async def test_reads_fall_through_to_es_when_flag_off(engine, monkeypatch):
    """With the flag off, the service queries ES (legacy path)."""
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "false")
    clear_settings_cache()

    es = AsyncMock()
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [{"_source": {"customer_id": "cust_es", "display_name": "From ES"}}]},
    })
    service = CustomerService(es)
    doc = await service.get(TENANT, "cust_es")
    assert doc["display_name"] == "From ES"
    es.search_documents.assert_awaited()
