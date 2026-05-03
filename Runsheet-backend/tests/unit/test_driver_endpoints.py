"""
Unit tests for driver acknowledgment endpoints (ack, accept, reject).

Tests state validation, event recording, status transitions, and error
handling for the driver acknowledgment endpoints under /api/scheduling.

Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5
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

from errors.exceptions import AppException
from scheduling.api.driver_endpoints import (
    router as driver_router,
    configure_driver_endpoints,
    _validate_job_state,
    _ALLOWED_STATES,
    _STATE_ALLOWED_ACTIONS,
)
from scheduling.models import JobStatus

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
    """Return a minimal job document with configurable status."""
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
    """Create a mock JobService with the methods used by driver endpoints."""
    es = MagicMock()
    es.update_document = AsyncMock(return_value={"result": "updated"})

    svc = MagicMock()
    svc._es = es
    svc._get_job_doc = AsyncMock()
    svc._append_event = AsyncMock(return_value="evt-123")
    return svc


def _make_app(job_service, scheduling_ws=None, driver_ws=None) -> FastAPI:
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


# ---------------------------------------------------------------------------
# Test: _validate_job_state (pure function, no HTTP needed)
# ---------------------------------------------------------------------------


class TestValidateJobState:
    """Tests for the _validate_job_state helper function."""

    def test_ack_valid_in_assigned(self):
        """ack is valid when job is assigned. Validates: Req 5.1"""
        doc = _job_doc(status="assigned")
        _validate_job_state(doc, "ack", "JOB_1")

    def test_ack_invalid_in_scheduled(self):
        """ack is invalid when job is scheduled. Validates: Req 5.4"""
        doc = _job_doc(status="scheduled")
        with pytest.raises(AppException) as exc_info:
            _validate_job_state(doc, "ack", "JOB_1")
        assert exc_info.value.status_code == 400
        assert exc_info.value.details["current_status"] == "scheduled"
        assert "allowed_actions" in exc_info.value.details

    def test_ack_invalid_in_completed(self):
        """ack is invalid when job is completed. Validates: Req 5.4"""
        doc = _job_doc(status="completed")
        with pytest.raises(AppException) as exc_info:
            _validate_job_state(doc, "ack", "JOB_1")
        assert exc_info.value.status_code == 400
        assert exc_info.value.details["allowed_actions"] == []

    def test_accept_valid_in_scheduled(self):
        """accept is valid when job is scheduled. Validates: Req 5.2"""
        doc = _job_doc(status="scheduled")
        _validate_job_state(doc, "accept", "JOB_1")

    def test_accept_valid_in_assigned(self):
        """accept is valid when job is assigned. Validates: Req 5.2"""
        doc = _job_doc(status="assigned")
        _validate_job_state(doc, "accept", "JOB_1")

    def test_accept_invalid_in_in_progress(self):
        """accept is invalid when job is in_progress. Validates: Req 5.4"""
        doc = _job_doc(status="in_progress")
        with pytest.raises(AppException) as exc_info:
            _validate_job_state(doc, "accept", "JOB_1")
        assert exc_info.value.status_code == 400

    def test_reject_valid_in_assigned(self):
        """reject is valid when job is assigned. Validates: Req 5.3"""
        doc = _job_doc(status="assigned")
        _validate_job_state(doc, "reject", "JOB_1")

    def test_reject_invalid_in_scheduled(self):
        """reject is invalid when job is scheduled. Validates: Req 5.4"""
        doc = _job_doc(status="scheduled")
        with pytest.raises(AppException) as exc_info:
            _validate_job_state(doc, "reject", "JOB_1")
        assert exc_info.value.status_code == 400

    def test_error_includes_allowed_actions_for_scheduled(self):
        """Error for scheduled state includes 'accept' as allowed. Validates: Req 5.4"""
        doc = _job_doc(status="scheduled")
        with pytest.raises(AppException) as exc_info:
            _validate_job_state(doc, "ack", "JOB_1")
        assert "accept" in exc_info.value.details["allowed_actions"]

    def test_error_includes_allowed_actions_for_assigned(self):
        """Error for assigned state includes ack/accept/reject. Validates: Req 5.4"""
        doc = _job_doc(status="in_progress")
        with pytest.raises(AppException) as exc_info:
            _validate_job_state(doc, "ack", "JOB_1")
        # in_progress has no allowed driver actions
        assert exc_info.value.details["allowed_actions"] == []


# ---------------------------------------------------------------------------
# Test: ack_job endpoint
# ---------------------------------------------------------------------------


class TestAckJob:
    """Tests for the POST /jobs/{job_id}/ack endpoint."""

    def test_ack_assigned_job_succeeds(self):
        """Ack on assigned job records event. Validates: Req 5.1"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={"device_id": "mobile-123"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["action"] == "ack"
        assert data["job_id"] == "JOB_1"
        assert data["actor_id"] == "driver-1"
        assert data["device_id"] == "mobile-123"
        assert "timestamp" in data

    def test_ack_appends_event(self):
        """Ack appends an 'ack' event to the job timeline. Validates: Req 5.1"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={"device_id": "mobile-123"},
                headers=_auth_headers(),
            )

        svc._append_event.assert_called_once()
        call_kwargs = svc._append_event.call_args.kwargs
        assert call_kwargs["event_type"] == "ack"
        assert call_kwargs["job_id"] == "JOB_1"
        assert call_kwargs["actor_id"] == "driver-1"
        assert call_kwargs["payload"]["device_id"] == "mobile-123"

    def test_ack_scheduled_job_returns_400(self):
        """Ack on scheduled job returns 400. Validates: Req 5.4"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="scheduled")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={"device_id": "mobile-123"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 400
        body = resp.json()
        assert body["details"]["current_status"] == "scheduled"
        assert "allowed_actions" in body["details"]

    def test_ack_without_device_id(self):
        """Ack without device_id still succeeds (optional field). Validates: Req 5.1"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={},
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["device_id"] is None


# ---------------------------------------------------------------------------
# Test: accept_job endpoint
# ---------------------------------------------------------------------------


class TestAcceptJob:
    """Tests for the POST /jobs/{job_id}/accept endpoint."""

    def test_accept_scheduled_transitions_to_assigned(self):
        """Accept on scheduled job transitions to assigned. Validates: Req 5.2"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="scheduled")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/accept",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["action"] == "accept"
        assert data["previous_status"] == "scheduled"
        assert data["new_status"] == "assigned"

        # Verify ES update was called to transition status
        svc._es.update_document.assert_called_once()
        update_call = svc._es.update_document.call_args
        assert update_call.args[2]["status"] == "assigned"

    def test_accept_assigned_confirms_without_transition(self):
        """Accept on assigned job confirms without changing status. Validates: Req 5.2"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/accept",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["previous_status"] == "assigned"
        assert data["new_status"] == "assigned"

        # No ES update needed for confirmation
        svc._es.update_document.assert_not_called()

    def test_accept_appends_event(self):
        """Accept appends an 'accept' event to the job timeline. Validates: Req 5.2"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="scheduled")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/scheduling/jobs/JOB_1/accept",
                headers=_auth_headers(),
            )

        svc._append_event.assert_called_once()
        call_kwargs = svc._append_event.call_args.kwargs
        assert call_kwargs["event_type"] == "accept"
        assert call_kwargs["job_id"] == "JOB_1"

    def test_accept_in_progress_returns_400(self):
        """Accept on in_progress job returns 400. Validates: Req 5.4"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="in_progress")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/accept",
                headers=_auth_headers(),
            )

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Test: reject_job endpoint
# ---------------------------------------------------------------------------


class TestRejectJob:
    """Tests for the POST /jobs/{job_id}/reject endpoint."""

    def test_reject_assigned_reverts_to_scheduled(self):
        """Reject on assigned job reverts to scheduled. Validates: Req 5.3"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/reject",
                json={"reason": "Too far away"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["action"] == "reject"
        assert data["previous_status"] == "assigned"
        assert data["new_status"] == "scheduled"
        assert data["reason"] == "Too far away"

        # Verify ES update was called to revert status
        svc._es.update_document.assert_called_once()
        update_call = svc._es.update_document.call_args
        assert update_call.args[2]["status"] == "scheduled"

    def test_reject_appends_event_with_reason(self):
        """Reject appends a 'reject' event with reason. Validates: Req 5.3"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/scheduling/jobs/JOB_1/reject",
                json={"reason": "Vehicle breakdown"},
                headers=_auth_headers(),
            )

        svc._append_event.assert_called_once()
        call_kwargs = svc._append_event.call_args.kwargs
        assert call_kwargs["event_type"] == "reject"
        assert call_kwargs["payload"]["reason"] == "Vehicle breakdown"

    def test_reject_scheduled_job_returns_400(self):
        """Reject on scheduled job returns 400. Validates: Req 5.4"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="scheduled")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/reject",
                json={"reason": "Not available"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 400

    def test_reject_completed_job_returns_400(self):
        """Reject on completed job returns 400. Validates: Req 5.4"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="completed")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/reject",
                json={"reason": "Changed mind"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 400
        body = resp.json()
        assert body["details"]["allowed_actions"] == []

    def test_reject_without_reason_returns_422(self):
        """Reject without reason returns 422 (Pydantic validation). Validates: Req 5.3"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        app = _make_app(svc)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/reject",
                json={},
                headers=_auth_headers(),
            )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test: WS broadcast
# ---------------------------------------------------------------------------


class TestBroadcast:
    """Tests for WebSocket broadcast on driver actions."""

    def test_ack_broadcasts_through_scheduling_ws(self):
        """Ack broadcasts event through scheduling WS. Validates: Req 5.5"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()

        app = _make_app(svc, scheduling_ws=ws_manager)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={"device_id": "mobile-123"},
                headers=_auth_headers(),
            )

        ws_manager.broadcast.assert_called_once()
        call_args = ws_manager.broadcast.call_args
        assert call_args.args[0] == "driver_ack"
        assert call_args.args[1]["job_id"] == "JOB_1"

    def test_broadcast_failure_does_not_propagate(self):
        """WS broadcast failure does not break the endpoint. Validates: Req 5.5"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock(side_effect=Exception("WS down"))

        app = _make_app(svc, scheduling_ws=ws_manager)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/scheduling/jobs/JOB_1/ack",
                json={"device_id": "mobile-123"},
                headers=_auth_headers(),
            )

        # Should succeed despite WS failure
        assert resp.status_code == 200
        assert resp.json()["data"]["action"] == "ack"

    def test_accept_broadcasts_through_scheduling_ws(self):
        """Accept broadcasts event through scheduling WS. Validates: Req 5.5"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()

        app = _make_app(svc, scheduling_ws=ws_manager)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/scheduling/jobs/JOB_1/accept",
                headers=_auth_headers(),
            )

        ws_manager.broadcast.assert_called_once()
        call_args = ws_manager.broadcast.call_args
        assert call_args.args[0] == "driver_accept"

    def test_reject_broadcasts_through_scheduling_ws(self):
        """Reject broadcasts event through scheduling WS. Validates: Req 5.5"""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()

        app = _make_app(svc, scheduling_ws=ws_manager)
        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/scheduling/jobs/JOB_1/reject",
                json={"reason": "Not available"},
                headers=_auth_headers(),
            )

        ws_manager.broadcast.assert_called_once()
        call_args = ws_manager.broadcast.call_args
        assert call_args.args[0] == "driver_reject"


# ---------------------------------------------------------------------------
# Test: Allowed states mapping
# ---------------------------------------------------------------------------


class TestAllowedStatesMapping:
    """Tests for the allowed states configuration."""

    def test_ack_only_allowed_in_assigned(self):
        """ack is only allowed in assigned state."""
        assert _ALLOWED_STATES["ack"] == {JobStatus.ASSIGNED}

    def test_accept_allowed_in_scheduled_and_assigned(self):
        """accept is allowed in scheduled and assigned states."""
        assert _ALLOWED_STATES["accept"] == {
            JobStatus.SCHEDULED,
            JobStatus.ASSIGNED,
        }

    def test_reject_only_allowed_in_assigned(self):
        """reject is only allowed in assigned state."""
        assert _ALLOWED_STATES["reject"] == {JobStatus.ASSIGNED}

    def test_assigned_state_allows_all_driver_actions(self):
        """assigned state allows ack, accept, and reject."""
        actions = _STATE_ALLOWED_ACTIONS[JobStatus.ASSIGNED]
        assert "ack" in actions
        assert "accept" in actions
        assert "reject" in actions

    def test_terminal_states_have_no_allowed_actions(self):
        """Terminal states (completed, cancelled, failed) have no allowed actions."""
        for status in [JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED]:
            assert _STATE_ALLOWED_ACTIONS[status] == []
