"""Every compliance router enforces a role.

The regression: all eight endpoint modules under ``compliance/api/`` resolved a
``TenantContext`` per handler and performed no role check. Verified against a
running server before the fix — the ``driver`` account got HTTP 200 from
``GET /api/compliance/meters``. These are DOT / FMCSA / IRS / IFTA records:
driver qualification, asset certifications, the meter audit trail, terminal bills
of lading, IFTA returns, k-factor calibration, tax jurisdictions and exemptions.

The audience is the operations roles, not staff. Compliance is the customer's own
regulatory obligation and a dispatcher checks certification expiry before
assigning a driver, so ``platform_admin``-only would lock a customer out of their
own records. That asymmetry with ``commerce/api`` (staff-only, because the ERP
owns the authoritative price) is the point worth pinning.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import compliance.api
from compliance.api._authz import (
    COMPLIANCE_OPS_ROLES,
    compliance_ops_dependency,
)
from errors.exceptions import AppException
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


def _ctx(*roles: str) -> TenantContext:
    return TenantContext(
        tenant_id="tenant-A",
        user_id="user-1",
        has_pii_access=False,
        roles=list(roles),
        region="US",
        measurement_units={"volume": "gal", "distance": "mi"},
    )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class TestCompliancePolicy:
    def test_audience_is_the_operations_roles(self) -> None:
        # Pinned as a value, not just as behaviour: widening this tuple is a
        # decision that should have to be made here.
        assert COMPLIANCE_OPS_ROLES == ("admin", "dispatcher")

    def test_not_staff_only(self) -> None:
        # Compliance is the customer's own DOT/IRS obligation. If this ever
        # becomes platform_admin-only, a customer loses access to their own
        # regulated records — the opposite of the commerce/api decision.
        assert "platform_admin" not in COMPLIANCE_OPS_ROLES


# ---------------------------------------------------------------------------
# Behaviour, through HTTP
# ---------------------------------------------------------------------------


def _app_with(*roles: str) -> TestClient:
    """A minimal app carrying only the gate, so nothing else can explain a 403."""
    app = FastAPI()
    # The gate raises AppException; without the handlers it would propagate as an
    # exception rather than becoming a 403, and the assertions would be checking
    # the wrong thing.
    register_exception_handlers(app)

    from fastapi import APIRouter, Depends

    router = APIRouter(dependencies=[Depends(compliance_ops_dependency)])

    @router.get("/probe")
    async def _probe() -> dict:
        return {"ok": True}

    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = lambda: _ctx(*roles)
    return TestClient(app)


class TestGateBehaviour:
    @pytest.mark.parametrize("role", ["admin", "dispatcher"])
    def test_operations_roles_allowed(self, role: str) -> None:
        assert _app_with(role).get("/probe").status_code == 200

    def test_driver_is_refused(self) -> None:
        # The account that could read the meter audit trail before this gate.
        assert _app_with("driver").get("/probe").status_code == 403

    def test_platform_admin_alone_is_refused(self) -> None:
        # platform_admin implies nothing; staff hold an operations role too.
        assert _app_with("platform_admin").get("/probe").status_code == 403

    def test_staff_bundle_is_allowed(self) -> None:
        assert _app_with("admin", "platform_admin").get("/probe").status_code == 200

    def test_no_roles_is_refused(self) -> None:
        assert _app_with().get("/probe").status_code == 403

    @pytest.mark.parametrize("held", ["admin_ops", "lead-dispatcher", "dispatch"])
    def test_substring_neighbours_are_refused(self, held: str) -> None:
        # Mirrors the backend's Req 4.2: exact matching, never substring.
        assert _app_with(held).get("/probe").status_code == 403

    def test_rejection_does_not_echo_held_roles(self) -> None:
        response = _app_with("some-internal-role-name").get("/probe")
        assert response.status_code == 403
        assert "some-internal-role-name" not in response.text


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------


class TestNoComplianceRouterIsUngated:
    """Globbed from the package, so a ninth module cannot ship ungated."""

    @staticmethod
    def _endpoint_modules() -> list[Path]:
        return sorted(Path(compliance.api.__file__).parent.glob("*_endpoints.py"))

    def test_discovers_the_endpoint_modules(self) -> None:
        # Guards the guard: a glob matching nothing makes the next test vacuous.
        modules = self._endpoint_modules()
        assert len(modules) >= 8, [m.name for m in modules]

    def test_every_endpoint_module_applies_the_gate(self) -> None:
        ungated = [
            module.name
            for module in self._endpoint_modules()
            if "Depends(compliance_ops_dependency)"
            not in module.read_text(encoding="utf-8")
        ]
        assert ungated == [], (
            "compliance endpoint modules with no role gate: "
            f"{ungated}. Attach compliance_ops_dependency to the APIRouter."
        )
