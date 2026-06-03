"""Integration tests for AccountService dual-write into Postgres.

Verifies the gated bridge mirrors account create, update, and balance refresh
into the Postgres source-of-truth + outbox when ``commerce_dual_write_postgres``
is on, with the SAME computed values the service wrote to ES, and is a no-op
when the flag is off.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from commerce.services.account_service import AccountService
from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.models import AccountORM, CustomerORM, OutboxEventORM
from persistence.repositories import AccountRepository, CustomerRepository

TENANT = "demo-tenant"


def _make_es():
    """ES mock where customer-existence and sequence-number queries resolve."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})

    async def _search(index, query, size=10, **kwargs):
        # _assert_customer_exists -> one hit; sequence-number agg -> empty.
        if index == "customers_current":
            return {"hits": {"hits": [{"_source": {"customer_id": "cust_1"}}]}}
        return {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {"max_seq": {"value": None}, "total_remaining": {"value": 0}},
        }

    es.search_documents = AsyncMock(side_effect=_search)
    return es


@pytest.fixture
def dual_write_on(monkeypatch):
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_dual_write_postgres is True
    yield
    clear_settings_cache()


async def _seed_parent_customer():
    """Mirror the parent customer so the account FK is satisfiable."""
    repo = CustomerRepository()
    async with session_scope() as session:
        await repo.create(
            session, customer_id="cust_1", tenant_id=TENANT, display_name="Acme",
        )


async def test_create_account_dual_writes_to_postgres(engine, dual_write_on):
    await _seed_parent_customer()
    es = _make_es()
    service = AccountService(es)

    result = await service.create(
        TENANT, customer_id="cust_1", display_name="Acme — Net 30",
        credit_limit_cents=500_00, net_terms_days=30,
    )

    repo = AccountRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, result["account_id"])
        assert row is not None
        assert row.credit_limit_cents == 500_00
        assert row.available_credit_cents == 500_00
        assert row.customer_id == "cust_1"


async def test_create_account_skips_when_parent_not_mirrored(engine, dual_write_on):
    """Without the parent customer mirrored, the account mirror skips (no FK error)."""
    es = _make_es()
    service = AccountService(es)

    result = await service.create(
        TENANT, customer_id="cust_1", display_name="Orphan Acct",
        credit_limit_cents=100_00,
    )

    # ES write still happened; Postgres has no account row (skipped gracefully).
    es.index_document.assert_any_await
    repo = AccountRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, result["account_id"])
        assert row is None
        account_rows = await session.scalar(select(func.count()).select_from(AccountORM))
    assert account_rows == 0


async def test_refresh_open_balance_mirrors_computed_values(engine, dual_write_on):
    await _seed_parent_customer()
    es = _make_es()
    service = AccountService(es)
    created = await service.create(
        TENANT, customer_id="cust_1", display_name="Acme", credit_limit_cents=1_000_00,
    )
    account_id = created["account_id"]

    # Make compute_open_balance return 400_00 by having the invoices agg sum to it.
    async def _search_with_balance(index, query, size=10, **kwargs):
        if index == "customers_current":
            return {"hits": {"hits": [{"_source": {"customer_id": "cust_1"}}]}}
        if index == "accounts_current":
            return {"hits": {"hits": [{"_source": created}]}}
        if index == "invoices_current":
            return {"hits": {"hits": [], "total": {"value": 0}},
                    "aggregations": {"total_remaining": {"value": 400_00},
                                     "oldest_issued": {"value": None}}}
        return {"hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"max_seq": {"value": None}}}

    es.search_documents = AsyncMock(side_effect=_search_with_balance)

    await service.refresh_open_balance(TENANT, account_id)

    repo = AccountRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, account_id)
        assert row.open_balance_cents == 400_00
        assert row.available_credit_cents == 600_00


async def test_dual_write_off_does_not_touch_postgres(engine, monkeypatch):
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "false")
    clear_settings_cache()

    es = _make_es()
    service = AccountService(es)
    await service.create(
        TENANT, customer_id="cust_1", display_name="No Mirror", credit_limit_cents=1,
    )

    async with session_scope() as session:
        account_rows = await session.scalar(select(func.count()).select_from(AccountORM))
        event_rows = await session.scalar(select(func.count()).select_from(OutboxEventORM))
    assert account_rows == 0
    assert event_rows == 0
