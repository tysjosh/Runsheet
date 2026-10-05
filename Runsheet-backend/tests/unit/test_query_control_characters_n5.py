"""N5 regression: control characters in query values are a 400, not a 500.

``GET /api/agent/feedback?agent_id=%00`` reached ``PostgresDocumentStore`` as a
jsonb ``@>`` with ``\\x00``, which Postgres rejects; the endpoint's ``except
Exception: raise internal_error(...)`` turned that into a 500. The store now
rejects control characters before touching the database with
``InvalidQueryValueError`` (a ``ValueError``), and ``errors.handlers`` finds that
error under an ``internal_error`` wrapper and answers 400 VALIDATION_ERROR.

``InvalidQueryValueError`` is read off the module inside each test (not imported
at the top) so the file collects and fails per test on code that predates it.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

import agent_endpoints
import persistence.document_query as dq
from Agents.feedback_service import FeedbackService
from config.settings import clear_settings_cache
from errors.exceptions import internal_error
from errors.handlers import (
    handle_app_exception,
    handle_unexpected_exception,
    register_exception_handlers,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

TENANT = "tenant-A"
INDEX = "agent_feedback"


# ---------------------------------------------------------------------------
# Fixtures: the document store on in-memory SQLite (tests/persistence pattern)
# ---------------------------------------------------------------------------


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
    await doc_store.index_document(
        INDEX,
        "fb-1",
        {
            "feedback_id": "fb-1",
            "tenant_id": TENANT,
            "agent_id": "agent-1",
            "action_type": "reroute",
            "timestamp": "2026-10-04T00:00:00+00:00",
        },
    )
    try:
        yield doc_store
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await dispose_engine()
        clear_settings_cache()


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/agent/feedback",
            "headers": [],
            "query_string": b"",
        }
    )


# ---------------------------------------------------------------------------
# (1) The store rejects control characters before the database sees them
# ---------------------------------------------------------------------------


class TestStoreRejectsControlCharacters:
    async def test_search_documents_rejects_nul_term(self, store):
        with pytest.raises(dq.InvalidQueryValueError) as info:
            await store.search_documents(
                INDEX, {"query": {"term": {"agent_id": "\x00"}}}
            )
        assert info.value.reason == "control_character"
        assert "\x00" not in str(info.value)

    async def test_search_documents_rejects_control_character_in_key(self, store):
        with pytest.raises(dq.InvalidQueryValueError):
            await store.search_documents(
                INDEX, {"query": {"term": {"agent\x01id": "agent-1"}}}
            )

    async def test_update_and_delete_by_query_reject_nul(self, store):
        query = {"term": {"agent_id": "a\x00b"}}
        with pytest.raises(dq.InvalidQueryValueError):
            await store.update_by_query(INDEX, query, lambda doc: doc)
        with pytest.raises(dq.InvalidQueryValueError):
            await store.delete_by_query(INDEX, query)
        # Nothing was deleted.
        assert await store.get_document(INDEX, "fb-1") is not None


# ---------------------------------------------------------------------------
# (4) Tab, newline, carriage return and unicode still work
# ---------------------------------------------------------------------------


class TestOrdinaryValuesStillWork:
    """SQLite has no jsonb ``@>``, so the search round trip for these values
    lives in ``tests/postgres/test_document_store.py``; here the validator
    itself must let them through."""

    @pytest.mark.parametrize(
        "value", ["a\tb", "line\nnext", "cr\r", "Zürich – 東京 🚚", "agent-1"]
    )
    def test_allowed_values_pass_the_validator(self, value):
        dq.reject_control_characters(
            {"query": {"term": {"agent_id": value}}, "sort": [{value: "asc"}]}
        )

    def test_reject_control_characters_walks_nested_values(self):
        dq.reject_control_characters(
            {"bool": {"must": [{"terms": {"status": ["a\tb", "ü"]}}]}}
        )
        for bad in ("\x00", "\x08", "\x0b", "\x0c", "\x1f", "\x7f"):
            with pytest.raises(dq.InvalidQueryValueError):
                dq.reject_control_characters(
                    {"bool": {"must": [{"terms": {"status": ["ok", bad]}}]}}
                )


# ---------------------------------------------------------------------------
# Relational read path (commerce_persistence_bridge)
# ---------------------------------------------------------------------------


class TestRelationalReadHelpersReject:
    async def test_read_hybrid_helpers_reject_before_the_session(
        self, monkeypatch
    ):
        import commerce.services.commerce_persistence_bridge as bridge

        monkeypatch.setattr(bridge, "_hybrid_cut_over", lambda _agg: True)

        cases = [
            bridge.read_hybrid_get("depot", TENANT, "d\x00"),
            bridge.read_hybrid_get_any("depot", "d\x00"),
            bridge.read_hybrid_find_one(
                "depot", TENANT, term_filters={"code": "\x00"}
            ),
            bridge.read_hybrid_list("depot", TENANT, filters={"x": "\x00"}),
            bridge.read_hybrid_search(
                "fuel_order", TENANT, in_filters={"status": ["placed", "\x00"]}
            ),
            bridge.read_hybrid_search(
                "fuel_order", TENANT, text_query="abc\x00"
            ),
            bridge.read_hybrid_search_all_tenants(
                "fuel_order", term_filters={"tenant_id": "\x00"}
            ),
        ]
        for coro in cases:
            with pytest.raises(dq.InvalidQueryValueError):
                await coro


# ---------------------------------------------------------------------------
# (3) Handler unwraps internal_error(...) around the query-value error
# ---------------------------------------------------------------------------


class TestHandlerMapping:
    async def test_internal_error_wrapping_query_value_error_is_400(self):
        try:
            try:
                raise dq.InvalidQueryValueError()
            except Exception:
                raise internal_error(message="Failed to list feedback")
        except Exception as exc:
            wrapped = exc

        response = await handle_app_exception(_request(), wrapped)

        assert response.status_code == 400
        body = response.body.decode()
        assert '"error_code":"VALIDATION_ERROR"' in body
        assert '"reason":"control_character"' in body
        assert '"request_id"' in body

    async def test_plain_internal_error_is_still_500(self):
        try:
            try:
                raise RuntimeError("db down")
            except Exception:
                raise internal_error(message="Failed to list feedback")
        except Exception as exc:
            wrapped = exc

        response = await handle_app_exception(_request(), wrapped)

        assert response.status_code == 500

    async def test_unexpected_exception_caused_by_query_value_error_is_400(self):
        try:
            try:
                raise dq.InvalidQueryValueError()
            except Exception as inner:
                raise RuntimeError("wrapped") from inner
        except Exception as exc:
            wrapped = exc

        response = await handle_unexpected_exception(_request(), wrapped)

        assert response.status_code == 400
        assert "wrapped" not in response.body.decode()

    async def test_cyclic_chain_without_the_error_is_500(self):
        first, second = RuntimeError("a"), RuntimeError("b")
        first.__context__ = second
        second.__context__ = first

        response = await handle_unexpected_exception(_request(), first)

        assert response.status_code == 500


# ---------------------------------------------------------------------------
# (2) HTTP: the real feedback endpoint answers 400
# ---------------------------------------------------------------------------


@pytest.fixture
def client(store, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        agent_endpoints, "_feedback_service", FeedbackService(store)
    )
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


class TestFeedbackEndpoint:
    async def test_nul_agent_id_is_400_validation_error(self, client):
        response = client.get("/api/agent/feedback?agent_id=%00")

        assert response.status_code == 400, response.text
        body = response.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert body["details"] == {"reason": "control_character"}
        assert body.get("request_id")
        text = response.text.lower()
        assert "\\u0000" not in text and "\x00" not in text
        for leak in ("select", "sql", "jsonb", "traceback", "psycopg"):
            assert leak not in text
