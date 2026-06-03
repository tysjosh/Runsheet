"""Tests for the outbox-relay bootstrap module.

Verifies the relay background task starts only when the persistence layer is
active and not disabled, drains pending outbox events into the (fake) ES, and
is cleanly cancelled on shutdown — and is a strict no-op when dormant.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from bootstrap import persistence as relay_boot
from bootstrap.container import ServiceContainer
from config.settings import clear_settings_cache, get_settings
from persistence.database import session_scope
from persistence.models import OutboxEventORM
from persistence.repositories import CustomerRepository

TENANT = "demo-tenant"


class _FakeES:
    def __init__(self):
        self.indexed = []
        self.index_document = AsyncMock(side_effect=self._index)

    async def _index(self, index, doc_id, document):
        self.indexed.append((index, doc_id))
        return {"result": "created"}


def _container(es, settings):
    c = ServiceContainer()
    c.settings = settings
    c.es_service = es
    return c


async def test_relay_drains_outbox_when_active(engine, monkeypatch):
    # engine fixture sets DATABASE_URL to in-memory sqlite (persistence active).
    monkeypatch.setenv("OUTBOX_RELAY_POLL_INTERVAL_SECONDS", "0.1")
    clear_settings_cache()
    settings = get_settings()

    # Enqueue an outbox event via a normal write.
    async with session_scope() as s:
        await CustomerRepository().create(
            s, customer_id="cust_relay", tenant_id=TENANT, display_name="Relay Co",
        )

    es = _FakeES()
    container = _container(es, settings)
    await relay_boot.initialize(None, container)
    try:
        # Give the relay a few cycles to drain.
        for _ in range(20):
            await asyncio.sleep(0.1)
            if es.indexed:
                break
    finally:
        await relay_boot.shutdown(None, container)

    assert ("customers_current", "cust_relay") in es.indexed
    async with session_scope() as s:
        unpublished = await s.scalar(
            select(func.count()).select_from(OutboxEventORM)
            .where(OutboxEventORM.published_at.is_(None))
        )
    assert unpublished == 0
    clear_settings_cache()


async def test_relay_no_op_when_disabled(engine, monkeypatch):
    monkeypatch.setenv("OUTBOX_RELAY_ENABLED", "false")
    clear_settings_cache()
    settings = get_settings()
    assert settings.outbox_relay_enabled is False

    es = _FakeES()
    container = _container(es, settings)
    await relay_boot.initialize(None, container)
    await relay_boot.shutdown(None, container)

    # Relay never registered on the container.
    assert not container.has("outbox_relay")
    clear_settings_cache()


async def test_relay_no_op_when_dormant(monkeypatch):
    # No DATABASE_URL -> persistence dormant. (No engine fixture here.)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    clear_settings_cache()
    settings = get_settings()

    es = _FakeES()
    container = _container(es, settings)
    await relay_boot.initialize(None, container)
    await relay_boot.shutdown(None, container)

    assert not container.has("outbox_relay")
    assert es.index_document.await_count == 0
    clear_settings_cache()
