"""The inventory REST surface enforces roles, split by what the call does.

The regression: all nine routes under ``/api/inventory`` resolved a
``TenantContext`` with no role check. Verified live before the fix — the
``driver`` account got HTTP 200 from ``GET /api/inventory/items``.

The split is the point, and a blanket admin-only gate would be wrong. This module
exists to feed operational decisions: ``inventory-pipeline-integration``
Requirement 1 is the dispatcher's story — verify critical parts are in stock
before assigning a truck. A dispatcher locked out of reads loses the readiness
indicator on assignment and the alert badge on ``/ops/control``.

So reads and stock adjustments take the operations roles, while catalogue
mutations (create / update / delete an item) are ``admin`` only: a part number's
``compatible_assets`` value changes what every readiness check in the tenant
evaluates against, which is master data, not shift work.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import inventory.api.endpoints as endpoints_module
from errors.handlers import register_exception_handlers
from inventory.api._authz import (
    INVENTORY_ADMIN_ROLES,
    INVENTORY_OPS_ROLES,
    inventory_admin_dependency,
    inventory_ops_dependency,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

#: Route → the dependency it must carry. Written out so a route that silently
#: changes audience fails here rather than in production.
EXPECTED_AUDIENCE = {
    ("GET", "/api/inventory/items"): "ops",
    ("GET", "/api/inventory/items/{item_id}"): "ops",
    ("GET", "/api/inventory/alerts"): "ops",
    ("GET", "/api/inventory/summary"): "ops",
    ("GET", "/api/inventory/items/{item_id}/history"): "ops",
    ("POST", "/api/inventory/items/{item_id}/adjust"): "ops",
    ("POST", "/api/inventory/items"): "admin",
    ("PATCH", "/api/inventory/items/{item_id}"): "admin",
    ("DELETE", "/api/inventory/items/{item_id}"): "admin",
}

_DEPENDENCY_BY_AUDIENCE = {
    "ops": inventory_ops_dependency,
    "admin": inventory_admin_dependency,
}


def _ctx(*roles: str) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-A",
        user_id="user-1",
        has_pii_access=False,
        roles=list(roles),
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )


def _client(dependency, *roles: str) -> TestClient:
    """Minimal app carrying only the gate, so nothing else explains a 403."""
    from fastapi import APIRouter, Depends

    app = FastAPI()
    # The gate raises AppException; without the handlers it would propagate as an
    # exception rather than becoming a 403.
    register_exception_handlers(app)
    router = APIRouter(dependencies=[Depends(dependency)])

    @router.get("/probe")
    async def _probe() -> dict:
        return {"ok": True}

    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = lambda: _ctx(*roles)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Policy values
# ---------------------------------------------------------------------------


class TestPolicy:
    def test_reads_and_adjustments_take_the_operations_roles(self) -> None:
        assert INVENTORY_OPS_ROLES == ("admin", "dispatcher")

    def test_catalogue_mutations_are_admin_only(self) -> None:
        assert INVENTORY_ADMIN_ROLES == ("admin",)

    def test_dispatcher_is_deliberately_excluded_from_mutations(self) -> None:
        # The asymmetry is the whole design; collapsing it in either direction
        # either locks the dispatcher out of readiness or lets them edit master
        # data.
        assert "dispatcher" in INVENTORY_OPS_ROLES
        assert "dispatcher" not in INVENTORY_ADMIN_ROLES


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


class TestReadAndAdjustGate:
    @pytest.mark.parametrize("role", ["admin", "dispatcher"])
    def test_operations_roles_allowed(self, role: str) -> None:
        assert _client(inventory_ops_dependency, role).get("/probe").status_code == 200

    def test_driver_is_refused(self) -> None:
        # The account that could list inventory items before this gate.
        assert _client(inventory_ops_dependency, "driver").get("/probe").status_code == 403

    def test_platform_admin_alone_is_refused(self) -> None:
        response = _client(inventory_ops_dependency, "platform_admin").get("/probe")
        assert response.status_code == 403


class TestCatalogueMutationGate:
    def test_admin_allowed(self) -> None:
        assert _client(inventory_admin_dependency, "admin").get("/probe").status_code == 200

    def test_dispatcher_is_refused(self) -> None:
        # A dispatcher may consume stock but not redefine the catalogue.
        response = _client(inventory_admin_dependency, "dispatcher").get("/probe")
        assert response.status_code == 403

    def test_driver_is_refused(self) -> None:
        assert _client(inventory_admin_dependency, "driver").get("/probe").status_code == 403

    def test_rejection_does_not_echo_held_roles(self) -> None:
        response = _client(inventory_admin_dependency, "some-internal-role").get("/probe")
        assert response.status_code == 403
        assert "some-internal-role" not in response.text


# ---------------------------------------------------------------------------
# Drift guard — derived from the router, not from prose
# ---------------------------------------------------------------------------


class TestEveryRouteIsGated:
    @staticmethod
    def _routes() -> list[tuple[str, str, list]]:
        collected = []
        for route in endpoints_module.router.routes:
            for method in sorted(route.methods or []):
                if method in {"HEAD", "OPTIONS"}:
                    continue
                collected.append((method, route.path, list(route.dependencies)))
        return collected

    def test_route_set_matches_expectations(self) -> None:
        # Guards the guard: a new route appears here as an unexpected key rather
        # than slipping through unexamined.
        actual = {(m, p) for m, p, _ in self._routes()}
        assert actual == set(EXPECTED_AUDIENCE)

    def test_no_route_is_ungated(self) -> None:
        ungated = [(m, p) for m, p, deps in self._routes() if not deps]
        assert ungated == [], f"inventory routes with no role gate: {ungated}"

    def test_each_route_carries_its_expected_audience(self) -> None:
        wrong = []
        for method, path, deps in self._routes():
            expected = _DEPENDENCY_BY_AUDIENCE[EXPECTED_AUDIENCE[(method, path)]]
            if not any(d.dependency is expected for d in deps):
                wrong.append((method, path))
        assert wrong == [], (
            "inventory routes carrying the wrong audience: "
            f"{wrong}. Reads/adjust take inventory_ops_dependency; create, update "
            "and delete take inventory_admin_dependency."
        )
