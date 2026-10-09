"""Review LOW 2: control characters in document ids are a 400, not a 500.

N5 guarded query bodies, but the by-id store calls passed ``doc_id`` straight
to the database. Postgres rejects NUL in a text column, so
``POST /api/agent/approvals/%00/approve`` reached ``get_document`` and failed
as a 500. The store now rejects a control character in an id before touching
the database with ``InvalidDocumentIdError`` (an ``InvalidQueryValueError``).

SQLite accepts NUL, so on the old code the store tests fail on "DID NOT RAISE".
``persistence.document_query`` is read off the module inside each test so the
file collects on code that predates the new error.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_endpoints
import persistence.document_query as dq
from Agents.approval_queue_service import ApprovalQueueService
from config.settings import clear_settings_cache
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

TENANT = "tenant-A"
INDEX = "agent_approval_queue"
BAD_ID = "ab\x00c"
ID_MESSAGE = "An id contains a control character."


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
    doc_store = PostgresDocumentStore()
    try:
        yield doc_store
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await dispose_engine()
        clear_settings_cache()


def _calls(store, doc_id):
    doc = {"tenant_id": TENANT, "name": "x"}
    return {
        "index_document": lambda: store.index_document(INDEX, doc_id, dict(doc)),
        "update_document": lambda: store.update_document(INDEX, doc_id, {"name": "y"}),
        "delete_document": lambda: store.delete_document(INDEX, doc_id),
        "get_document": lambda: store.get_document(INDEX, doc_id),
        "atomic_update": lambda: store.atomic_update(
            INDEX, doc_id, lambda d: d, upsert=dict(doc)
        ),
        "document_exists": lambda: store.document_exists(INDEX, doc_id),
    }


async def _row_count(store) -> int:
    response = await store.search_documents(INDEX, {"query": {"match_all": {}}})
    return len(response["hits"]["hits"])


@pytest.mark.parametrize(
    "method",
    [
        "index_document",
        "update_document",
        "delete_document",
        "get_document",
        "atomic_update",
        "document_exists",
    ],
)
async def test_store_rejects_control_character_in_id(store, method):
    with pytest.raises(dq.InvalidQueryValueError) as info:
        await _calls(store, BAD_ID)[method]()
    assert str(info.value) == ID_MESSAGE
    assert "\x00" not in str(info.value)
    assert await _row_count(store) == 0


async def test_tab_in_id_is_accepted(store):
    calls = _calls(store, "ab\tc")
    await calls["index_document"]()
    assert await calls["document_exists"]()
    assert (await calls["get_document"]())["name"] == "x"
    await calls["update_document"]()
    await calls["atomic_update"]()
    assert await calls["delete_document"]()


# ---------------------------------------------------------------------------
# HTTP: the approve endpoint answers 400 with the safe message
# ---------------------------------------------------------------------------


@pytest.fixture
def client(store, monkeypatch) -> TestClient:
    activity_log = MagicMock()
    activity_log.log = AsyncMock(return_value="log-1")
    service = ApprovalQueueService(
        es_service=store,
        ws_manager=MagicMock(),
        activity_log_service=activity_log,
    )
    monkeypatch.setattr(agent_endpoints, "_approval_queue_service", service)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(agent_endpoints.router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=TENANT,
        user_id="user-1",
        has_pii_access=False,
        roles=["admin", "platform_admin"],
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )
    return TestClient(app, raise_server_exceptions=False)


def test_nul_action_id_is_400_with_the_safe_message(client):
    response = client.post("/api/agent/approvals/%00/approve")
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    assert body["message"] == ID_MESSAGE
    assert "\\u0000" not in response.text and "\x00" not in response.text
