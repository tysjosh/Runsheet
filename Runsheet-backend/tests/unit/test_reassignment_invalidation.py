"""
Unit tests for reassignment invalidation (task 9.3).

Tests that:
- JobService.reassign_asset publishes assignment_revoked to previous driver
- JobService.reassign_asset publishes assignment to new driver
- JobService.reassign_asset appends assignment_revoked event to job timeline
- Driver endpoints reject requests from non-assigned driver with 403

Validates: Requirements 11.1, 11.2, 11.3, 11.4
"""

import sys
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

from scheduling.api.driver_endpoints import (
    router as driver_router,
    configure_driver_endpoints,
    _check_driver_assignment,
)
from driver.api.message_endpoints import (
    router as message_router,
    configure_message_endpoints,
)
from driver.api.exception_endpoints import (
    router as exception_router,
    configure_exception_endpoints,
)
from driver.api.pod_endpoints import (
    router as pod_router,
    configure_pod_endpoints,
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


def _auth_headers(tenant_id: str = TENANT_ID, sub: str = "driver-1") -> dict:
    return {"Authorization": f"Bearer {_make_token(tenant_id, sub)}"}


def _job_doc(
    job_id="JOB_1",
    status="assigned",
    tenant_id="t1",
    asset_assigned="driver-1",
    job_type="cargo_transport",
    scheduled_time="2026-03-12T10:00:00Z",
) -> dict:
    """Return a minimal job document with configurable fields."""
    return {
        "job_id": job_id,
        "status": status,
        "tenant_id": tenant_id,
        "asset_assigned": asset_assigned,
        "job_type": job_type,
        "origin": "Port A",
        "destination": "Port B",
        "scheduled_time": scheduled_time,
        "updated_at": "2026-03-12T00:00:00Z",
        "created_at": "2026-03-12T00:00:00Z",
        "priority": "normal",
        "delayed": False,
        "delay_duration_minutes": None,
        "failure_reason": None,
        "notes": None,
        "cargo_manifest": None,
        "estimated_arrival": None,
        "started_at": None,
        "completed_at": None,
        "created_by": None,
    }


def _make_job_service() -> MagicMock:
    """Create a mock JobService with the methods used by driver endpoints."""
    es = MagicMock()
    es.update_document = AsyncMock(return_value={"result": "updated"})

    svc = MagicMock()
    svc._es = es
    svc._get_job_doc = AsyncMock()
    svc._append_event = AsyncMock(return_value="evt-123")
    return svc


def _make_driver_app(job_service, scheduling_ws=None, driver_ws=None) -> FastAPI:
    """Create a test FastAPI app with the driver router."""
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(driver_router)

    configure_driver_endpoints(
        job_service=job_service,
        scheduling_ws_manager=scheduling_ws,
        driver_ws_manager=driver_ws,
    )
    return app


def _make_message_app(es_service, job_service, scheduling_ws=None, driver_ws=None) -> FastAPI:
    """Create a test FastAPI app with the message router."""
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(message_router)

    configure_message_endpoints(
        es_service=es_service,
        job_service=job_service,
        scheduling_ws_manager=scheduling_ws,
        driver_ws_manager=driver_ws,
    )
    return app


def _make_exception_app(es_service, job_service, scheduling_ws=None, driver_ws=None) -> FastAPI:
    """Create a test FastAPI app with the exception router."""
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(exception_router)

    configure_exception_endpoints(
        es_service=es_service,
        job_service=job_service,
        scheduling_ws_manager=scheduling_ws,
        driver_ws_manager=driver_ws,
    )
    return app


def _make_pod_app(es_service, job_service, scheduling_ws=None, driver_ws=None) -> FastAPI:
    """Create a test FastAPI app with the POD router."""
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(pod_router)

    configure_pod_endpoints(
        es_service=es_service,
        job_service=job_service,
        scheduling_ws_manager=scheduling_ws,
        driver_ws_manager=driver_ws,
    )
    return app


def _make_es_service() -> MagicMock:
    """Create a mock ElasticsearchService."""
    es = MagicMock()
    es.index_document = AsyncMock()
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [], "total": {"value": 0}},
    })
    return es


# ---------------------------------------------------------------------------
# Test: _check_driver_assignment helper
# ---------------------------------------------------------------------------


class TestCheckDriverAssignment:
    """Tests for the _check_driver_assignment access control helper."""

    def test_assigned_driver_passes(self):
        """Assigned driver passes the check. Validates: Req 11.2"""
        doc = _job_doc(asset_assigned="driver-1")
        # Should not raise
        _check_driver_assignment(doc, "driver-1", "JOB_1")

    def test_non_assigned_driver_raises_403(self):
        """Non-assigned driver gets 403 'Assignment revoked'. Validates: Req 11.2"""
        doc = _job_doc(asset_assigned="driver-2")
        with pytest.raises(AppException) as exc_info:
            _check_driver_assignment(doc, "driver-1", "JOB_1")
        assert exc_info.value.status_code == 403
        assert "Assignment revoked" in exc_info.value.message

    def test_no_asset_assigned_passes(self):
        """Job with no asset_assigned passes (no driver to check against)."""
        doc = _job_doc(asset_assigned=None)
        # Should not raise
        _check_driver_assignment(doc, "driver-1", "JOB_1")

    def test_empty_asset_assigned_passes(self):
        """Job with empty string asset_assigned passes."""
        doc = _job_doc(asset_assigned="")
        # Should not raise (empty string is falsy)
        _check_driver_assignment(doc, "driver-1", "JOB_1")

    def test_error_details_include_driver_ids(self):
        """Error details include requesting and assigned driver IDs. Validates: Req 11.2"""
        doc = _job_doc(asset_assigned="driver-new")
        with pytest.raises(AppException) as exc_info:
            _check_driver_assignment(doc, "driver-old", "JOB_1")
        details = exc_info.value.details
        assert details["requesting_driver"] == "driver-old"
        assert details["assigned_driver"] == "driver-new"
        assert details["job_id"] == "JOB_1"


# ---------------------------------------------------------------------------
# Test: Driver endpoints access control after reassignment
# ---------------------------------------------------------------------------


class TestDriverEndpointsAccessControl:
    """Tests that driver endpoints reject requests from non-assigned drivers."""

    def test_ack_from_previous_driver_returns_403(self):
        """Ack from previous driver after reassignment returns 403. Validates: Req 11.2"""
        svc = _make_job_service()
        # Job is now assigned to driver-2, but driver-1 is requesting
        svc._get_job_doc.return_value = _job_doc(
            status="assigned", asset_assigned="driver-2"
        )

        app = _make_driver_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={"device_id": "mobile-123"},
                headers=_auth_headers(sub="driver-1"),
            )

        assert resp.status_code == 403
        assert "Assignment revoked" in resp.json()["message"]

    def test_accept_from_previous_driver_returns_403(self):
        """Accept from previous driver after reassignment returns 403. Validates: Req 11.2"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(
            status="assigned", asset_assigned="driver-2"
        )

        app = _make_driver_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/accept",
                headers=_auth_headers(sub="driver-1"),
            )

        assert resp.status_code == 403
        assert "Assignment revoked" in resp.json()["message"]

    def test_reject_from_previous_driver_returns_403(self):
        """Reject from previous driver after reassignment returns 403. Validates: Req 11.2"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(
            status="assigned", asset_assigned="driver-2"
        )

        app = _make_driver_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/reject",
                json={"reason": "Not available"},
                headers=_auth_headers(sub="driver-1"),
            )

        assert resp.status_code == 403
        assert "Assignment revoked" in resp.json()["message"]

    def test_ack_from_current_driver_succeeds(self):
        """Ack from current assigned driver succeeds. Validates: Req 11.2"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(
            status="assigned", asset_assigned="driver-1"
        )

        app = _make_driver_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={"device_id": "mobile-123"},
                headers=_auth_headers(sub="driver-1"),
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: Message endpoints access control after reassignment
# ---------------------------------------------------------------------------


class TestMessageEndpointsAccessControl:
    """Tests that message endpoints reject requests from non-assigned drivers."""

    def test_message_from_previous_driver_returns_403(self):
        """Message from previous driver after reassignment returns 403. Validates: Req 11.2"""
        es = _make_es_service()
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(
            status="assigned", asset_assigned="driver-2"
        )

        app = _make_message_app(es, svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Hello",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(sub="driver-1"),
            )

        assert resp.status_code == 403
        assert "Assignment revoked" in resp.json()["message"]

    def test_message_from_current_driver_succeeds(self):
        """Message from current assigned driver succeeds. Validates: Req 11.2"""
        es = _make_es_service()
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(
            status="assigned", asset_assigned="driver-1"
        )

        app = _make_message_app(es, svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Hello",
                    "sender_id": "driver-1",
                    "sender_role": "driver",
                },
                headers=_auth_headers(sub="driver-1"),
            )

        assert resp.status_code == 200

    def test_message_from_dispatcher_always_succeeds(self):
        """Dispatcher messages are not affected by reassignment. Validates: Req 11.2"""
        es = _make_es_service()
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(
            status="assigned", asset_assigned="driver-2"
        )

        app = _make_message_app(es, svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/messages",
                json={
                    "body": "Status update?",
                    "sender_id": "dispatcher-1",
                    "sender_role": "dispatcher",
                },
                headers=_auth_headers(sub="dispatcher-1"),
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: Exception endpoints access control after reassignment
# ---------------------------------------------------------------------------


class TestExceptionEndpointsAccessControl:
    """Tests that exception endpoints reject requests from non-assigned drivers."""

    def test_exception_from_previous_driver_returns_403(self):
        """Exception from previous driver after reassignment returns 403. Validates: Req 11.2"""
        es = _make_es_service()
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(
            status="assigned", asset_assigned="driver-2"
        )

        app = _make_exception_app(es, svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json={
                    "exception_type": "road_closure",
                    "severity": "high",
                    "note": "Road blocked",
                },
                headers=_auth_headers(sub="driver-1"),
            )

        assert resp.status_code == 403
        assert "Assignment revoked" in resp.json()["message"]

    def test_exception_from_current_driver_succeeds(self):
        """Exception from current assigned driver succeeds. Validates: Req 11.2"""
        es = _make_es_service()
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(
            status="assigned", asset_assigned="driver-1"
        )

        app = _make_exception_app(es, svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json={
                    "exception_type": "road_closure",
                    "severity": "high",
                    "note": "Road blocked",
                },
                headers=_auth_headers(sub="driver-1"),
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: POD endpoints access control after reassignment
# ---------------------------------------------------------------------------


class TestPodEndpointsAccessControl:
    """Tests that POD endpoints reject requests from non-assigned drivers."""

    def test_pod_from_previous_driver_returns_403(self):
        """POD from previous driver after reassignment returns 403. Validates: Req 11.2"""
        es = _make_es_service()
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(
            status="in_progress", asset_assigned="driver-2"
        )

        app = _make_pod_app(es, svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json={
                    "recipient_name": "John Doe",
                    "signature_url": "https://example.com/sig.png",
                    "photo_urls": ["https://example.com/photo1.jpg"],
                    "geotag": {"lat": 40.7128, "lng": -74.0060},
                    "timestamp": "2026-03-12T12:00:00Z",
                },
                headers=_auth_headers(sub="driver-1"),
            )

        assert resp.status_code == 403
        assert "Assignment revoked" in resp.json()["message"]

    def test_pod_from_current_driver_succeeds(self):
        """POD from current assigned driver succeeds. Validates: Req 11.2"""
        es = _make_es_service()
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(
            status="in_progress", asset_assigned="driver-1"
        )
        # Mock tenant policies search
        es.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
        }

        app = _make_pod_app(es, svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json={
                    "recipient_name": "John Doe",
                    "signature_url": "https://example.com/sig.png",
                    "photo_urls": ["https://example.com/photo1.jpg"],
                    "geotag": {"lat": 40.7128, "lng": -74.0060},
                    "timestamp": "2026-03-12T12:00:00Z",
                },
                headers=_auth_headers(sub="driver-1"),
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: JobService.reassign_asset WS events and timeline
# ---------------------------------------------------------------------------


class TestReassignAssetWSEvents:
    """Tests for reassignment WS events and timeline in JobService."""

    @pytest.mark.asyncio
    async def test_reassign_publishes_assignment_revoked_to_previous_driver(self):
        """Reassignment publishes assignment_revoked to previous driver. Validates: Req 11.1"""
        from scheduling.services.job_service import JobService

        es = MagicMock()
        es.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{"_source": _job_doc(
                    status="assigned",
                    asset_assigned="driver-old",
                )}],
                "total": {"value": 1},
            },
        })
        es.update_document = AsyncMock(return_value={"result": "updated"})
        es.index_document = AsyncMock()

        svc = JobService(es, redis_url=None)

        # Mock asset verification and availability
        svc._verify_asset_compatible = AsyncMock(return_value={"asset_id": "driver-new"})
        svc._check_asset_availability = AsyncMock()

        # Mock driver WS manager
        driver_ws = MagicMock()
        driver_ws.send_assignment_revoked = AsyncMock(return_value=True)
        driver_ws.send_assignment = AsyncMock(return_value=True)
        svc._driver_ws_manager = driver_ws

        await svc.reassign_asset(
            job_id="JOB_1",
            new_asset_id="driver-new",
            tenant_id="t1",
            actor_id="dispatcher-1",
        )

        # Verify assignment_revoked was sent to previous driver
        driver_ws.send_assignment_revoked.assert_called_once()
        call_args = driver_ws.send_assignment_revoked.call_args
        assert call_args.args[0] == "driver-old"
        revocation_data = call_args.args[1]
        assert revocation_data["job_id"] == "JOB_1"
        assert revocation_data["previous_driver_id"] == "driver-old"
        assert revocation_data["new_driver_id"] == "driver-new"

    @pytest.mark.asyncio
    async def test_reassign_publishes_assignment_to_new_driver(self):
        """Reassignment publishes assignment to new driver. Validates: Req 11.4"""
        from scheduling.services.job_service import JobService

        es = MagicMock()
        es.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{"_source": _job_doc(
                    status="assigned",
                    asset_assigned="driver-old",
                )}],
                "total": {"value": 1},
            },
        })
        es.update_document = AsyncMock(return_value={"result": "updated"})
        es.index_document = AsyncMock()

        svc = JobService(es, redis_url=None)
        svc._verify_asset_compatible = AsyncMock(return_value={"asset_id": "driver-new"})
        svc._check_asset_availability = AsyncMock()

        driver_ws = MagicMock()
        driver_ws.send_assignment_revoked = AsyncMock(return_value=True)
        driver_ws.send_assignment = AsyncMock(return_value=True)
        svc._driver_ws_manager = driver_ws

        await svc.reassign_asset(
            job_id="JOB_1",
            new_asset_id="driver-new",
            tenant_id="t1",
            actor_id="dispatcher-1",
        )

        # Verify assignment was sent to new driver with full job details
        driver_ws.send_assignment.assert_called_once()
        call_args = driver_ws.send_assignment.call_args
        assert call_args.args[0] == "driver-new"
        job_data = call_args.args[1]
        assert job_data["job_id"] == "JOB_1"
        assert job_data["asset_assigned"] == "driver-new"

    @pytest.mark.asyncio
    async def test_reassign_appends_assignment_revoked_event(self):
        """Reassignment appends assignment_revoked event to timeline. Validates: Req 11.3"""
        from scheduling.services.job_service import JobService

        es = MagicMock()
        es.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{"_source": _job_doc(
                    status="assigned",
                    asset_assigned="driver-old",
                )}],
                "total": {"value": 1},
            },
        })
        es.update_document = AsyncMock(return_value={"result": "updated"})
        es.index_document = AsyncMock()

        svc = JobService(es, redis_url=None)
        svc._verify_asset_compatible = AsyncMock(return_value={"asset_id": "driver-new"})
        svc._check_asset_availability = AsyncMock()
        svc._driver_ws_manager = None  # No WS manager

        await svc.reassign_asset(
            job_id="JOB_1",
            new_asset_id="driver-new",
            tenant_id="t1",
            actor_id="dispatcher-1",
        )

        # Verify _append_event was called twice:
        # 1. asset_reassigned event
        # 2. assignment_revoked event
        index_calls = es.index_document.mock_calls
        # Find the assignment_revoked event
        revoked_event = None
        for call in index_calls:
            args = call.args if call.args else call[1]
            if len(args) >= 3:
                doc = args[2]
                if isinstance(doc, dict) and doc.get("event_type") == "assignment_revoked":
                    revoked_event = doc
                    break

        assert revoked_event is not None, "assignment_revoked event not found in index calls"
        assert revoked_event["job_id"] == "JOB_1"
        payload = revoked_event["event_payload"]
        assert payload["previous_driver_id"] == "driver-old"
        assert payload["new_driver_id"] == "driver-new"
        assert "timestamp" in payload

    @pytest.mark.asyncio
    async def test_reassign_ws_failure_does_not_block(self):
        """WS failure during reassignment does not block the operation. Validates: Req 11.1"""
        from scheduling.services.job_service import JobService

        es = MagicMock()
        es.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{"_source": _job_doc(
                    status="assigned",
                    asset_assigned="driver-old",
                )}],
                "total": {"value": 1},
            },
        })
        es.update_document = AsyncMock(return_value={"result": "updated"})
        es.index_document = AsyncMock()

        svc = JobService(es, redis_url=None)
        svc._verify_asset_compatible = AsyncMock(return_value={"asset_id": "driver-new"})
        svc._check_asset_availability = AsyncMock()

        # WS manager that raises on send
        driver_ws = MagicMock()
        driver_ws.send_assignment_revoked = AsyncMock(side_effect=Exception("WS down"))
        driver_ws.send_assignment = AsyncMock(side_effect=Exception("WS down"))
        svc._driver_ws_manager = driver_ws

        # Should not raise despite WS failures
        result = await svc.reassign_asset(
            job_id="JOB_1",
            new_asset_id="driver-new",
            tenant_id="t1",
            actor_id="dispatcher-1",
        )

        assert result.asset_assigned == "driver-new"

    @pytest.mark.asyncio
    async def test_reassign_without_ws_manager_succeeds(self):
        """Reassignment works when no WS manager is wired. Validates: Req 11.1"""
        from scheduling.services.job_service import JobService

        es = MagicMock()
        es.search_documents = AsyncMock(return_value={
            "hits": {
                "hits": [{"_source": _job_doc(
                    status="assigned",
                    asset_assigned="driver-old",
                )}],
                "total": {"value": 1},
            },
        })
        es.update_document = AsyncMock(return_value={"result": "updated"})
        es.index_document = AsyncMock()

        svc = JobService(es, redis_url=None)
        svc._verify_asset_compatible = AsyncMock(return_value={"asset_id": "driver-new"})
        svc._check_asset_availability = AsyncMock()
        svc._driver_ws_manager = None

        result = await svc.reassign_asset(
            job_id="JOB_1",
            new_asset_id="driver-new",
            tenant_id="t1",
            actor_id="dispatcher-1",
        )

        assert result.asset_assigned == "driver-new"
