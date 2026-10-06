"""Credit override reaches the real Postgres ``accounts`` table (N-CFV-3).

Staging serves account GETs from Postgres (``COMMERCE_READ_FROM_POSTGRES``).
``CreditService`` used to write the override only to the ES projection, so the
POST returned 200 and the GET still said ``ok``. This drives the real service
and the real read path against the local server, inside a per-test tenant
that is deleted afterwards.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from commerce.services.commerce_persistence_bridge import read_account_get
from commerce.services.credit_service import CreditService
from config.settings import clear_settings_cache
from persistence import database
from persistence.database import session_scope
from persistence.repositories import AccountRepository, CustomerRepository
from services.time_utils import utcnow


def _make_es():
    es = AsyncMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": []}, "aggregations": {"max_seq": {"value": None}}}
    )
    return es


@pytest.fixture
async def pg_commerce(postgres_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    clear_settings_cache()
    await database.dispose_engine()
    try:
        async with session_scope() as session:
            await session.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        await database.dispose_engine()
        pytest.skip(f"PostgreSQL unreachable: {exc}")
    yield
    await database.dispose_engine()
    clear_settings_cache()


async def _cleanup(tenant: str) -> None:
    async with session_scope() as session:
        for table in ("account_events", "outbox_events", "accounts", "customers"):
            await session.execute(
                text(f"delete from {table} where tenant_id = :t"), {"t": tenant}
            )


async def test_apply_override_visible_through_postgres_read(pg_commerce):
    tenant = f"pytest-credit-{uuid.uuid4().hex[:12]}"
    account_id = f"acct_{uuid.uuid4().hex[:12]}"
    customer_id = f"cust_{uuid.uuid4().hex[:12]}"
    try:
        async with session_scope() as session:
            await CustomerRepository().create(
                session, customer_id=customer_id, tenant_id=tenant, display_name="QA",
            )
            await AccountRepository().create(
                session, account_id=account_id, tenant_id=tenant,
                customer_id=customer_id, display_name="QA", credit_limit_cents=100_00,
            )

        expires = (utcnow() + timedelta(days=1)).replace(microsecond=0)
        await CreditService(_make_es()).apply_override(
            tenant_id=tenant, account_id=account_id, reason="QA",
            authorized_by="qa", expires_at=expires,
        )

        got = await read_account_get(tenant, account_id)
        assert got["credit_state"] == "override"
        assert got["credit_override_expires_at"] is not None
        assert str(got["credit_override_expires_at"]).startswith(
            expires.isoformat()[:19]
        )

        async with session_scope() as session:
            n = await session.scalar(
                text(
                    "select count(*) from account_events where tenant_id = :t "
                    "and event_type = 'override_applied'"
                ),
                {"t": tenant},
            )
        assert n == 1
    finally:
        await _cleanup(tenant)
