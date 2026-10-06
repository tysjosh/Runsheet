"""C2: integration-instance writes are admin-only; reads are unchanged.

Before the gate every ``/api/integrations`` route took only a tenant context,
so a dispatcher or driver could create, edit, delete or force-sync a tenant's
integrations. The UI only offers these controls on the admin-only AdminHub.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

import integrations.api.integrations_endpoints as endpoints_module
from errors.handlers import register_exception_handlers
from integrations.api._authz import (
    INTEGRATION_ADMIN_ROLES,
    integration_admin_dependency,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

#: Route -> "admin" (gated) or None (read, ungated). Written out so a new
#: write route without a gate fails here.
EXPECTED_AUDIENCE = {
    ("GET", "/api/integrations"): None,
    ("POST", "/api/integrations"): "admin",
    ("PATCH", "/api/integrations/{instance_id}"): "admin",
    ("DELETE", "/api/integrations/{instance_id}"): "admin",
    ("POST", "/api/integrations/{instance_id}/enable"): "admin",
    ("POST", "/api/integrations/{instance_id}/disable"): "admin",
    ("POST", "/api/integrations/{instance_id}/sync-now"): "admin",
    ("GET", "/api/integrations/{instance_id}/sync-runs"): None,
    ("GET", "/api/integrations/providers"): None,
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
    router = APIRouter(dependencies=[Depends(integration_admin_dependency)])

    @router.get("/probe")
    async def _probe() -> dict:
        return {"ok": True}

    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = lambda: _ctx(*roles)
    return TestClient(app)


def test_policy_is_admin_only() -> None:
    assert INTEGRATION_ADMIN_ROLES == ("admin",)


def test_admin_allowed() -> None:
    assert _client("admin").get("/probe").status_code == 200


@pytest.mark.parametrize("role", ["dispatcher", "driver", "platform_admin"])
def test_non_admin_refused(role: str) -> None:
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
        gated = any(d.dependency is integration_admin_dependency for d in deps)
        if gated != (EXPECTED_AUDIENCE[(method, path)] == "admin"):
            wrong.append((method, path))
    assert wrong == []
