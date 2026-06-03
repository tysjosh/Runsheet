"""Dual-write tests for the rest-of-commerce aggregates.

Covers price books + rules, the invoice/account/dunning event ledgers, and AR
aging snapshots: each service write mirrors into Postgres + outbox when
``commerce_dual_write_postgres`` is on, and is a no-op when off.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.models import (
    AccountEventORM,
    ArAgingSnapshotORM,
    DunningEventORM,
    InvoiceEventORM,
    OutboxEventORM,
    PriceBookORM,
    PricingRuleORM,
)
from persistence.repositories import (
    AccountEventRepository,
    ArAgingSnapshotRepository,
    DunningEventRepository,
    InvoiceEventRepository,
    PriceBookRepository,
    PricingRuleRepository,
)

TENANT = "demo-tenant"


@pytest.fixture
def dual_write_on(monkeypatch):
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_dual_write_postgres is True
    yield
    clear_settings_cache()


# ---------------------------------------------------------------------------
# Repository-level (direct) coverage
# ---------------------------------------------------------------------------


async def test_price_book_and_rules_persist_with_outbox(engine):
    books = PriceBookRepository()
    rules = PricingRuleRepository()
    async with session_scope() as s:
        await books.create(s, price_book_id="pb_1", tenant_id=TENANT,
                           name="Standard", status="active", rule_count=1)
        await rules.upsert(s, rule={
            "rule_id": "rule_1", "price_book_id": "pb_1", "tenant_id": TENANT,
            "product_code": "DSL", "scope_type": "default", "scope_value": "default",
            "effective_from": "2026-01-01T00:00:00+00:00", "effective_to": None,
            "min_quantity_gallons": None, "unit_price_cents": 350,
        })

    async with session_scope() as s:
        book = await s.get(PriceBookORM, "pb_1")
        rule = await s.get(PricingRuleORM, "rule_1")
        outbox_count = await s.scalar(select(func.count()).select_from(OutboxEventORM))
    assert book.name == "Standard"
    assert rule.unit_price_cents == 350
    assert rule.effective_from is not None  # ISO string coerced to datetime
    assert outbox_count == 2  # book + rule


async def test_invoice_event_unique_sequence_enforced(engine):
    from sqlalchemy.exc import IntegrityError

    repo = InvoiceEventRepository()
    async with session_scope() as s:
        await repo.append(s, doc={
            "event_id": "ievt_1", "invoice_id": "inv_1", "tenant_id": TENANT,
            "event_type": "created", "payload": {}, "actor": "system",
            "occurred_at": "2026-01-01T00:00:00+00:00", "sequence_number": 1,
        })
    # Same (invoice_id, sequence_number) must be rejected by the unique constraint.
    with pytest.raises(IntegrityError):
        async with session_scope() as s:
            await repo.append(s, doc={
                "event_id": "ievt_2", "invoice_id": "inv_1", "tenant_id": TENANT,
                "event_type": "finalized", "payload": {}, "actor": "system",
                "occurred_at": "2026-01-02T00:00:00+00:00", "sequence_number": 1,
            })


async def test_account_event_appends(engine):
    repo = AccountEventRepository()
    async with session_scope() as s:
        await repo.append(s, doc={
            "event_id": "aevt_1", "account_id": "acct_1", "tenant_id": TENANT,
            "event_type": "created", "payload": {"credit_limit_cents": 1000},
            "actor": "system", "occurred_at": "2026-01-01T00:00:00+00:00",
            "sequence_number": 1,
        })
    async with session_scope() as s:
        row = await s.get(AccountEventORM, "aevt_1")
    assert row.event_type == "created"
    assert row.payload["credit_limit_cents"] == 1000


async def test_dunning_event_create_and_cancel(engine):
    repo = DunningEventRepository()
    async with session_scope() as s:
        await repo.create(s, doc={
            "event_id": "dun_1", "invoice_id": "inv_1", "account_id": "acct_1",
            "tenant_id": TENANT, "threshold_days": 30, "template_key": "dunning_level_30",
            "queued_at": "2026-01-01T00:00:00+00:00", "cancelled_at": None,
            "cancellation_reason": None,
        })
    async with session_scope() as s:
        await repo.set_fields(s, TENANT, "dun_1",
                              cancelled_at="2026-01-05T00:00:00+00:00",
                              cancellation_reason="invoice_paid")
    async with session_scope() as s:
        row = await s.get(DunningEventORM, "dun_1")
    assert row.cancellation_reason == "invoice_paid"
    assert row.cancelled_at is not None


async def test_ar_aging_snapshot_upsert_idempotent(engine):
    repo = ArAgingSnapshotRepository()
    doc = {
        "snapshot_id": f"{TENANT}:2026-01-01", "tenant_id": TENANT,
        "snapshot_date": "2026-01-01", "total_open_cents": 50000,
        "bucket_0_30_cents": 50000, "bucket_31_60_cents": 0,
        "bucket_61_90_cents": 0, "bucket_90_plus_cents": 0,
        "account_count_with_balance": 2,
    }
    async with session_scope() as s:
        await repo.upsert(s, doc=doc)
    # Re-run same day with updated totals -> upsert (no duplicate row).
    doc["total_open_cents"] = 60000
    doc["bucket_0_30_cents"] = 60000
    async with session_scope() as s:
        await repo.upsert(s, doc=doc)
    async with session_scope() as s:
        count = await s.scalar(select(func.count()).select_from(ArAgingSnapshotORM))
        row = await s.get(ArAgingSnapshotORM, f"{TENANT}:2026-01-01")
    assert count == 1
    assert row.total_open_cents == 60000


# ---------------------------------------------------------------------------
# Service-level wiring
# ---------------------------------------------------------------------------


async def test_price_book_service_create_mirrors(engine, dual_write_on):
    from commerce.services.price_book_service import PriceBookService

    es = AsyncMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    # PriceBookService needs a redis-like cache; pass None-tolerant mock.
    service = PriceBookService(es, redis_client=None)

    result = await service.create(
        TENANT, name="Book A", status="active",
        rules=[{"product_code": "DSL", "scope_type": "default",
                "scope_value": "default", "unit_price_cents": 350,
                "effective_from": "2026-01-01T00:00:00+00:00"}],
    )

    async with session_scope() as s:
        book = await s.get(PriceBookORM, result["price_book_id"])
        rule_count = await s.scalar(select(func.count()).select_from(PricingRuleORM))
    assert book is not None
    assert book.name == "Book A"
    assert rule_count == 1


async def test_dual_write_off_no_postgres(engine, monkeypatch):
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "false")
    clear_settings_cache()
    from commerce.services.commerce_persistence_bridge import mirror_ar_aging_snapshot

    await mirror_ar_aging_snapshot({
        "snapshot_id": f"{TENANT}:2026-02-02", "tenant_id": TENANT,
        "snapshot_date": "2026-02-02", "total_open_cents": 1,
    })
    async with session_scope() as s:
        count = await s.scalar(select(func.count()).select_from(ArAgingSnapshotORM))
    assert count == 0
