"""
Unit tests for driver acknowledgment endpoints (ack, accept, reject).

Tests state validation, event recording, status transitions, and error
handling for the driver acknowledgment endpoints under /api/scheduling.

Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5
"""

import sys
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.support.auth_seam import auth_headers, install_test_auth

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

TENANT_ID = "t1"

# Endpoints authenticate via the Test_Auth_Path dependency-override seam
# (installed per-app in ``_make_app``); no legacy-JWT settings patch needed.
_SETTINGS_PATCH = nullcontext()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


DRIVER_ID = "DRV-1"


def _auth_headers(
    tenant_id: str = TENANT_ID,
    *,
    sub: str = "driver-1",
    roles=("driver",),
    driver_id=DRIVER_ID,
) -> dict:
    """A driver session: the exact ``driver`` role and a canonical driver_id.

    The scheduling ack/accept/reject endpoints gate on
    ``require_driver_identity`` (B5), so the default context is a driver.
    """
    return auth_headers(tenant_id, sub=sub, roles=list(roles), driver_id=driver_id)


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
    install_test_auth(app)
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
        doc = _job_doc(status="assigned")
        doc["assigned_driver_id"] = DRIVER_ID  # already linked: a pure read
        svc._get_job_doc.return_value = doc

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
# Test: accept_job writes the canonical driver identifier (Req 1.13, 1.14)
# ---------------------------------------------------------------------------


def _install_driver_session(
    app,
    *,
    driver_id,
    user_id: str = "driver-1",
    tenant_id: str = TENANT_ID,
) -> None:
    """Override the tenant context with a driver session carrying driver_id.

    The header-driven seam in ``tests.support.auth_seam`` carries no driver
    identity yet, so the driver-surface contexts are issued directly through
    ``issue_test_context``.
    """
    from auth.test_auth import issue_test_context
    from ops.middleware.tenant_guard import get_tenant_context

    app.dependency_overrides[get_tenant_context] = lambda: issue_test_context(
        tenant_id,
        roles=["driver"],
        has_pii_access=False,
        user_id=user_id,
        driver_id=driver_id,
    )


class TestAcceptJobRecordsAssignedDriverId:
    """Accept records both identifier namespaces on the job document.

    Validates: Requirements 1.13, 1.14
    """

    def test_scheduled_accept_stamps_driver_id_without_asset_assigned(self):
        """Claiming a job stamps assigned_driver_id and never writes asset_assigned.

        ``asset_assigned`` holds the dispatcher's truck, so accept leaves it
        alone (B5, D3).
        """
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="scheduled", asset_assigned=None)

        app = _make_app(svc)
        _install_driver_session(app, driver_id="DRV-77")
        with _SETTINGS_PATCH:
            resp = TestClient(app).post(
                "/api/scheduling/jobs/JOB_1/accept",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        update_fields = svc._es.update_document.call_args.args[2]
        assert "asset_assigned" not in update_fields
        assert update_fields["assigned_driver_id"] == "DRV-77"
        assert update_fields["status"] == "assigned"

    def test_assigned_accept_backfills_only_the_driver_id(self):
        """Confirming an assignment stamps driver_id without touching status."""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        app = _make_app(svc)
        _install_driver_session(app, driver_id="DRV-77")
        with _SETTINGS_PATCH:
            resp = TestClient(app).post(
                "/api/scheduling/jobs/JOB_1/accept",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["new_status"] == "assigned"
        update_fields = svc._es.update_document.call_args.args[2]
        assert update_fields["assigned_driver_id"] == "DRV-77"
        assert "status" not in update_fields
        assert "asset_assigned" not in update_fields

    def test_already_linked_accept_writes_nothing(self):
        """A repeated accept for the same driver stays a read."""
        svc = _make_job_service()
        doc = _job_doc(status="assigned")
        doc["assigned_driver_id"] = "DRV-77"
        svc._get_job_doc.return_value = doc

        app = _make_app(svc)
        _install_driver_session(app, driver_id="DRV-77")
        with _SETTINGS_PATCH:
            resp = TestClient(app).post(
                "/api/scheduling/jobs/JOB_1/accept",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        svc._es.update_document.assert_not_called()

    def test_session_without_driver_id_is_refused(self):
        """No driver claim means 403 DRIVER_IDENTITY_MISSING and no write (B5)."""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="scheduled", asset_assigned=None)

        app = _make_app(svc)
        _install_driver_session(app, driver_id=None)
        with _SETTINGS_PATCH:
            resp = TestClient(app).post(
                "/api/scheduling/jobs/JOB_1/accept",
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "DRIVER_IDENTITY_MISSING"
        svc._es.update_document.assert_not_called()


# ---------------------------------------------------------------------------
# Test: B5 driver-role gate and assigned-driver check (D3)
# ---------------------------------------------------------------------------

#: (action, request body) for each driver action endpoint.
_ACTIONS = [
    ("ack", {"device_id": "mobile-123"}),
    ("accept", None),
    ("reject", {"reason": "Not available"}),
]


def _post_action(app, action, body, headers):
    client = TestClient(app)
    url = f"/api/scheduling/jobs/JOB_1/{action}"
    if body is None:
        return client.post(url, headers=headers)
    return client.post(url, json=body, headers=headers)


class TestDriverAuthorization:
    """ack/accept/reject require a driver identity and the job's own driver.

    Validates: B5 (D3), Requirement 11.2, R15.14
    """

    @pytest.mark.parametrize("action,body", _ACTIONS)
    @pytest.mark.parametrize("field", ["assigned_driver_id", "driver_id"])
    def test_other_named_driver_is_403(self, action, body, field):
        """A job naming another driver refuses the caller, whatever asset_assigned says."""
        svc = _make_job_service()
        doc = _job_doc(status="assigned", asset_assigned="driver-1")
        doc[field] = "DRV-OTHER"
        svc._get_job_doc.return_value = doc

        resp = _post_action(_make_app(svc), action, body, _auth_headers())

        assert resp.status_code == 403
        payload = resp.json()
        assert payload["message"] == "Assignment revoked"
        # Details name the job only, never either driver (R15.14).
        assert payload["details"] == {"job_id": "JOB_1"}
        svc._es.update_document.assert_not_called()
        svc._append_event.assert_not_called()

    @pytest.mark.parametrize("action,body", _ACTIONS)
    def test_admin_without_driver_role_is_403_insufficient_role(self, action, body):
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        resp = _post_action(
            _make_app(svc), action, body,
            _auth_headers(roles=("admin",), driver_id=None),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"
        svc._get_job_doc.assert_not_called()
        svc._append_event.assert_not_called()

    @pytest.mark.parametrize("action,body", _ACTIONS)
    def test_driver_without_driver_id_is_403_identity_missing(self, action, body):
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        resp = _post_action(_make_app(svc), action, body, _auth_headers(driver_id=None))

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "DRIVER_IDENTITY_MISSING"
        svc._append_event.assert_not_called()

    def test_accept_keeps_the_dispatchers_truck(self):
        """Accepting a truck-assigned job named for the caller keeps TRUCK-1."""
        svc = _make_job_service()
        doc = _job_doc(status="scheduled", asset_assigned="TRUCK-1")
        doc["driver_id"] = DRIVER_ID
        svc._get_job_doc.return_value = doc

        resp = _post_action(_make_app(svc), "accept", None, _auth_headers())

        assert resp.status_code == 200
        update_fields = svc._es.update_document.call_args.args[2]
        assert "asset_assigned" not in update_fields
        assert update_fields["assigned_driver_id"] == DRIVER_ID
        assert update_fields["status"] == "assigned"
        assert doc["asset_assigned"] == "TRUCK-1"

    @pytest.mark.parametrize("action,body", _ACTIONS)
    def test_truck_assigned_job_without_named_driver_is_403(self, action, body):
        """asset_assigned holding a truck id is not the caller's claim."""
        svc = _make_job_service()
        status = "scheduled" if action == "accept" else "assigned"
        svc._get_job_doc.return_value = _job_doc(status=status, asset_assigned="TRUCK-9")

        resp = _post_action(_make_app(svc), action, body, _auth_headers())

        assert resp.status_code == 403
        assert resp.json()["details"] == {"job_id": "JOB_1"}
        svc._es.update_document.assert_not_called()

    @pytest.mark.parametrize("action,body", _ACTIONS)
    def test_pre_migration_doc_keyed_on_user_id_is_allowed(self, action, body):
        """A job that stored the caller's user id in asset_assigned still authorizes."""
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned", asset_assigned="driver-1")

        resp = _post_action(_make_app(svc), action, body, _auth_headers())

        assert resp.status_code == 200

    @pytest.mark.parametrize("action,body", _ACTIONS)
    def test_named_caller_is_allowed_on_truck_assigned_job(self, action, body):
        svc = _make_job_service()
        doc = _job_doc(status="assigned", asset_assigned="TRUCK-1")
        doc["assigned_driver_id"] = DRIVER_ID
        svc._get_job_doc.return_value = doc

        resp = _post_action(_make_app(svc), action, body, _auth_headers())

        assert resp.status_code == 200
        if svc._es.update_document.called:
            assert "asset_assigned" not in svc._es.update_document.call_args.args[2]

    def test_unclaimed_scheduled_job_can_be_accepted(self):
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="scheduled", asset_assigned="")

        resp = _post_action(_make_app(svc), "accept", None, _auth_headers())

        assert resp.status_code == 200
        assert svc._es.update_document.call_args.args[2]["assigned_driver_id"] == DRIVER_ID

    def test_events_and_broadcasts_keep_the_user_id_as_actor(self):
        svc = _make_job_service()
        svc._get_job_doc.return_value = _job_doc(status="assigned")

        resp = _post_action(_make_app(svc), "ack", {"device_id": "d"}, _auth_headers())

        assert resp.status_code == 200
        assert svc._append_event.call_args.kwargs["actor_id"] == "driver-1"
        assert resp.json()["data"]["actor_id"] == "driver-1"


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
