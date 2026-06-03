"""Fixtures for persistence-layer tests.

Each test runs against a fresh in-memory SQLite database via the async engine.
``settings.database_url`` is pointed at ``sqlite+aiosqlite:///:memory:`` so the
persistence layer activates exactly as it would against Postgres, but with no
external dependency (matches the mocked-services test policy).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from config.settings import clear_settings_cache, get_settings
from persistence.database import Base, dispose_engine, get_engine, get_sessionmaker


@pytest.fixture(autouse=True)
def _sqlite_database_url(monkeypatch):
    """Point the persistence layer at an in-memory SQLite DB for the test."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    clear_settings_cache()
    # Sanity: settings should now expose the test URL.
    assert get_settings().database_url == "sqlite+aiosqlite:///:memory:"
    yield
    clear_settings_cache()


@pytest_asyncio.fixture
async def engine():
    """Create the schema on a fresh engine and tear it down afterwards."""
    eng = get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await dispose_engine()


@pytest_asyncio.fixture
async def sessionmaker(engine):
    """Return the async session factory bound to the test engine."""
    return get_sessionmaker()


@pytest.fixture
def fake_es():
    """A fake ElasticsearchService exposing async index_document.

    Records every (index, id, doc) the relay projects so tests can assert the
    ES projection matches the Postgres source-of-truth.
    """

    class FakeES:
        def __init__(self):
            self.indexed = []  # list of (index, doc_id, doc)
            self.index_document = AsyncMock(side_effect=self._index)

        async def _index(self, index, doc_id, document):
            self.indexed.append((index, doc_id, dict(document)))
            return {"result": "created"}

    return FakeES()
