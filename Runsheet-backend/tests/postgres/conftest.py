"""Fixtures for tests that need a real PostgreSQL database.

Everything else in ``tests/`` runs against in-memory SQLite, which is the right
default: no external dependency, and the ORM layer behaves the same. The document
store cannot be tested that way. Its whole point is the jsonb operators —
containment (``@>``), key existence (``?``), jsonb's total ordering for ``sort`` —
and SQLite has none of them. A SQLite-shimmed test would exercise a different
translation from the one that ships, which is worse than no test.

So these tests want a real server. Without one they **skip**, they do not pass:
a green run that silently covered nothing is how a translator ships broken.

The connection URL comes from ``POSTGRES_TEST_URL`` if set, otherwise
``DATABASE_URL``. Locally that is the development database, which is safe because
every test writes under its own unique index name (see :func:`index_name`) and the
document store is keyed ``(index_name, doc_id)`` — a test cannot see or disturb
application rows, and the session fixture removes its own prefix afterwards.
"""

from __future__ import annotations

import os
import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio

#: Every index this suite creates starts with this, so cleanup is exact and a
#: leaked row is obviously a test artifact rather than application data.
TEST_INDEX_PREFIX = "pytest_docstore_"


def _database_url() -> str | None:
    for key in ("POSTGRES_TEST_URL", "DATABASE_URL"):
        url = os.environ.get(key)
        if url and "postgresql" in url:
            return url
    return None


def _normalise(url: str) -> str:
    """Force the async driver, whichever form the environment supplied."""
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql+asyncpg://"):
        return url
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture(scope="session")
def postgres_url() -> str:
    url = _database_url()
    if not url:
        pytest.skip(
            "no PostgreSQL URL (set POSTGRES_TEST_URL or DATABASE_URL); the "
            "document store needs jsonb operators SQLite does not have"
        )
    return _normalise(url)


@pytest_asyncio.fixture
async def pg_engine(postgres_url):
    """A fresh engine per test.

    Function-scoped because ``pytest.ini`` sets
    ``asyncio_default_fixture_loop_scope = function``: a session-scoped async
    fixture would outlive the loop it was created on. Cheap enough — this suite is
    small and a connection to a local server costs milliseconds.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url, pool_pre_ping=True)
    try:
        from sqlalchemy import text

        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"PostgreSQL at {postgres_url.split('@')[-1]} unreachable: {exc}")

    # Create the document-store table if the database has not had migrations
    # applied. Only this table: creating the whole metadata would collide with an
    # already-migrated development database.
    from persistence.models import EsDocumentORM

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: EsDocumentORM.__table__.create(sync_conn, checkfirst=True)
        )
    try:
        yield engine
    finally:
        from sqlalchemy import text

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "delete from es_documents where index_name like :prefix"
                ),
                {"prefix": f"{TEST_INDEX_PREFIX}%"},
            )
        await engine.dispose()


@pytest_asyncio.fixture
async def pg_sessionmaker(pg_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
def index_name() -> str:
    """A unique index name for one test.

    Unique per test rather than shared-and-truncated so tests cannot interfere
    with each other even when run in parallel, and so a failure leaves its data
    in place for inspection.
    """
    return f"{TEST_INDEX_PREFIX}{uuid.uuid4().hex}"


@pytest_asyncio.fixture
async def store(pg_sessionmaker) -> AsyncIterator["object"]:
    """A :class:`PostgresDocumentStore` bound to the test session factory."""
    from contextlib import asynccontextmanager

    from persistence.document_store import PostgresDocumentStore

    @asynccontextmanager
    async def scope():
        async with pg_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    yield PostgresDocumentStore(session_factory=scope)
