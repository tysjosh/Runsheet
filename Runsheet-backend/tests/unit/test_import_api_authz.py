"""Every import route is admin-only.

Two changes are pinned here.

**Narrowed from admin-or-dispatcher to admin.** A bulk import is the
highest-leverage write in the product: one CSV can create or overwrite the
customer roster, the asset fleet, the driver list, or the inventory catalogue in a
single call, and those rows then feed pricing, routing and readiness decisions for
the whole tenant. It belongs with the role that owns Feature Flags, not with the
dispatcher working a shift.

**Applied at the router, not per handler.** The previous helper was called from
the four mutating routes only, so ``GET /history``, ``GET /history/{session_id}``,
``GET /templates/{data_type}`` and ``GET /schemas/{data_type}`` had no role check
at all — any signed-in user, a driver included, could read what data had been
imported, by whom, and how many rows landed. A router-level gate covers those and
any route added later.

The frontend `import` tab is gated to ``admin`` to match; see
``runsheet/src/config/modules.ts``.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import import_endpoints
from errors.handlers import register_exception_handlers
from import_endpoints import IMPORT_ADMIN_ROLES, import_admin_dependency
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

#: Every route the import router declares. Written out so a new route shows up
#: here as an unexpected key rather than slipping in unexamined.
EXPECTED_ROUTES = {
    ("POST", "/api/import/upload/csv"),
    ("POST", "/api/import/upload/sheets"),
    ("POST", "/api/import/validate"),
    ("POST", "/api/import/commit"),
    ("GET", "/api/import/history"),
    ("GET", "/api/import/history/{session_id}"),
    ("GET", "/api/import/templates/{data_type}"),
    ("GET", "/api/import/schemas/{data_type}"),
}

#: The four that had no gate before this change.
PREVIOUSLY_UNGATED = {
    ("GET", "/api/import/history"),
    ("GET", "/api/import/history/{session_id}"),
    ("GET", "/api/import/templates/{data_type}"),
    ("GET", "/api/import/schemas/{data_type}"),
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


def _client(*roles: str) -> TestClient:
    """Minimal app carrying only the gate, so nothing else explains a 403."""
    from fastapi import APIRouter

    app = FastAPI()
    # The gate raises AppException; without the handlers it would propagate as an
    # exception rather than becoming a 403.
    register_exception_handlers(app)
    router = APIRouter(dependencies=[Depends(import_admin_dependency)])

    @router.get("/probe")
    async def _probe() -> dict:
        return {"ok": True}

    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = lambda: _ctx(*roles)
    return TestClient(app)


class TestPolicy:
    def test_admin_only(self) -> None:
        assert IMPORT_ADMIN_ROLES == ("admin",)

    def test_dispatcher_is_deliberately_excluded(self) -> None:
        # The narrowing is the change; if dispatcher reappears here the decision
        # has been reverted and this test is the place to argue about it.
        assert "dispatcher" not in IMPORT_ADMIN_ROLES


class TestGateBehaviour:
    def test_admin_allowed(self) -> None:
        assert _client("admin").get("/probe").status_code == 200

    def test_dispatcher_is_refused(self) -> None:
        assert _client("dispatcher").get("/probe").status_code == 403

    def test_driver_is_refused(self) -> None:
        assert _client("driver").get("/probe").status_code == 403

    def test_platform_admin_alone_is_refused(self) -> None:
        # platform_admin implies nothing; staff carry admin alongside it.
        assert _client("platform_admin").get("/probe").status_code == 403

    def test_staff_bundle_is_allowed(self) -> None:
        assert _client("admin", "platform_admin").get("/probe").status_code == 200

    def test_no_roles_is_refused(self) -> None:
        assert _client().get("/probe").status_code == 403

    @pytest.mark.parametrize("held", ["admin_ops", "administrator", "notadmin"])
    def test_substring_neighbours_are_refused(self, held: str) -> None:
        # Mirrors Req 4.2 — exact matching, never substring.
        assert _client(held).get("/probe").status_code == 403

    def test_rejection_does_not_echo_held_roles(self) -> None:
        response = _client("some-internal-role-name").get("/probe")
        assert response.status_code == 403
        assert "some-internal-role-name" not in response.text


class TestEveryImportRouteIsGated:
    @staticmethod
    def _routes() -> set[tuple[str, str]]:
        collected = set()
        for route in import_endpoints.router.routes:
            for method in sorted(route.methods or []):
                if method in {"HEAD", "OPTIONS"}:
                    continue
                collected.add((method, route.path))
        return collected

    def test_route_set_matches_expectations(self) -> None:
        assert self._routes() == EXPECTED_ROUTES

    def test_gate_is_attached_to_the_router(self) -> None:
        # Router-level is what makes a future route gated by default. If someone
        # moves this back to per-handler calls, this fails.
        names = [d.dependency.__name__ for d in import_endpoints.router.dependencies]
        assert any("admin" in name for name in names), names

    def test_no_route_is_ungated(self) -> None:
        ungated = [
            (method, route.path)
            for route in import_endpoints.router.routes
            for method in sorted(route.methods or [])
            if method not in {"HEAD", "OPTIONS"} and not route.dependencies
        ]
        assert ungated == [], f"import routes with no role gate: {ungated}"

    def test_the_previously_open_reads_are_now_covered(self) -> None:
        # Named explicitly because these are the ones a driver could read.
        assert PREVIOUSLY_UNGATED <= self._routes()
        for route in import_endpoints.router.routes:
            for method in sorted(route.methods or []):
                if (method, route.path) in PREVIOUSLY_UNGATED:
                    assert route.dependencies, (method, route.path)
