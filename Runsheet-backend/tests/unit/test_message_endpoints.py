"""
Unit tests for job-thread messaging endpoints (send_message, list_messages).

Tests message storage, access control, pagination, and WebSocket broadcast
for the driver messaging endpoints under /api/driver.

Validates: Requirements 6.1, 6.2, 6.3, 6.4
"""

import sys
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.support.auth_seam import auth_headers, install_test_auth

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
)
from errors.exceptions import AppException

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "t1"

# The Test_Auth_Path override is installed per-app (see ``_make_app``); no
# settings patch is needed now that endpoints authenticate via the
# dependency-override seam instead of a legacy JWT.
_SETTINGS_PATCH = nullcontext()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_headers(
    tenant_id: str = TENANT_ID,
    *,
    sub: str = "driver-1",
    roles: list = None,
    driver_id: str = "driver-1",
) -> dict:
    """A driver-scoped Test_Auth_Path header set.

    ``/api/driver`` resolution runs ``require_driver_identity``, so the context
    must hold the exact ``driver`` role and a canonical ``driver_id`` (Req 1.5,
    1.6). The sender identity a message is stamped with is derived from this
    context, never from the request body (Req 7.5–7.7).
    """
    return auth_headers(
        tenant_id,
        sub=sub,
        roles=roles if roles is not None else ["driver"],
        driver_id=driver_id,
    )


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


def _order_doc(
    order_id="ORD_1",
    tenant_id="t1",
    assigned_driver_id="driver-1",
) -> dict:
    """Return a minimal fuel-order document for the order-keyed path."""
    return {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "assigned_driver_id": assigned_driver_id,
        "status": "in_transit",
    }


def _make_order_repository(order_return=None) -> MagicMock:
    """Create a mock order repository exposing ``get(tenant_id, order_id)``."""
    repo = MagicMock()
    repo.get = AsyncMock(
        return_value=_order_doc() if order_return is None else order_return
    )
    return repo


def _make_app(
    es_service=None,
    job_service=None,
    order_repository=None,
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
        order_repository=order_repository,
        scheduling_ws_manager=scheduling_ws,
        driver_ws_manager=driver_ws,
    )
    install_test_auth(app)
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

    def test_body_sender_role_is_ignored(self):
        """A body ``sender_role`` never wins over the derived role.

        Previously a caller could name ``sender_role: "dispatcher"`` in the body
        and ``_validate_sender_access`` would skip the assignment check
        entirely. The role is now derived from ``TenantContext``.

        Validates: Req 7.7
        """
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Please confirm ETA",
                    "sender_id": "driver-1",
                    "sender_role": "dispatcher",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["sender_role"] == "driver"
        assert data["sender_id"] == "driver-1"

    def test_body_sender_id_naming_another_driver_is_rejected(self):
        """A body ``sender_id`` that is not the caller is 403.

        This is the hole that let a driver post as any other driver simply by
        naming them in the body.

        Validates: Req 7.5, 7.6
        """
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Not me",
                    "sender_id": "driver-99",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "SENDER_IDENTITY_MISMATCH"
        es.index_document.assert_not_called()

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

    def test_unknown_sender_identity_rejected(self):
        """A body naming an unrelated sender gets 403. Validates: Req 6.4, 7.6"""
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
                    "sender_id": "driver-1",
                    "sender_role": "driver",
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
# Test: thread authorization (the resolution, not a handler helper)
# ---------------------------------------------------------------------------


class TestThreadAuthorization:
    """Both handlers authorize by resolving the Work_Ref.

    ``_validate_sender_access`` is gone: the assignment check now happens in
    ``WorkRefResolver.resolve_job``, above both the write and the read, which is
    what closes the unchecked-read hole in ``list_messages``.
    """

    def test_thread_read_by_unassigned_driver_is_rejected(self):
        """A driver may not read a thread they are not assigned to.

        Validates: Req 7.8
        """
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-2"))

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/driver/jobs/JOB_1/messages",
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        es.search_documents.assert_not_called()

    def test_thread_read_by_assigned_driver_returns_thread(self):
        """The assigned driver reads their own thread. Validates: Req 7.9"""
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/driver/jobs/JOB_1/messages",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200

    def test_caller_without_driver_role_is_rejected(self):
        """A non-driver session cannot reach the driver surface.

        Validates: Req 1.5
        """
        es = _make_es_service()
        job_svc = _make_job_service(_job_doc(asset_assigned="driver-1"))

        app = _make_app(es_service=es, job_service=job_svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/driver/jobs/JOB_1/messages",
                headers=_auth_headers(roles=["admin"], driver_id=None),
            )

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test: order-keyed siblings
# ---------------------------------------------------------------------------


class TestOrderKeyedMessages:
    """POST / GET /api/driver/orders/{order_id}/messages.

    The siblings are the job-keyed handlers with ``resolve_order`` substituted
    for ``resolve_job``: no rule of their own, so validation and error codes
    cannot diverge from the job-keyed path (Req 7.14, 7.17, 7.19).
    """

    def test_assigned_driver_sends_order_message(self):
        """The order's assigned driver posts to the order thread.

        Validates: Req 7.14, 7.17
        """
        es = _make_es_service()
        repo = _make_order_repository(_order_doc(assigned_driver_id="driver-1"))

        app = _make_app(es_service=es, order_repository=repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_1/messages",
                json={
                    "body": "Loaded and rolling",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["order_id"] == "ORD_1"
        assert data["driver_id"] == "driver-1"
        assert "job_id" not in data
        assert data["sender_id"] == "driver-1"
        assert data["sender_role"] == "driver"
        assert data["tenant_id"] == TENANT_ID

        es.index_document.assert_called_once()
        assert es.index_document.call_args.args[0] == "job_messages"

    def test_order_message_body_sender_id_mismatch_is_rejected(self):
        """A body ``sender_id`` naming another driver is 403 on this path too.

        Same error code as the job-keyed sibling, because the same service
        produces it.

        Validates: Req 7.19
        """
        es = _make_es_service()
        repo = _make_order_repository(_order_doc(assigned_driver_id="driver-1"))

        app = _make_app(es_service=es, order_repository=repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_1/messages",
                json={
                    "body": "Not me",
                    "sender_id": "driver-99",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "SENDER_IDENTITY_MISMATCH"
        es.index_document.assert_not_called()

    def test_unassigned_driver_cannot_post_to_order_thread(self):
        """A driver who is not the order's assigned driver gets 403 FORBIDDEN.

        Validates: Req 7.21
        """
        es = _make_es_service()
        repo = _make_order_repository(_order_doc(assigned_driver_id="driver-2"))

        app = _make_app(es_service=es, order_repository=repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_1/messages",
                json={
                    "body": "Hello",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "FORBIDDEN"
        es.index_document.assert_not_called()

    def test_missing_order_returns_404(self):
        """An order that does not exist in the tenant is 404.

        Validates: Req 7.21
        """
        es = _make_es_service()
        repo = _make_order_repository(order_return=None)
        repo.get = AsyncMock(return_value=None)

        app = _make_app(es_service=es, order_repository=repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_404/messages",
                json={
                    "body": "Hello",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"

    def test_cross_tenant_order_returns_404(self):
        """An order belonging to another tenant is indistinguishable from absent.

        Validates: Req 7.21
        """
        es = _make_es_service()
        repo = _make_order_repository(_order_doc(tenant_id="other-tenant"))

        app = _make_app(es_service=es, order_repository=repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/driver/orders/ORD_1/messages",
                headers=_auth_headers(),
            )

        assert resp.status_code == 404
        es.search_documents.assert_not_called()

    def test_order_thread_read_filters_on_order_id(self):
        """The thread read filters on ``order_id`` plus ``tenant_id``.

        Validates: Req 7.14, 7.17
        """
        es = _make_es_service()
        es.search_documents.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_source": {
                            "message_id": "m1",
                            "order_id": "ORD_1",
                            "driver_id": "driver-1",
                            "sender_id": "driver-1",
                            "sender_role": "driver",
                            "body": "First",
                            "timestamp": "2026-01-01T10:00:00+00:00",
                            "tenant_id": "t1",
                        }
                    }
                ],
            }
        }
        repo = _make_order_repository(_order_doc(assigned_driver_id="driver-1"))

        app = _make_app(es_service=es, order_repository=repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/driver/orders/ORD_1/messages?page=2&size=10",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["data"][0]["order_id"] == "ORD_1"
        assert body["pagination"] == {
            "page": 2,
            "size": 10,
            "total": 1,
            "total_pages": 1,
        }

        query = es.search_documents.call_args.args[1]
        assert query["from"] == 10
        assert query["sort"] == [{"timestamp": {"order": "asc"}}]
        filters = query["query"]["bool"]["filter"]
        assert {"term": {"order_id": "ORD_1"}} in filters
        assert {"term": {"tenant_id": TENANT_ID}} in filters

    def test_unassigned_driver_cannot_read_order_thread(self):
        """Resolution is the authorization on the read path as well.

        Validates: Req 7.21
        """
        es = _make_es_service()
        repo = _make_order_repository(_order_doc(assigned_driver_id="driver-2"))

        app = _make_app(es_service=es, order_repository=repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/driver/orders/ORD_1/messages",
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        es.search_documents.assert_not_called()

    def test_order_message_broadcasts_to_assigned_driver(self):
        """Delivery reaches the assigned driver over the driver channel.

        Validates: Req 7.17
        """
        es = _make_es_service()
        repo = _make_order_repository(_order_doc(assigned_driver_id="driver-1"))
        driver_ws = MagicMock()
        driver_ws.send_to_driver = AsyncMock()

        app = _make_app(es_service=es, order_repository=repo, driver_ws=driver_ws)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_1/messages",
                json={
                    "body": "On site",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        driver_ws.send_to_driver.assert_called_once()
        assert driver_ws.send_to_driver.call_args.args[0] == "driver-1"
        assert driver_ws.send_to_driver.call_args.args[1]["type"] == "job_message"

    def test_caller_without_driver_role_is_rejected_on_order_path(self):
        """A non-driver session cannot reach the order-keyed surface.

        Validates: Req 1.5, 1.6
        """
        es = _make_es_service()
        repo = _make_order_repository()

        app = _make_app(es_service=es, order_repository=repo)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.get(
                "/api/driver/orders/ORD_1/messages",
                headers=_auth_headers(roles=["admin"], driver_id=None),
            )

        assert resp.status_code == 403
        repo.get.assert_not_called()
