"""
Unit tests for the idempotency middleware (driver endpoints).

Tests the IdempotencyMiddleware class, the FastAPI dependency
``check_idempotency``, and the integration with driver endpoints
(ack, accept, reject, message, exception, POD).

Validates: Requirements 14.1, 14.2, 14.3, 14.4
"""

import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt

# ---------------------------------------------------------------------------
# Patch ElasticsearchService singleton BEFORE any scheduling imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver.middleware.idempotency import (
    IdempotencyMiddleware,
    IdempotencyResult,
    configure_idempotency_middleware,
    get_idempotency_middleware,
    store_idempotency_response,
)
from driver.services.driver_es_mappings import IDEMPOTENCY_KEYS_INDEX

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JWT_SECRET = "test-jwt-secret"
JWT_ALGORITHM = "HS256"
TENANT_ID = "t1"

_SETTINGS_PATCH = patch(
    "ops.middleware.tenant_guard.get_settings",
    return_value=MagicMock(jwt_secret=JWT_SECRET, jwt_algorithm=JWT_ALGORITHM),
)


def _make_token(tenant_id: str = TENANT_ID, sub: str = "driver-1") -> str:
    return jwt.encode(
        {"tenant_id": tenant_id, "sub": sub}, JWT_SECRET, algorithm=JWT_ALGORITHM
    )


def _auth_headers(tenant_id: str = TENANT_ID) -> dict:
    return {"Authorization": f"Bearer {_make_token(tenant_id)}"}


# ---------------------------------------------------------------------------
# Test: IdempotencyMiddleware class
# ---------------------------------------------------------------------------


class TestIdempotencyMiddleware:
    """Tests for the IdempotencyMiddleware class."""

    def _make_es(self) -> MagicMock:
        es = MagicMock()
        es.get_document = AsyncMock()
        es.index_document = AsyncMock()
        return es

    @pytest.mark.asyncio
    async def test_check_and_cache_returns_none_on_miss(self):
        """Cache miss returns None. Validates: Req 14.1"""
        es = self._make_es()
        es.get_document = AsyncMock(side_effect=Exception("not found"))
        mw = IdempotencyMiddleware(es_service=es)

        result = await mw.check_and_cache("key-1", TENANT_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_check_and_cache_returns_cached_response(self):
        """Cache hit returns stored response. Validates: Req 14.1"""
        es = self._make_es()
        cached_doc = {
            "idempotency_key": "key-1",
            "tenant_id": TENANT_ID,
            "response": {"body": {"data": "cached"}, "status_code": 200},
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=12)
            ).isoformat(),
        }
        es.get_document = AsyncMock(return_value=cached_doc)
        mw = IdempotencyMiddleware(es_service=es)

        result = await mw.check_and_cache("key-1", TENANT_ID)
        assert result is not None
        assert result["body"]["data"] == "cached"

    @pytest.mark.asyncio
    async def test_check_and_cache_returns_none_on_expired(self):
        """Expired key returns None. Validates: Req 14.2"""
        es = self._make_es()
        cached_doc = {
            "idempotency_key": "key-1",
            "tenant_id": TENANT_ID,
            "response": {"body": {"data": "old"}, "status_code": 200},
            "expires_at": (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat(),
        }
        es.get_document = AsyncMock(return_value=cached_doc)
        mw = IdempotencyMiddleware(es_service=es)

        result = await mw.check_and_cache("key-1", TENANT_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_store_response_indexes_document(self):
        """store_response writes to ES with correct TTL. Validates: Req 14.2"""
        es = self._make_es()
        mw = IdempotencyMiddleware(es_service=es, ttl_hours=24)

        await mw.store_response("key-1", TENANT_ID, {"data": "ok"}, 200)

        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args.args[0] == IDEMPOTENCY_KEYS_INDEX
        doc = call_args.args[2]
        assert doc["idempotency_key"] == "key-1"
        assert doc["tenant_id"] == TENANT_ID
        assert doc["response"]["body"]["data"] == "ok"
        assert doc["response"]["status_code"] == 200
        assert "expires_at" in doc

    @pytest.mark.asyncio
    async def test_store_response_handles_es_failure_gracefully(self):
        """ES failure during store does not raise. Validates: Req 14.2"""
        es = self._make_es()
        es.index_document = AsyncMock(side_effect=Exception("ES down"))
        mw = IdempotencyMiddleware(es_service=es)

        # Should not raise
        await mw.store_response("key-1", TENANT_ID, {"data": "ok"})

    def test_doc_id_includes_tenant(self):
        """Document ID is scoped by tenant. Validates: Req 14.1"""
        es = self._make_es()
        mw = IdempotencyMiddleware(es_service=es)
        doc_id = mw._make_doc_id("key-1", "tenant-a")
        assert "tenant-a" in doc_id
        assert "key-1" in doc_id


# ---------------------------------------------------------------------------
# Test: IdempotencyResult
# ---------------------------------------------------------------------------


class TestIdempotencyResult:
    """Tests for the IdempotencyResult data class."""

    def test_no_key_is_not_replay(self):
        """No key means no replay. Validates: Req 14.3"""
        r = IdempotencyResult()
        assert r.key is None
        assert r.is_replay is False

    def test_key_without_cache_is_not_replay(self):
        """Key present but no cache is not a replay."""
        r = IdempotencyResult(key="abc")
        assert r.key == "abc"
        assert r.is_replay is False

    def test_key_with_cache_is_replay(self):
        """Key with cached response is a replay. Validates: Req 14.4"""
        cached = {"body": {"data": "cached"}, "status_code": 200}
        r = IdempotencyResult(key="abc", cached_response=cached)
        assert r.is_replay is True

    def test_replay_response_returns_json_response(self):
        """replay_response returns JSONResponse with correct header. Validates: Req 14.4"""
        cached = {"body": {"data": "cached"}, "status_code": 200}
        r = IdempotencyResult(key="abc", cached_response=cached)
        resp = r.replay_response()
        assert resp.status_code == 200
        assert resp.headers.get("x-idempotent-replayed") == "true"

    def test_replay_response_preserves_status_code(self):
        """replay_response preserves the original status code. Validates: Req 14.4"""
        cached = {"body": {"error": "bad"}, "status_code": 400}
        r = IdempotencyResult(key="abc", cached_response=cached)
        resp = r.replay_response()
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Test: configure / get singleton
# ---------------------------------------------------------------------------


class TestMiddlewareSingleton:
    """Tests for the module-level singleton management."""

    def test_configure_creates_instance(self):
        """configure_idempotency_middleware creates and returns instance."""
        es = MagicMock()
        mw = configure_idempotency_middleware(es_service=es, ttl_hours=12)
        assert isinstance(mw, IdempotencyMiddleware)
        assert mw._ttl_hours == 12

    def test_get_returns_configured_instance(self):
        """get_idempotency_middleware returns the configured instance."""
        es = MagicMock()
        configure_idempotency_middleware(es_service=es)
        mw = get_idempotency_middleware()
        assert mw is not None
        assert isinstance(mw, IdempotencyMiddleware)


# ---------------------------------------------------------------------------
# Test: Integration with driver ack endpoint
# ---------------------------------------------------------------------------


def _job_doc(
    job_id="JOB_1",
    status="assigned",
    tenant_id="t1",
    asset_assigned="driver-1",
) -> dict:
    return {
        "job_id": job_id,
        "status": status,
        "tenant_id": tenant_id,
        "asset_assigned": asset_assigned,
        "origin": "Port A",
        "destination": "Port B",
        "scheduled_time": "2026-03-12T10:00:00Z",
        "updated_at": "2026-03-12T00:00:00Z",
    }


def _make_job_service() -> MagicMock:
    es = MagicMock()
    es.update_document = AsyncMock(return_value={"result": "updated"})
    svc = MagicMock()
    svc._es = es
    svc._get_job_doc = AsyncMock()
    svc._append_event = AsyncMock(return_value="evt-123")
    return svc


class TestIdempotencyEndpointIntegration:
    """Integration tests: idempotency with the ack endpoint."""

    def _make_app(self, job_service, idempotency_mw=None):
        from errors.handlers import register_exception_handlers
        from scheduling.api.driver_endpoints import (
            router as driver_router,
            configure_driver_endpoints,
        )

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(driver_router)
        configure_driver_endpoints(job_service=job_service)

        # Wire idempotency middleware
        if idempotency_mw is not None:
            import driver.middleware.idempotency as idem_mod
            idem_mod._idempotency_middleware = idempotency_mw

        return app

    def test_request_without_idempotency_header_processes_normally(self):
        """No X-Idempotency-Key → normal processing. Validates: Req 14.3"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        app = self._make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={"device_id": "mobile-1"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        assert "x-idempotent-replayed" not in resp.headers
        data = resp.json()["data"]
        assert data["action"] == "ack"

    def test_first_request_with_key_processes_and_stores(self):
        """First request with key processes normally and stores response. Validates: Req 14.2"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        es_mock = MagicMock()
        es_mock.get_document = AsyncMock(side_effect=Exception("not found"))
        es_mock.index_document = AsyncMock()
        mw = IdempotencyMiddleware(es_service=es_mock)

        app = self._make_app(svc, idempotency_mw=mw)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={"device_id": "mobile-1"},
                headers={
                    **_auth_headers(),
                    "X-Idempotency-Key": "idem-key-1",
                },
            )

        assert resp.status_code == 200
        assert "x-idempotent-replayed" not in resp.headers
        # Verify the response was stored
        es_mock.index_document.assert_called()

    def test_duplicate_request_returns_cached_with_replay_header(self):
        """Duplicate request returns cached response with replay header. Validates: Req 14.1, 14.4"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        cached_body = {
            "data": {"job_id": "JOB_1", "action": "ack"},
            "request_id": "req-1",
        }
        cached_doc = {
            "idempotency_key": "idem-key-1",
            "tenant_id": TENANT_ID,
            "response": {"body": cached_body, "status_code": 200},
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=12)
            ).isoformat(),
        }

        es_mock = MagicMock()
        es_mock.get_document = AsyncMock(return_value=cached_doc)
        mw = IdempotencyMiddleware(es_service=es_mock)

        app = self._make_app(svc, idempotency_mw=mw)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={"device_id": "mobile-1"},
                headers={
                    **_auth_headers(),
                    "X-Idempotency-Key": "idem-key-1",
                },
            )

        assert resp.status_code == 200
        assert resp.headers.get("x-idempotent-replayed") == "true"
        body = resp.json()
        assert body["data"]["action"] == "ack"

        # The job service should NOT have been called (cached response)
        svc._append_event.assert_not_called()

    def test_expired_key_processes_normally(self):
        """Expired idempotency key processes as new request. Validates: Req 14.2"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        expired_doc = {
            "idempotency_key": "idem-key-1",
            "tenant_id": TENANT_ID,
            "response": {"body": {"old": True}, "status_code": 200},
            "expires_at": (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat(),
        }

        es_mock = MagicMock()
        es_mock.get_document = AsyncMock(return_value=expired_doc)
        es_mock.index_document = AsyncMock()
        mw = IdempotencyMiddleware(es_service=es_mock)

        app = self._make_app(svc, idempotency_mw=mw)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={"device_id": "mobile-1"},
                headers={
                    **_auth_headers(),
                    "X-Idempotency-Key": "idem-key-1",
                },
            )

        assert resp.status_code == 200
        assert "x-idempotent-replayed" not in resp.headers
        # Should have processed normally and stored new response
        svc._append_event.assert_called_once()
