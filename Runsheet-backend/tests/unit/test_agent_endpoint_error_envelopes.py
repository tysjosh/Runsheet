"""Agent API 500 envelopes carry no exception text (review R6, F3/F8).

F8 dropped ``details.error = str(e)`` from the pause/resume envelopes, but the
approval, activity, autonomy, memory and feedback handlers still returned it.
The supersede guard's search in ``approve()`` added one more way to reach the
approve handler's envelope. Each handler now logs the exception and returns
only its message (and the id it was asked about).

Drives the real ``agent_endpoints.router`` with every wired service raising.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_endpoints
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

SECRET = "psycopg.OperationalError: connection to 10.0.3.17 failed SECRET-91c2"


class _StoreDown(Exception):
    """An unexpected failure. Not ValueError/RuntimeError: those are the
    handlers' 400/409 paths, whose messages are the service's own text."""


class _Raising:
    """Every method is a coroutine that raises with internal detail."""

    def __getattr__(self, name):
        async def _fail(*args, **kwargs):
            raise _StoreDown(SECRET)

        return _fail


@pytest.fixture
def client(monkeypatch) -> TestClient:
    for attr in (
        "_approval_queue_service",
        "_activity_log_service",
        "_autonomy_config_service",
        "_memory_service",
        "_feedback_service",
    ):
        monkeypatch.setattr(agent_endpoints, attr, _Raising())
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(agent_endpoints.router)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id="tenant-A",
        user_id="user-1",
        has_pii_access=False,
        roles=["admin", "platform_admin"],
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", "/api/agent/approvals", None),
        ("POST", "/api/agent/approvals/act-1/approve", None),
        ("POST", "/api/agent/approvals/act-1/reject", {"reason": "no"}),
        ("GET", "/api/agent/activity", None),
        ("GET", "/api/agent/activity/stats", None),
        ("PATCH", "/api/agent/config/autonomy", {"level": "auto-low"}),
        ("GET", "/api/agent/memory", None),
        ("DELETE", "/api/agent/memory/mem-1", None),
        ("GET", "/api/agent/feedback", None),
        ("GET", "/api/agent/feedback/stats", None),
    ],
)
def test_500_body_omits_exception_text(client, method, path, body) -> None:
    response = client.request(method, path, json=body)

    assert response.status_code == 500, response.text
    assert "SECRET-91c2" not in response.text
    assert "10.0.3.17" not in response.text
    assert "error" not in (response.json().get("details") or {})


# ---------------------------------------------------------------------------
# Loading-plan approve/reject mapping (loading-plan-executor K10)
# ---------------------------------------------------------------------------

from Agents.approval_queue_service import (  # noqa: E402
    ApprovalExpiredError,
    ApprovalForbiddenError,
    LoadingPlanExecutionError,
    LoadingPlanOverlapError,
)
from fuel.services.loading_plan_executor import LoadingPlanExecutionResult  # noqa: E402


class _Recording:
    """Approval service stub that records its kwargs and raises ``exc``."""

    def __init__(self, exc=None, value=None):
        self.exc, self.value, self.calls = exc, value, []

    def __getattr__(self, name):
        async def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.exc is not None:
                raise self.exc
            return self.value if self.value is not None else {"items": [], "total": 0}
        return _call


@pytest.fixture
def make_client(client, monkeypatch):
    def _make(service):
        monkeypatch.setattr(agent_endpoints, "_approval_queue_service", service)
        return client
    return _make


def _execution_error(status="incomplete"):
    result = LoadingPlanExecutionResult(
        outcome="incomplete", success=False, replay=False, plan_id="P1", run_id="R1",
        truck_id="T1", failures=[{"order_id": "o2", "reason": "write_failed"}],
        reason="write_failed", retryable=True, writes_made=True,
        message="Loading plan P1 is partly applied: a write failed. Retry to finish it.",
        attempt_id="a1",
    )
    return LoadingPlanExecutionError({"status": status, "parameters": {"plan_id": "P1"}}, result)


def test_approve_passes_session_identity_and_never_agent_actor(make_client):
    svc = _Recording(value={"status": "executed"})
    response = make_client(svc).post("/api/agent/approvals/act-1/approve?reviewer_id=spoof")
    assert response.status_code == 200
    ((name, _args, kwargs),) = svc.calls
    assert name == "approve"
    assert kwargs == {
        "action_id": "act-1", "reviewer_id": "user-1",
        "tenant_id": "tenant-A", "session_user_id": "user-1",
    }


def test_execution_error_maps_to_409_with_details(make_client):
    response = make_client(_Recording(exc=_execution_error())).post("/api/agent/approvals/act-1/approve")
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error_code"] == "LOADING_PLAN_EXECUTION_FAILED"
    assert body["message"].startswith("Loading plan P1 is partly applied")
    assert body["details"] == {
        "action_id": "act-1", "plan_id": "P1", "reason": "write_failed",
        "failures": [{"order_id": "o2", "reason": "write_failed"}],
        "retryable": True, "writes_made": True, "status": "incomplete",
    }


def test_expired_maps_to_409_approval_expired(make_client):
    """N7 supersedes K10's INVALID_STATUS_TRANSITION: one code for expiry."""
    exc = ApprovalExpiredError("act-1", "2026-10-04T00:00:00+00:00")
    response = make_client(_Recording(exc=exc)).post("/api/agent/approvals/act-1/approve")
    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "APPROVAL_EXPIRED"
    assert body["message"] == "This approval expired before a decision was made."
    assert body["details"] == {
        "action_id": "act-1", "expiry_time": "2026-10-04T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# N7: approve/reject of a past-due entry through the real service
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/agent/approvals/action-1/approve", None),
        ("/api/agent/approvals/action-1/reject", {"reason": "late"}),
    ],
)
def test_decision_after_expiry_is_409_approval_expired(make_client, path, body):
    from datetime import datetime, timedelta, timezone

    from tests.unit.test_approval_queue_service import _make_service

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    entry = {
        "action_id": "action-1", "tool_name": "cancel_job",
        "parameters": {"job_id": "JOB_1"}, "risk_level": "high",
        "proposed_by": "ai_agent", "status": "pending",
        "expiry_time": past, "tenant_id": "tenant-A",
    }
    service = _make_service(get_response=entry)

    response = make_client(service).post(path, json=body)

    assert response.status_code == 409, response.text
    payload = response.json()
    assert payload["error_code"] == "APPROVAL_EXPIRED"
    assert payload["details"] == {"action_id": "action-1", "expiry_time": past}
    assert payload.get("request_id")
    assert service._es.stored_document["status"] == "expired"


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/agent/approvals/nope-1/approve", None),
        ("/api/agent/approvals/nope-1/reject", {"reason": "no"}),
    ],
)
@pytest.mark.parametrize("stored", [None, "foreign"])
def test_unknown_or_foreign_approval_is_404(make_client, path, body, stored):
    """An id that doesn't exist, or belongs to another tenant, is 404, not
    400 VALIDATION_ERROR, and both look the same so existence never leaks."""
    from unittest.mock import AsyncMock

    from tests.unit.test_approval_queue_service import _make_service

    service = _make_service()
    service._es.get_document = AsyncMock(return_value=None if stored is None else {
        "action_id": "nope-1", "tool_name": "cancel_job", "parameters": {},
        "status": "pending", "tenant_id": "tenant-B",
    })

    response = make_client(service).post(path, json=body)

    assert response.status_code == 404, response.text
    payload = response.json()
    assert payload["error_code"] == "RESOURCE_NOT_FOUND"
    assert payload["message"] == "Approval not found"
    assert payload["details"] == {"action_id": "nope-1"}
    assert "tenant-B" not in response.text


def test_decided_entry_replay_stays_400(make_client):
    from unittest.mock import AsyncMock

    from tests.unit.test_approval_queue_service import _make_service

    service = _make_service()
    service._es.get_document = AsyncMock(return_value={
        "action_id": "done-1", "tool_name": "cancel_job", "parameters": {},
        "status": "executed", "tenant_id": "tenant-A",
    })
    response = make_client(service).post("/api/agent/approvals/done-1/approve")
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"


def test_forbidden_maps_to_403(make_client):
    response = make_client(_Recording(exc=ApprovalForbiddenError("act-1"))).post("/api/agent/approvals/act-1/approve")
    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN"


def test_overlap_keeps_the_validation_response(make_client):
    response = make_client(_Recording(exc=LoadingPlanOverlapError("conflicts with approved loading plan A"))).post(
        "/api/agent/approvals/act-1/approve"
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "VALIDATION_ERROR"


def test_reject_passes_tenant(make_client):
    svc = _Recording(value={"status": "rejected"})
    response = make_client(svc).post("/api/agent/approvals/act-1/reject", json={"reason": "no"})
    assert response.status_code == 200
    ((name, _args, kwargs),) = svc.calls
    assert name == "reject" and kwargs["tenant_id"] == "tenant-A"


@pytest.mark.parametrize("query, expected", [("", False), ("?include_unresolved=true", True)])
def test_list_forwards_include_unresolved(make_client, query, expected):
    svc = _Recording()
    response = make_client(svc).get(f"/api/agent/approvals{query}")
    assert response.status_code == 200
    ((name, _args, kwargs),) = svc.calls
    assert name == "list_pending" and kwargs["include_unresolved"] is expected
