"""``create_document`` is an atomic create-if-absent on ``(index_name, doc_id)``.

The store key carries no tenant, so a plain ``index_document`` on an id another
tenant already owns replaces that tenant's row and rewrites its ``tenant_id``
(finding S7). ``create_document`` is the primitive the creating endpoints use
instead: ``INSERT … ON CONFLICT DO NOTHING``, reporting whether a row went in.

Real PostgreSQL required — see ``conftest.py``.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select


async def _row(pg_sessionmaker, index_name: str, doc_id: str):
    from persistence.models import EsDocumentORM

    async with pg_sessionmaker() as session:
        return (
            await session.execute(
                select(EsDocumentORM).where(
                    EsDocumentORM.index_name == index_name,
                    EsDocumentORM.doc_id == doc_id,
                )
            )
        ).scalars().first()


async def test_first_create_inserts_and_stamps_timestamps(store, index_name, pg_sessionmaker):
    doc = {"tenant_id": "tenant-a", "name": "first"}

    created = await store.create_document(index_name, "X-1", doc)

    assert created is True
    stored = await store.get_document(index_name, "X-1")
    assert stored["name"] == "first"
    assert stored["created_at"] and stored["updated_at"]
    row = await _row(pg_sessionmaker, index_name, "X-1")
    assert row.tenant_id == "tenant-a"


async def test_second_create_with_same_id_returns_false_and_keeps_body(store, index_name):
    assert await store.create_document(index_name, "X-2", {"tenant_id": "tenant-a", "name": "first"})
    before = await store.get_document(index_name, "X-2")

    created = await store.create_document(
        index_name, "X-2", {"tenant_id": "tenant-a", "name": "second"}
    )

    assert created is False
    assert await store.get_document(index_name, "X-2") == before


async def test_other_tenant_cannot_take_over_an_existing_id(store, index_name, pg_sessionmaker):
    assert await store.create_document(
        index_name, "X-3", {"tenant_id": "tenant-a", "name": "owned by A"}
    )

    created = await store.create_document(
        index_name, "X-3", {"tenant_id": "tenant-b", "name": "B's attempt"}
    )

    assert created is False
    stored = await store.get_document(index_name, "X-3")
    assert stored["tenant_id"] == "tenant-a"
    assert stored["name"] == "owned by A"
    row = await _row(pg_sessionmaker, index_name, "X-3")
    assert row.tenant_id == "tenant-a"


async def test_concurrent_creates_of_one_id_insert_exactly_once(store, index_name):
    results = await asyncio.gather(
        *[
            store.create_document(index_name, "X-4", {"tenant_id": f"tenant-{n}", "n": n})
            for n in range(8)
        ]
    )

    assert sorted(results) == [False] * 7 + [True]
    winner = results.index(True)
    assert (await store.get_document(index_name, "X-4"))["n"] == winner


async def test_create_requires_a_doc_id(store, index_name):
    import pytest

    with pytest.raises(ValueError):
        await store.create_document(index_name, "", {"tenant_id": "tenant-a"})
