"""
Unit tests for the driver extension of the Test_Auth_Path seam.

``tests/support/auth_seam.auth_headers`` now emits an ``X-Test-Driver-Id``
header and ``auth/test_auth.issue_test_context`` sets
``TenantContext.driver_id`` from it, so every driver-surface test can mint a
driver-scoped session without a live SuperTokens instance.

The seam is only useful if the fail-closed path stays exercised, so these tests
drive a minimal ``/api/driver`` route guarded by ``require_driver_identity`` and
assert the three outcomes the design names:

* driver role + driver header -> 200, and the handler receives the canonical id;
* driver role, no driver header -> 403 ``DRIVER_IDENTITY_MISSING``;
* no tenant header at all -> 401 (unauthenticated).

Validates: Requirements 1.5, 1.6
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from auth.authorization import require_driver_identity
from auth.test_auth import issue_test_context
from errors.handlers import register_exception_handlers
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from tests.support.auth_seam import DRIVER_HEADER, auth_headers, install_test_auth

TENANT_ID = "t1"
DRIVER_ID = "drv_1"


@pytest.fixture
def client() -> TestClient:
    """A one-route app whose handler is gated by ``require_driver_identity``."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/api/driver/whoami")
    async def whoami(tenant: TenantContext = Depends(get_tenant_context)) -> dict:
        driver_id = require_driver_identity(tenant)
        return {"driver_id": driver_id, "tenant_id": tenant.tenant_id}

    install_test_auth(app)
    return TestClient(app)


# ---------------------------------------------------------------------------
# issue_test_context carries the requested driver identity
# ---------------------------------------------------------------------------


class TestIssueTestContextDriverId:
    """``issue_test_context`` sets ``TenantContext.driver_id`` (Req 1.5, 1.6)."""

    def test_driver_id_is_carried_through(self):
        ctx = issue_test_context(TENANT_ID, roles=["driver"], driver_id=DRIVER_ID)
        assert ctx.driver_id == DRIVER_ID

    def test_omitted_driver_id_defaults_to_none(self):
        ctx = issue_test_context(TENANT_ID, roles=["driver"])
        assert ctx.driver_id is None

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_blank_driver_id_coerces_to_none(self, blank):
        """Mirrors the tenant guard: only a non-empty string is an identity."""
        ctx = issue_test_context(TENANT_ID, roles=["driver"], driver_id=blank)
        assert ctx.driver_id is None


# ---------------------------------------------------------------------------
# auth_headers emits the driver header only when asked
# ---------------------------------------------------------------------------


class TestAuthHeadersDriverHeader:
    """``auth_headers`` emits ``X-Test-Driver-Id`` on request (Req 1.5)."""

    def test_header_present_when_driver_id_given(self):
        headers = auth_headers(TENANT_ID, roles=["driver"], driver_id=DRIVER_ID)
        assert headers[DRIVER_HEADER] == DRIVER_ID

    def test_header_absent_when_driver_id_omitted(self):
        headers = auth_headers(TENANT_ID, roles=["driver"])
        assert DRIVER_HEADER not in headers


# ---------------------------------------------------------------------------
# End-to-end through the seam, including the fail-closed cases
# ---------------------------------------------------------------------------


class TestDriverSurfaceThroughSeam:
    """The seam reaches a driver surface and keeps it fail-closed."""

    def test_driver_scoped_request_succeeds(self, client):
        """Driver role + driver header reaches the handler (Req 1.5)."""
        response = client.get(
            "/api/driver/whoami",
            headers=auth_headers(TENANT_ID, roles=["driver"], driver_id=DRIVER_ID),
        )
        assert response.status_code == 200
        assert response.json() == {"driver_id": DRIVER_ID, "tenant_id": TENANT_ID}

    def test_missing_driver_header_is_rejected(self, client):
        """No driver identity -> 403 DRIVER_IDENTITY_MISSING (Req 1.6)."""
        response = client.get(
            "/api/driver/whoami",
            headers=auth_headers(TENANT_ID, roles=["driver"]),
        )
        assert response.status_code == 403
        assert response.json()["error_code"] == "DRIVER_IDENTITY_MISSING"

    def test_missing_driver_role_is_rejected(self, client):
        """A driver id without the driver role -> 403 INSUFFICIENT_ROLE (Req 1.5)."""
        response = client.get(
            "/api/driver/whoami",
            headers=auth_headers(TENANT_ID, roles=["dispatcher"], driver_id=DRIVER_ID),
        )
        assert response.status_code == 403
        assert response.json()["error_code"] == "INSUFFICIENT_ROLE"

    def test_missing_tenant_header_is_unauthenticated(self, client):
        """No Test_Auth_Path scope at all -> 401 (Req 1.5, 1.6)."""
        response = client.get("/api/driver/whoami")
        assert response.status_code == 401
