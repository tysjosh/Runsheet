"""C2: notification writes take admin + dispatcher; reads are unchanged.

Before the gate every ``/api/notifications`` route took only a tenant context,
so a driver could edit rules and templates, rewrite customer preferences or
retry sends. The notifications UI is the admin/dispatcher nav item.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

import notifications.api.endpoints as endpoints_module
from errors.handlers import register_exception_handlers
from notifications.api._authz import (
    NOTIFICATION_WRITE_ROLES,
    notification_write_dependency,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

#: Route -> "write" (gated) or None (read, ungated).
EXPECTED_AUDIENCE = {
    ("GET", "/api/notifications"): None,
    ("GET", "/api/notifications/summary"): None,
    ("GET", "/api/notifications/rules"): None,
    ("PATCH", "/api/notifications/rules/{rule_id}"): "write",
    ("GET", "/api/notifications/preferences"): None,
    ("GET", "/api/notifications/preferences/{customer_id}"): None,
    ("PUT", "/api/notifications/preferences/{customer_id}"): "write",
    ("PUT", "/api/notifications/preferences/{customer_id}/template-opt-outs"): "write",
    ("GET", "/api/notifications/preferences/{customer_id}/template-opt-outs"): None,
    ("GET", "/api/notifications/templates"): None,
    ("PUT", "/api/notifications/templates/{template_id}"): "write",
    ("GET", "/api/notifications/{notification_id}"): None,
    ("POST", "/api/notifications/{notification_id}/retry"): "write",
}


def _ctx(*roles: str) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-A",
        user_id="user-1",
        has_pii_access=False,
        roles=list(roles),
    )


def _client(*roles: str) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    router = APIRouter(dependencies=[Depends(notification_write_dependency)])

    @router.get("/probe")
    async def _probe() -> dict:
        return {"ok": True}

    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = lambda: _ctx(*roles)
    return TestClient(app)


def test_policy_is_admin_and_dispatcher() -> None:
    assert NOTIFICATION_WRITE_ROLES == ("admin", "dispatcher")


@pytest.mark.parametrize("role", ["admin", "dispatcher"])
def test_operations_roles_allowed(role: str) -> None:
    assert _client(role).get("/probe").status_code == 200


@pytest.mark.parametrize("role", ["driver", "platform_admin"])
def test_other_roles_refused(role: str) -> None:
    response = _client(role).get("/probe")
    assert response.status_code == 403
    assert response.json()["error_code"] == "INSUFFICIENT_ROLE"


def _routes() -> list[tuple[str, str, list]]:
    collected = []
    for route in endpoints_module.router.routes:
        for method in sorted(route.methods or []):
            if method in {"HEAD", "OPTIONS"}:
                continue
            collected.append((method, route.path, list(route.dependencies)))
    return collected


def test_route_set_matches_expectations() -> None:
    assert {(m, p) for m, p, _ in _routes()} == set(EXPECTED_AUDIENCE)


def test_each_route_carries_its_expected_audience() -> None:
    wrong = []
    for method, path, deps in _routes():
        gated = any(d.dependency is notification_write_dependency for d in deps)
        if gated != (EXPECTED_AUDIENCE[(method, path)] == "write"):
            wrong.append((method, path))
    assert wrong == []
