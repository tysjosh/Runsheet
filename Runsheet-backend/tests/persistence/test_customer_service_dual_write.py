"""Integration tests for CustomerService dual-write into Postgres.

Verifies the gated bridge: when ``commerce_dual_write_postgres`` is on (and a
database is configured), creating/updating a customer through the existing
CustomerService also writes the Postgres source-of-truth row + outbox event.
When the flag is off, only ES is touched (legacy behavior).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from commerce.services.customer_service import CustomerService
from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.models import CustomerORM, OutboxEventORM
from persistence.repositories import CustomerRepository

TENANT = "demo-tenant"


def _make_es():
    es = AsyncMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    return es


@pytest.fixture
def dual_write_on(monkeypatch):
    """Enable the dual-write flag (DATABASE_URL set by the autouse fixture)."""
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    clear_settings_cache()
    assert get_settings().commerce_dual_write_postgres is True
    yield
    clear_settings_cache()


async def test_create_customer_dual_writes_to_postgres(engine, dual_write_on):
    es = _make_es()
    service = CustomerService(es)

    result = await service.create(TENANT, display_name="Acme Fuel", tax_id="ACME-1")

    # ES write still happened (read path unchanged).
    es.index_document.assert_awaited_once()

    # Postgres source-of-truth row + one outbox event exist.
    repo = CustomerRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, result["customer_id"])
        assert row is not None
        assert row.display_name == "Acme Fuel"
        assert row.tax_id == "ACME-1"
        events = (await session.execute(select(OutboxEventORM))).scalars().all()
    assert len(events) == 1
    assert events[0].aggregate_type == "customer"
    assert events[0].event_type == "created"


async def test_update_customer_dual_writes_to_postgres(engine, dual_write_on):
    es = _make_es()
    # get() reads back the existing doc; return what ES would hold post-create.
    service = CustomerService(es)
    created = await service.create(TENANT, display_name="Acme Fuel")
    customer_id = created["customer_id"]

    # Make service.get() resolve to the created doc for the update path.
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [{"_source": created}]}}
    )

    await service.update(TENANT, customer_id, display_name="Acme Fuel Renamed")

    repo = CustomerRepository()
    async with session_scope() as session:
        row = await repo.get(session, TENANT, customer_id)
        assert row.display_name == "Acme Fuel Renamed"
        event_count = await session.scalar(
            select(func.count()).select_from(OutboxEventORM)
        )
    # one created + one updated
    assert event_count == 2


async def test_dual_write_off_does_not_touch_postgres(engine, monkeypatch):
    """With the flag off, the service must not write any Postgres rows."""
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "false")
    clear_settings_cache()

    es = _make_es()
    service = CustomerService(es)
    await service.create(TENANT, display_name="No Mirror Co")

    async with session_scope() as session:
        row_count = await session.scalar(select(func.count()).select_from(CustomerORM))
        event_count = await session.scalar(
            select(func.count()).select_from(OutboxEventORM)
        )
    assert row_count == 0
    assert event_count == 0
