"""
Unit tests for job-thread messaging endpoints (send_message, list_messages).

Tests message storage, access control, pagination, and WebSocket broadcast
for the driver messaging endpoints under /api/driver.

Validates: Requirements 6.1, 6.2, 6.3, 6.4
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt

# ---------------------------------------------------------------------------
# Patch ElasticsearchService singleton BEFORE any imports that trigger it
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver.api.message_endpoints import (
    router as message_router,
    configure_message_endpoints,
    _validate_sender_access,
)
from errors.exceptions import AppException

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(tenant_id: str = TENANT_ID, sub: str = "driver-1") -> str:
    return jwt.encode(
        {"tenant_id": tenant_id, "sub": sub}, JWT_SECRET, algorithm=JWT_ALGORITHM
    )


def _auth_headers(tenant_id: str = TENANT_ID) -> dict:
    return {"Authorization": f"Bearer {_make_token(tenant_id)}"}


def _job_doc(
    job_id="JOB_1",
    status="assigned",
    tenant_id="t1",
    asset_assigned="driver-1",
) -> dict:
    """Return a minimal job document."""
    return {
        "job_id": job_id,
        "status": status,
        "tenant_id": tenant_id,
        "asset_assigned": asset_assigned,
        "origin": "Port A",
        "destination": "Port B",
    }


def _make_es_service() -> MagicMock:
    """Create a mock ElasticsearchService."""
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(return_value={
        "hits": {
            "total": {"value": 0},
            "hits": [],
        }
    })
    return es


def _make_job_service(job_doc_return=None) -> MagicMock:
    """Create a mock JobService."""
    svc = MagicMock()
    svc._get_job_doc = AsyncMock(
        return_value=job_doc_return or _job_doc()
    )
    return svc


def _make_app(
    es_service=None,
    job_service=None,
    scheduling_ws=None,
    driver_ws=None,
) -> FastAPI:
    """Create a test FastAPI app with the message router."""
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(message_router)

    configure_message_endpoints(
        es_service=es_service or _make_es_service(),
        job_service=job_service or _make_job_service(),
        scheduling_ws_manager=scheduling_ws,
        driver_ws_manager=driver_ws,
    )
    return app


# ---------------------------------------------------------------------------
# Test: send_message endpoint
# ---------------------------------------------------------------------------


class TestSendMessage:
    """Tests for the POST /jobs/{job_id}/messages endpoint."""

    def test_driver_sends_message_succeeds(self):
        """Driver assigned to job can send a message. Validates: Req 6.1, 6.4"""
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Arrived at location",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["job_id"] == "JOB_1"
        assert data["body"] == "Arrived at location"
        assert data["sender_id"] == "driver-1"
        assert data["sender_role"] == "driver"
        assert "message_id" in data
        assert "timestamp" in data
        assert data["tenant_id"] == TENANT_ID

    def test_dispatcher_sends_message_succeeds(self):
        """Dispatcher can send a message to any tenant job. Validates: Req 6.1, 6.4"""
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Please confirm ETA",
                    "sender_id": "dispatcher-1",
                    "sender_role": "dispatcher",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["sender_role"] == "dispatcher"
        assert data["sender_id"] == "dispatcher-1"

    def test_unassigned_driver_rejected(self):
        """Driver not assigned to job gets 403. Validates: Req 6.4"""
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-2"))

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Hello",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 403

    def test_invalid_sender_role_rejected(self):
        """Unknown sender_role gets 403. Validates: Req 6.4"""
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Hello",
                    "sender_id": "someone",
                    "sender_role": "customer",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 403

    def test_message_stored_in_es(self):
        """Message is indexed in job_messages ES index. Validates: Req 6.1"""
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "On my way",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args.args[0] == "job_messages"
        doc = call_args.args[2]
        assert doc["job_id"] == "JOB_1"
        assert doc["body"] == "On my way"
        assert doc["sender_id"] == "driver-1"

    def test_missing_body_returns_422(self):
        """Missing required fields returns 422. Validates: Req 6.1"""
        es = _make_es_service()
        job_svc = _make_job_service()

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={"sender_id": "driver-1"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 422

    def test_nonexistent_job_returns_404(self):
        """Message to nonexistent job returns 404. Validates: Req 6.4"""
        es = _make_es_service()
        job_svc = MagicMock()
        job_svc._get_job_doc = AsyncMock(
            side_effect=AppException(
                error_code=MagicMock(value="RESOURCE_NOT_FOUND"),
                message="Job 'JOB_999' not found",
                status_code=404,
            )
        )

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_999/messages",
                json={
                    "body": "Hello",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: list_messages endpoint
# ---------------------------------------------------------------------------


class TestListMessages:
    """Tests for the GET /jobs/{job_id}/messages endpoint."""

    def test_list_empty_messages(self):
        """Empty job thread returns empty list. Validates: Req 6.2"""
        es = _make_es_service()
        job_svc = _make_job_service()

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/driver/jobs/JOB_1/messages",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        assert body["pagination"]["total"] == 0
        assert body["pagination"]["page"] == 1

    def test_list_messages_returns_sorted(self):
        """Messages are returned sorted by timestamp ascending. Validates: Req 6.2"""
        es = _make_es_service()
        es.search_documents.return_value = {
            "hits": {
                "total": {"value": 2},
                "hits": [
                    {
                        "_source": {
                            "message_id": "m1",
                            "job_id": "JOB_1",
                            "sender_id": "driver-1",
                            "sender_role": "driver",
                            "body": "First message",
                            "timestamp": "2026-01-01T10:00:00+00:00",
                            "tenant_id": "t1",
                        }
                    },
                    {
                        "_source": {
                            "message_id": "m2",
                            "job_id": "JOB_1",
                            "sender_id": "dispatcher-1",
                            "sender_role": "dispatcher",
                            "body": "Second message",
                            "timestamp": "2026-01-01T10:05:00+00:00",
                            "tenant_id": "t1",
                        }
                    },
                ],
            }
        }
        job_svc = _make_job_service()

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/driver/jobs/JOB_1/messages",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 2
        assert body["data"][0]["message_id"] == "m1"
        assert body["data"][1]["message_id"] == "m2"
        assert body["pagination"]["total"] == 2

    def test_list_messages_pagination(self):
        """Pagination parameters are passed to ES query. Validates: Req 6.2"""
        es = _make_es_service()
        job_svc = _make_job_service()

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/driver/jobs/JOB_1/messages?page=2&size=10",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        # Verify the ES query used correct offset
        es.search_documents.assert_called_once()
        query = es.search_documents.call_args.args[1]
        assert query["from"] == 10  # (page 2 - 1) * size 10
        assert query["size"] == 10

    def test_list_messages_sorts_ascending(self):
        """ES query sorts by timestamp ascending. Validates: Req 6.2"""
        es = _make_es_service()
        job_svc = _make_job_service()

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.get(
                "/api/driver/jobs/JOB_1/messages",
                headers=_auth_headers(),
            )

        query = es.search_documents.call_args.args[1]
        assert query["sort"] == [{"timestamp": {"order": "asc"}}]

    def test_list_messages_filters_by_tenant(self):
        """ES query filters by tenant_id. Validates: Req 6.2"""
        es = _make_es_service()
        job_svc = _make_job_service()

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.get(
                "/api/driver/jobs/JOB_1/messages",
                headers=_auth_headers(),
            )

        query = es.search_documents.call_args.args[1]
        filters = query["query"]["bool"]["filter"]
        tenant_filter = next(
            f for f in filters if "term" in f and "tenant_id" in f["term"]
        )
        assert tenant_filter["term"]["tenant_id"] == TENANT_ID

    def test_pagination_total_pages_calculation(self):
        """Total pages is computed correctly. Validates: Req 6.2"""
        es = _make_es_service()
        es.search_documents.return_value = {
            "hits": {
                "total": {"value": 25},
                "hits": [],
            }
        }
        job_svc = _make_job_service()

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/driver/jobs/JOB_1/messages?page=1&size=10",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        pagination = resp.json()["pagination"]
        assert pagination["total"] == 25
        assert pagination["total_pages"] == 3  # ceil(25/10)


# ---------------------------------------------------------------------------
# Test: WebSocket broadcast
# ---------------------------------------------------------------------------


class TestMessageBroadcast:
    """Tests for WebSocket broadcast on new messages."""

    def test_message_broadcasts_through_scheduling_ws(self):
        """New message broadcasts through scheduling WS. Validates: Req 6.3"""
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))

        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()

        app = _make_app(
            es_service=es, job_service=job_svc, scheduling_ws=ws_manager
        )
        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Test message",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        ws_manager.broadcast.assert_called_once()
        call_args = ws_manager.broadcast.call_args
        assert call_args.args[0] == "job_message"
        assert call_args.args[1]["job_id"] == "JOB_1"

    def test_message_broadcasts_to_driver_ws(self):
        """New message broadcasts to assigned driver via driver WS. Validates: Req 6.3"""
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))

        driver_ws = MagicMock()
        driver_ws.send_to_driver = AsyncMock()

        app = _make_app(
            es_service=es, job_service=job_svc, driver_ws=driver_ws
        )
        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Test message",
                    "sender_id": "dispatcher-1",
                    "sender_role": "dispatcher",
                },
                headers=_auth_headers(),
            )

        driver_ws.send_to_driver.assert_called_once()
        call_args = driver_ws.send_to_driver.call_args
        assert call_args.args[0] == "driver-1"
        assert call_args.args[1]["type"] == "job_message"

    def test_ws_broadcast_failure_does_not_break_endpoint(self):
        """WS broadcast failure does not break the endpoint. Validates: Req 6.3"""
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))

        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock(side_effect=Exception("WS down"))

        app = _make_app(
            es_service=es, job_service=job_svc, scheduling_ws=ws_manager
        )
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Test message",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: _validate_sender_access (unit tests for the helper)
# ---------------------------------------------------------------------------


class TestValidateSenderAccess:
    """Tests for the _validate_sender_access helper."""

    @pytest.mark.asyncio
    async def test_assigned_driver_has_access(self):
        """Assigned driver can access the job thread. Validates: Req 6.4"""
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))
        configure_message_endpoints(
            es_service=_make_es_service(), job_service=job_svc
        )

        result = await _validate_sender_access(
            "JOB_1", "driver-1", "driver", "t1"
        )
        assert result["job_id"] == "JOB_1"

    @pytest.mark.asyncio
    async def test_unassigned_driver_rejected(self):
        """Unassigned driver is rejected. Validates: Req 6.4"""
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-2"))
        configure_message_endpoints(
            es_service=_make_es_service(), job_service=job_svc
        )

        with pytest.raises(AppException) as exc_info:
            await _validate_sender_access(
                "JOB_1", "driver-1", "driver", "t1"
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_dispatcher_always_has_access(self):
        """Dispatcher has access to any tenant job. Validates: Req 6.4"""
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))
        configure_message_endpoints(
            es_service=_make_es_service(), job_service=job_svc
        )

        result = await _validate_sender_access(
            "JOB_1", "dispatcher-1", "dispatcher", "t1"
        )
        assert result["job_id"] == "JOB_1"

    @pytest.mark.asyncio
    async def test_unknown_role_rejected(self):
        """Unknown sender_role is rejected. Validates: Req 6.4"""
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))
        configure_message_endpoints(
            es_service=_make_es_service(), job_service=job_svc
        )

        with pytest.raises(AppException) as exc_info:
            await _validate_sender_access(
                "JOB_1", "someone", "customer", "t1"
            )
        assert exc_info.value.status_code == 403
