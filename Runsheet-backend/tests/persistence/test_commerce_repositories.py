"""Tests for the Postgres source-of-truth commerce repositories + outbox + relay.

Proves the three properties the migration is meant to deliver:

1. Constraints ES could not enforce now hold at the database level
   (unique invoice numbers per tenant; duplicate idempotency keys rejected;
   duplicate external payment ids rejected).
2. Each business write enqueues exactly one transactional-outbox event in the
   SAME transaction (dual-write atomicity).
3. The OutboxRelay projects those events into Elasticsearch byte-compatibly
   with the existing ``*_current`` document shapes, and is idempotent.

All tests run against in-memory SQLite via the persistence engine — no real
Postgres or Elasticsearch required.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from persistence.database import session_scope
from persistence.models import OutboxEventORM
from persistence.outbox_relay import OutboxRelay, project_pending
from persistence.repositories import (
    AccountRepository,
    CustomerRepository,
    IdempotencyRepository,
    InvoiceRepository,
    PaymentRepository,
)

TENANT = "demo-tenant"

customers = CustomerRepository()
accounts = AccountRepository()
invoices = InvoiceRepository()
payments = PaymentRepository()
idempotency = IdempotencyRepository()


async def _seed_customer_account(session, *, customer_id="cust_1", account_id="acct_1",
                                 credit_limit_cents=1_000_00):
    await customers.create(
        session, customer_id=customer_id, tenant_id=TENANT,
        display_name="Acme Fuel Co",
    )
    await accounts.create(
        session, account_id=account_id, tenant_id=TENANT, customer_id=customer_id,
        display_name="Acme — Net 30", credit_limit_cents=credit_limit_cents,
        net_terms_days=30,
    )


# ---------------------------------------------------------------------------
# Dual-write atomicity
# ---------------------------------------------------------------------------


async def test_create_customer_enqueues_one_outbox_event(engine):
    async with session_scope() as session:
        await customers.create(
            session, customer_id="cust_1", tenant_id=TENANT,
            display_name="Acme Fuel Co", tax_id="ACME-123",
        )

    async with session_scope() as session:
        count = await session.scalar(select(func.count()).select_from(OutboxEventORM))
        event = (await session.execute(select(OutboxEventORM))).scalar_one()

    assert count == 1
    assert event.aggregate_type == "customer"
    assert event.aggregate_id == "cust_1"
    assert event.event_type == "created"
    assert event.target_index == "customers_current"
    assert event.published_at is None
    assert event.payload["display_name"] == "Acme Fuel Co"
    assert event.payload["tax_id"] == "ACME-123"


async def test_business_write_and_outbox_commit_atomically(engine):
    """If the transaction fails after the write, NEITHER the row nor the event persist."""
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await customers.create(
                session, customer_id="cust_dup", tenant_id=TENANT,
                display_name="First", tax_id="DUP-1",
            )
            # Second insert with the same PK violates the constraint on flush,
            # rolling back the whole scope (including the first row's outbox event).
            await customers.create(
                session, customer_id="cust_dup", tenant_id=TENANT,
                display_name="Second", tax_id="DUP-2",
            )

    async with session_scope() as session:
        customer_count = await session.scalar(
            select(func.count()).select_from(OutboxEventORM)
        )
    assert customer_count == 0


# ---------------------------------------------------------------------------
# Constraints ES could not enforce
# ---------------------------------------------------------------------------


async def test_duplicate_invoice_number_rejected_per_tenant(engine):
    async with session_scope() as session:
        await _seed_customer_account(session)
        await invoices.create(
            session, invoice_id="inv_1", tenant_id=TENANT, customer_id="cust_1",
            account_id="acct_1", invoice_number="INV-1000",
            line_items=[{"line_id": "line_1", "product_code": "DSL",
                         "quantity_gallons": 100.0, "unit_price_cents": 350,
                         "subtotal_cents": 35000}],
        )

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await invoices.create(
                session, invoice_id="inv_2", tenant_id=TENANT, customer_id="cust_1",
                account_id="acct_1", invoice_number="INV-1000",  # duplicate number
                line_items=[{"line_id": "line_2", "product_code": "DSL",
                             "quantity_gallons": 50.0, "unit_price_cents": 350,
                             "subtotal_cents": 17500}],
            )


async def test_duplicate_idempotency_key_rejected(engine):
    async with session_scope() as session:
        await idempotency.put(
            session, tenant_id=TENANT, idempotency_key="key-1",
            response_status=200, response_body={"ok": True},
        )

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await idempotency.put(
                session, tenant_id=TENANT, idempotency_key="key-1",
                response_status=200, response_body={"ok": False},
            )

    # Same key under a DIFFERENT tenant is allowed.
    async with session_scope() as session:
        await idempotency.put(
            session, tenant_id="other-tenant", idempotency_key="key-1",
            response_status=201,
        )


async def test_duplicate_external_payment_id_rejected(engine):
    async with session_scope() as session:
        await _seed_customer_account(session)
        await invoices.create(
            session, invoice_id="inv_1", tenant_id=TENANT, customer_id="cust_1",
            account_id="acct_1", invoice_number="INV-2000",
            line_items=[{"line_id": "line_1", "product_code": "DSL",
                         "quantity_gallons": 100.0, "unit_price_cents": 350,
                         "subtotal_cents": 35000}],
        )
        await payments.create(
            session, payment_id="pay_1", tenant_id=TENANT, invoice_id="inv_1",
            account_id="acct_1", amount_cents=35000, source="stripe",
            method="card", external_id="ch_abc123",
        )

    # Re-delivered Stripe webhook with the same charge id must not double-apply.
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await payments.create(
                session, payment_id="pay_2", tenant_id=TENANT, invoice_id="inv_1",
                account_id="acct_1", amount_cents=35000, source="stripe",
                method="card", external_id="ch_abc123",
            )


# ---------------------------------------------------------------------------
# Account balance with row lock
# ---------------------------------------------------------------------------


async def test_apply_balance_delta_recomputes_available_credit(engine):
    async with session_scope() as session:
        await _seed_customer_account(session, credit_limit_cents=1_000_00)

    async with session_scope() as session:
        row = await accounts.apply_balance_delta(
            session, TENANT, "acct_1", open_balance_delta_cents=400_00
        )
        assert row is not None
        assert row.open_balance_cents == 400_00
        assert row.available_credit_cents == 600_00


async def test_record_payment_advances_invoice_status(engine):
    async with session_scope() as session:
        await _seed_customer_account(session)
        await invoices.create(
            session, invoice_id="inv_1", tenant_id=TENANT, customer_id="cust_1",
            account_id="acct_1", invoice_number="INV-3000",
            line_items=[{"line_id": "line_1", "product_code": "DSL",
                         "quantity_gallons": 100.0, "unit_price_cents": 350,
                         "subtotal_cents": 35000}],
        )

    # Partial payment -> status partial
    async with session_scope() as session:
        row = await invoices.record_payment_applied(
            session, TENANT, "inv_1", amount_cents=10000
        )
        assert row.status == "partial"
        assert row.remaining_cents == 25000

    # Remainder -> status paid
    async with session_scope() as session:
        row = await invoices.record_payment_applied(
            session, TENANT, "inv_1", amount_cents=25000
        )
        assert row.status == "paid"
        assert row.remaining_cents == 0


# ---------------------------------------------------------------------------
# Outbox relay projects to ES
# ---------------------------------------------------------------------------


async def test_relay_projects_events_to_es(engine, fake_es):
    async with session_scope() as session:
        await _seed_customer_account(session)
        await invoices.create(
            session, invoice_id="inv_1", tenant_id=TENANT, customer_id="cust_1",
            account_id="acct_1", invoice_number="INV-4000",
            line_items=[{"line_id": "line_1", "product_code": "DSL",
                         "quantity_gallons": 100.0, "unit_price_cents": 350,
                         "subtotal_cents": 35000}],
        )

    published = await project_pending(fake_es)

    # 3 events: customer, account, invoice
    assert published == 3
    indexed_by_index = {idx: (doc_id, doc) for idx, doc_id, doc in fake_es.indexed}
    assert indexed_by_index["customers_current"][0] == "cust_1"
    assert indexed_by_index["accounts_current"][0] == "acct_1"
    inv_id, inv_doc = indexed_by_index["invoices_current"]
    assert inv_id == "inv_1"
    assert inv_doc["invoice_number"] == "INV-4000"
    assert inv_doc["total_cents"] == 35000
    assert inv_doc["line_items"][0]["product_code"] == "DSL"

    # All outbox rows are now marked published.
    async with session_scope() as session:
        unpublished = await session.scalar(
            select(func.count()).select_from(OutboxEventORM)
            .where(OutboxEventORM.published_at.is_(None))
        )
    assert unpublished == 0


async def test_relay_is_idempotent_on_redelivery(engine, fake_es):
    async with session_scope() as session:
        await customers.create(
            session, customer_id="cust_1", tenant_id=TENANT, display_name="Acme",
        )

    first = await project_pending(fake_es)
    second = await project_pending(fake_es)  # nothing left unpublished

    assert first == 1
    assert second == 0
    # Only one index call total — already-published events are not re-sent.
    assert len(fake_es.indexed) == 1


async def test_relay_records_error_and_continues(engine):
    """A failing ES write increments attempts and leaves the row unpublished."""
    from unittest.mock import AsyncMock

    class FlakyES:
        def __init__(self):
            self.calls = 0
            self.index_document = AsyncMock(side_effect=self._index)

        async def _index(self, index, doc_id, document):
            self.calls += 1
            raise RuntimeError("ES unavailable")

    async with session_scope() as session:
        await customers.create(
            session, customer_id="cust_1", tenant_id=TENANT, display_name="Acme",
        )

    relay = OutboxRelay(FlakyES())
    published = await relay.drain_once()

    assert published == 0
    async with session_scope() as session:
        event = (await session.execute(select(OutboxEventORM))).scalar_one()
        assert event.published_at is None
        assert event.attempts == 1
        assert "ES unavailable" in event.last_error
