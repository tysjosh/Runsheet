"""Pause/resume of the process-wide autonomous agents (staging F8).

Before the fix a tenant ``admin`` could pause an autonomous agent — which stops
its polling loop for every tenant — and the audit entry recorded
``tenant_id=None`` and whatever the caller put in an ``x-user-id`` header. A
failure also echoed ``str(e)`` in the 500 body.

Drives the real ``agent_endpoints.router`` with only ``get_tenant_context``
overridden, so both the router-level ops gate and the per-route gate run.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_endpoints
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

AGENT_ID = "inventory_monitor_agent"


def _ctx(*roles: str) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-A",
        user_id="user-1",
        has_pii_access=False,
        roles=list(roles),
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )


def _fake_agent(status: str) -> MagicMock:
    agent = MagicMock()
    agent.status = status
    agent.stop = AsyncMock()
    agent.start = AsyncMock()
    return agent


@pytest.fixture
def activity_log(monkeypatch):
    spy = MagicMock()
    spy.log = AsyncMock(return_value="log-1")
    monkeypatch.setattr(agent_endpoints, "_activity_log_service", spy)
    return spy


def _client(agent, *roles: str) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(agent_endpoints.router)
    app.state.autonomous_agents = {AGENT_ID: agent}
    app.dependency_overrides[get_tenant_context] = lambda: _ctx(*roles)
    return TestClient(app, raise_server_exceptions=False)


class TestTenantAdminCannotControlPlatformAgents:
    def test_admin_cannot_pause(self, activity_log) -> None:
        agent = _fake_agent("running")
        response = _client(agent, "admin").post(f"/api/agent/{AGENT_ID}/pause")

        assert response.status_code == 403
        agent.stop.assert_not_called()
        activity_log.log.assert_not_called()

    def test_admin_cannot_resume(self, activity_log) -> None:
        agent = _fake_agent("stopped")
        response = _client(agent, "admin").post(f"/api/agent/{AGENT_ID}/resume")

        assert response.status_code == 403
        agent.start.assert_not_called()

    def test_platform_admin_without_ops_role_is_refused(self, activity_log) -> None:
        # The router-level ops gate still applies on top.
        agent = _fake_agent("running")
        response = _client(agent, "platform_admin").post(f"/api/agent/{AGENT_ID}/pause")

        assert response.status_code == 403
        agent.stop.assert_not_called()


class TestPlatformAdminAuditUsesVerifiedIdentity:
    @pytest.mark.parametrize(
        "action, status_before, method, action_type",
        [
            ("pause", "running", "stop", "agent_paused"),
            ("resume", "stopped", "start", "agent_resumed"),
        ],
    )
    def test_audit_entry_carries_session_identity(
        self, activity_log, action, status_before, method, action_type
    ) -> None:
        agent = _fake_agent(status_before)
        response = _client(agent, "admin", "platform_admin").post(
            f"/api/agent/{AGENT_ID}/{action}",
            headers={"x-user-id": "someone-else"},
        )

        assert response.status_code == 200, response.text
        getattr(agent, method).assert_awaited_once()
        activity_log.log.assert_awaited_once()
        entry = activity_log.log.call_args[0][0]
        assert entry["action_type"] == action_type
        assert entry["tenant_id"] == "tenant-A"
        assert entry["user_id"] == "user-1"
        assert entry["details"] == {"action": action, "scope": "platform"}


class TestErrorsCarryNoExceptionText:
    @pytest.mark.parametrize(
        "action, status_before, method",
        [("pause", "running", "stop"), ("resume", "stopped", "start")],
    )
    def test_failure_body_omits_exception_text(
        self, activity_log, action, status_before, method
    ) -> None:
        agent = _fake_agent(status_before)
        getattr(agent, method).side_effect = RuntimeError("secret detail")

        response = _client(agent, "admin", "platform_admin").post(
            f"/api/agent/{AGENT_ID}/{action}"
        )

        assert response.status_code == 500
        assert "secret detail" not in response.text
        assert AGENT_ID in response.text
