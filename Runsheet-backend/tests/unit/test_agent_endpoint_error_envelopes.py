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
