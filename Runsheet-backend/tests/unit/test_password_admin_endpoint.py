"""
Tests for provisioned-user password administration (OQ6 follow-up).

Covers the two layers that close the "no way to set a provisioned user's
password" gap:

* :mod:`auth.api.password_admin_endpoints` — the admin-gated
  ``POST /api/auth/admin/password-reset-link`` route: requires the ``admin``
  role (403 otherwise), maps service errors to the right status, and returns
  the minted link on success.
* :mod:`auth.password_admin` error mapping — a ``not_provisioned`` failure
  surfaces as 404, an invalid email as 422.

The ``auth.password_admin.create_password_set_link`` collaborator is patched so
the tests never touch the SuperTokens SDK or a database; the focus is the
endpoint's auth gate and error contract.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from auth.api.password_admin_endpoints import router
from auth.password_admin import PasswordAdminError, PasswordSetLink
from errors.exceptions import AppException
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


def _ctx(roles):
    return TenantContext(
        tenant_id="demo-tenant",
        user_id="user-1",
        has_pii_access=True,
        roles=list(roles),
    )


def _build_app(roles) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppException)
    async def _handler(_request: Request, exc: AppException):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    app.dependency_overrides[get_tenant_context] = lambda: _ctx(roles)
    return app


class TestPasswordResetLinkEndpoint:
    def test_admin_gets_link(self):
        app = _build_app(["admin"])
        client = TestClient(app)

        with patch(
            "auth.api.password_admin_endpoints.create_password_set_link",
            new=AsyncMock(
                return_value=PasswordSetLink(
                    email="admin@runsheet.com",
                    st_user_id="st-1",
                    link="http://localhost:3000/auth/reset-password?token=abc&tenantId=public",
                )
            ),
        ):
            resp = client.post(
                "/api/auth/admin/password-reset-link",
                json={"email": "admin@runsheet.com"},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["email"] == "admin@runsheet.com"
        assert "token=abc" in body["link"]

    def test_non_admin_is_forbidden(self):
        app = _build_app(["dispatcher"])
        client = TestClient(app)

        with patch(
            "auth.api.password_admin_endpoints.create_password_set_link",
            new=AsyncMock(),
        ) as mock_create:
            resp = client.post(
                "/api/auth/admin/password-reset-link",
                json={"email": "admin@runsheet.com"},
            )

        assert resp.status_code == 403
        # The service must never be called when the caller lacks the role.
        mock_create.assert_not_called()

    def test_not_provisioned_returns_404(self):
        app = _build_app(["admin"])
        client = TestClient(app)

        with patch(
            "auth.api.password_admin_endpoints.create_password_set_link",
            new=AsyncMock(
                side_effect=PasswordAdminError(
                    "not_provisioned", "No provisioned auth_users record"
                )
            ),
        ):
            resp = client.post(
                "/api/auth/admin/password-reset-link",
                json={"email": "ghost@runsheet.com"},
            )

        assert resp.status_code == 404
        assert resp.json()["details"]["reason"] == "not_provisioned"

    def test_malformed_email_rejected_by_model(self):
        app = _build_app(["admin"])
        client = TestClient(app)

        resp = client.post(
            "/api/auth/admin/password-reset-link",
            json={"email": "not-an-email"},
        )

        # Pydantic model validation rejects before the handler body runs.
        assert resp.status_code == 422

    def test_extra_fields_forbidden(self):
        app = _build_app(["admin"])
        client = TestClient(app)

        resp = client.post(
            "/api/auth/admin/password-reset-link",
            json={"email": "admin@runsheet.com", "password": "sneaky"},
        )

        assert resp.status_code == 422


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
