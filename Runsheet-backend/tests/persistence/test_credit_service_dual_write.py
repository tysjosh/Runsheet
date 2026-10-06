"""CreditService writes reach the Postgres store that reads use (N-CFV-3).

Under ``COMMERCE_READ_FROM_POSTGRES`` the account GET is served from the
``accounts`` table. CreditService used to write ``credit_state`` and
``credit_override_expires_at`` to the ES projection only, so a credit override
returned 200 and then never showed up on GET. These tests drive the real
service against in-memory SQLite with dual-write and read-cutover both on.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from commerce.services.account_service import AccountService
from commerce.services.credit_service import CreditService
from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.models import AccountEventORM
from persistence.repositories import AccountRepository, CustomerRepository
from services.time_utils import utcnow

TENANT = "demo-tenant"
ACCOUNT = "acct_credit_1"


def _make_es():
    """ES mock: writes succeed; the event sequence agg increments per call."""
    es = AsyncMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    seq = {"n": 0}

    async def _search(index, query, size=10, **kwargs):
        if index == "account_events":
            value = seq["n"] or None
            seq["n"] += 1
            return {"hits": {"hits": []}, "aggregations": {"max_seq": {"value": value}}}
        return {"hits": {"hits": [], "total": {"value": 0}}, "aggregations": {}}

    es.search_documents = AsyncMock(side_effect=_search)
    return es


@pytest.fixture
def pg_reads_on(monkeypatch):
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_read_from_postgres is True
    yield
    clear_settings_cache()


async def _seed_account(credit_state: str = "ok"):
    async with session_scope() as session:
        await CustomerRepository().create(
            session, customer_id="cust_1", tenant_id=TENANT, display_name="Acme",
        )
        await AccountRepository().create(
            session, account_id=ACCOUNT, tenant_id=TENANT, customer_id="cust_1",
            display_name="Acme Net 30", credit_limit_cents=1_000_00,
        )
        if credit_state != "ok":
            await AccountRepository().set_fields(
                session, TENANT, ACCOUNT, credit_state=credit_state
            )


async def _row():
    async with session_scope() as session:
        return await AccountRepository().get(session, TENANT, ACCOUNT)


async def test_apply_override_is_visible_on_account_get(engine, pg_reads_on):
    await _seed_account()
    es = _make_es()
    expires = (utcnow() + timedelta(days=2)).replace(microsecond=0)

    await CreditService(es).apply_override(
        tenant_id=TENANT, account_id=ACCOUNT, reason="QA", authorized_by="qa",
        expires_at=expires,
    )

    row = await _row()
    assert row.credit_state == "override"
    stored = row.credit_override_expires_at
    if stored.tzinfo is None:  # SQLite drops tzinfo on read
        stored = stored.replace(tzinfo=expires.tzinfo)
    assert stored == expires

    async with session_scope() as session:
        events = (
            await session.execute(
                select(AccountEventORM.event_type).where(
                    AccountEventORM.account_id == ACCOUNT
                )
            )
        ).scalars().all()
    assert "override_applied" in events

    # The GET the API serves.
    got = await AccountService(es).get(TENANT, ACCOUNT)
    assert got["credit_state"] == "override"
    assert got["credit_override_expires_at"] is not None


async def test_expire_override_clears_postgres_row(engine, pg_reads_on):
    await _seed_account()
    es = _make_es()
    service = CreditService(es)
    await service.apply_override(
        tenant_id=TENANT, account_id=ACCOUNT, reason="QA", authorized_by="qa",
        expires_at=utcnow() + timedelta(hours=1),
    )

    await service.expire_override(tenant_id=TENANT, account_id=ACCOUNT)

    row = await _row()
    assert row.credit_state == "ok"
    assert row.credit_override_expires_at is None


async def test_payment_applied_releases_hold_in_postgres(engine, pg_reads_on):
    await _seed_account(credit_state="hold")
    es = _make_es()

    await CreditService(es).on_payment_applied(tenant_id=TENANT, account_id=ACCOUNT)

    row = await _row()
    assert row.credit_state == "ok"
    assert row.available_credit_cents == 1_000_00
