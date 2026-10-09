"""L1 defence in depth: the document store never moves a row between tenants.

The store key ``(index_name, doc_id)`` has no tenant. Before this guard, tenant
B writing an id tenant A owned replaced A's body and rewrote ``tenant_id``, so
any create path that used ``index_document`` on a client-supplied id was a
takeover. Each write method now refuses a change from one non-null tenant to a
different one. A tenantless row can still be adopted.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from config.settings import clear_settings_cache
from errors.codes import ErrorCode

INDEX = "customer_tanks"
A = "tenant-a"
B = "tenant-b"


@pytest_asyncio.fixture
async def store(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    clear_settings_cache()
    import persistence.models  # noqa: F401  (registers EsDocumentORM)
    from persistence.database import Base, dispose_engine, get_engine
    from persistence.document_store import PostgresDocumentStore

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield PostgresDocumentStore()
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await dispose_engine()
        clear_settings_cache()


async def _row(store, doc_id):
    from persistence.models import EsDocumentORM

    async with store._session_scope() as session:
        row = await session.get(EsDocumentORM, (INDEX, doc_id))
        return (row.tenant_id, dict(row.document)) if row else None


async def _seed_a(store, doc_id="X"):
    await store.index_document(INDEX, doc_id, {"tenant_id": A, "name": "a-body"})
    return await _row(store, doc_id)


def _assert_409(exc_info):
    from persistence.document_store import CrossTenantWriteError

    exc = exc_info.value
    assert isinstance(exc, CrossTenantWriteError)
    assert exc.error_code == ErrorCode.RESOURCE_ALREADY_EXISTS
    assert exc.status_code == 409
    assert A not in exc.message


@pytest.mark.asyncio
async def test_index_document_refuses_rehome(store):
    before = await _seed_a(store)
    with pytest.raises(Exception) as exc_info:
        await store.index_document(INDEX, "X", {"tenant_id": B, "name": "b-body"})
    _assert_409(exc_info)
    assert await _row(store, "X") == before


@pytest.mark.asyncio
async def test_update_document_refuses_tenant_change(store):
    before = await _seed_a(store)
    with pytest.raises(Exception) as exc_info:
        await store.update_document(INDEX, "X", {"tenant_id": B, "name": "b"})
    _assert_409(exc_info)
    assert await _row(store, "X") == before


@pytest.mark.asyncio
async def test_atomic_update_refuses_tenant_change(store):
    before = await _seed_a(store)
    with pytest.raises(Exception) as exc_info:
        await store.atomic_update(
            INDEX, "X", lambda d: {**d, "tenant_id": B, "name": "b"}
        )
    _assert_409(exc_info)
    assert await _row(store, "X") == before


@pytest.mark.asyncio
async def test_bulk_skips_foreign_row_and_reports_it(store):
    before = await _seed_a(store)
    result = await store.bulk_index_documents(
        INDEX,
        [
            {"id": "X", "tenant_id": B, "name": "b-body"},
            {"id": "Y", "tenant_id": B, "name": "b-new"},
        ],
    )
    assert result["failed"] == 1
    assert result["successful"] == 1
    assert result["errors"] == [{"position": 0, "reason": "id already exists"}]
    assert await _row(store, "X") == before
    assert (await _row(store, "Y"))[0] == B


@pytest.mark.asyncio
async def test_update_by_query_skips_foreign_rows(store):
    before = await _seed_a(store)
    changed = await store.update_by_query(
        INDEX, {"match_all": {}}, lambda d: {**d, "tenant_id": B}
    )
    assert changed == 0
    assert await _row(store, "X") == before


@pytest.mark.asyncio
async def test_same_tenant_reindex_still_updates(store):
    await _seed_a(store)
    await store.index_document(INDEX, "X", {"tenant_id": A, "name": "a-v2"})
    await store.update_document(INDEX, "X", {"name": "a-v3"})
    tenant, doc = await _row(store, "X")
    assert tenant == A and doc["name"] == "a-v3"


@pytest.mark.asyncio
async def test_null_tenant_row_is_adopted(store):
    await store.index_document(INDEX, "L", {"name": "legacy"})
    await store.index_document(INDEX, "L", {"tenant_id": A, "name": "adopted"})
    tenant, doc = await _row(store, "L")
    assert tenant == A and doc["name"] == "adopted"


@pytest.mark.asyncio
async def test_tenantless_doc_over_null_row_still_writes(store):
    await store.index_document(INDEX, "L", {"name": "legacy"})
    await store.index_document(INDEX, "L", {"name": "legacy-v2"})
    tenant, doc = await _row(store, "L")
    assert tenant is None and doc["name"] == "legacy-v2"
