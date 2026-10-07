"""
Regression tests for the admin-only / production-forbidden gating on
``POST /api/data/cleanup`` and removal of the dangerous demo endpoints.

Before the security sprint both destructive handlers took only a bare
``TenantContext`` dependency, which meant any authenticated caller —
regardless of role — could wipe every tenant's fleet / inventory /
analytics in a single POST. They now require:

* A bound ``TenantContext`` (so unauthenticated callers never reach
  the handler at all).
* ``"platform_admin"`` in ``tenant.roles`` (so dispatcher, driver and
  tenant-admin JWTs are rejected with 403, OI-08).
* ``ALLOW_DATA_CLEANUP=true`` (default off, local dev only, OI-08).
* Non-production ``environment`` (so a misconfigured production
  deployment cannot trigger a wipe).

These tests override the tenant guard dependency directly to exercise
each gate without fiddling with JWT signing.

Validates: security sprint items 5 and 6.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# Stub out ES + data_seeder imports at module import time so bringing in
# the data_endpoints router doesn't trigger a real ES connection or
# accidentally execute the seeder against a live index.
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)


from config.settings import Environment  # noqa: E402


def _build_app(router_module, *, roles: list[str], tenant_id: str = "tenant-a"):
    """Build a minimal FastAPI app mounting the supplied router and
    overriding ``get_tenant_context`` to return a deterministic tenant
    with the requested role set."""
    from ops.middleware.tenant_guard import TenantContext, get_tenant_context
    from errors.exceptions import AppException
    from fastapi import Request
    from fastapi.responses import JSONResponse

    app = FastAPI()

    async def _override_tenant() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-a",
            has_pii_access=False,
            roles=list(roles),
        )

    app.include_router(router_module.router)
    app.dependency_overrides[get_tenant_context] = _override_tenant

    @app.exception_handler(AppException)
    async def _handler(request: Request, exc: AppException):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    return app


# ---------------------------------------------------------------------------
# /api/data/cleanup
# ---------------------------------------------------------------------------


class TestDataCleanupGating:
    """``POST /api/data/cleanup`` requires platform_admin and ALLOW_DATA_CLEANUP,
    and refuses in production (OI-08)."""

    def _patch_settings(self, environment: Environment, *, allow_data_cleanup: bool = True):
        """Patch the module-level ``settings`` used by the cleanup handler
        so its environment and flag checks see the requested values."""
        import data_endpoints

        stub_settings = MagicMock(
            environment=environment,
            allow_data_cleanup=allow_data_cleanup,
            rate_limit_requests_per_minute=1000,
        )
        return patch.object(data_endpoints, "settings", stub_settings)

    def _fake_seeder(self):
        fake_seeder = MagicMock()
        fake_seeder.clear_all_data = AsyncMock()
        fake_seeder.seed_all_data = AsyncMock()
        return fake_seeder

    def _post(self, roles, environment, *, allow_data_cleanup=True, seeder=None):
        import data_endpoints

        seeder = seeder or self._fake_seeder()
        with patch.dict(
            sys.modules,
            {"services.data_seeder": MagicMock(data_seeder=seeder)},
            clear=False,
        ), self._patch_settings(environment, allow_data_cleanup=allow_data_cleanup):
            app = _build_app(data_endpoints, roles=roles)
            with TestClient(app) as client:
                return client.post("/api/data/cleanup")

    def test_non_admin_returns_403(self):
        resp = self._post(["dispatcher"], Environment.DEVELOPMENT)

        assert resp.status_code == 403, resp.text
        body = resp.json()
        # The shared Role_Authorizer reports the requirement under
        # ``details.required_roles`` via the canonical ``INSUFFICIENT_ROLE``.
        details = body.get("details") or {}
        assert "platform_admin" in details.get("required_roles", [])

    def test_tenant_admin_returns_403_even_with_flag(self):
        seeder = self._fake_seeder()
        resp = self._post(["admin"], Environment.DEVELOPMENT, seeder=seeder)

        assert resp.status_code == 403, resp.text
        assert "platform_admin" in (resp.json().get("details") or {}).get(
            "required_roles", []
        )
        seeder.clear_all_data.assert_not_awaited()

    def test_platform_admin_without_flag_returns_403(self):
        seeder = self._fake_seeder()
        resp = self._post(
            ["platform_admin"], Environment.DEVELOPMENT,
            allow_data_cleanup=False, seeder=seeder,
        )

        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert "ALLOW_DATA_CLEANUP" in body.get("message", "")
        assert (body.get("details") or {}).get("flag") == "ALLOW_DATA_CLEANUP"
        seeder.clear_all_data.assert_not_awaited()

    def test_production_returns_403_even_with_flag(self):
        seeder = self._fake_seeder()
        resp = self._post(["platform_admin"], Environment.PRODUCTION, seeder=seeder)

        assert resp.status_code == 403, resp.text
        assert "production" in resp.json().get("message", "").lower()
        seeder.clear_all_data.assert_not_awaited()

    def test_platform_admin_with_flag_in_development_succeeds(self):
        seeder = self._fake_seeder()
        resp = self._post(["platform_admin"], Environment.DEVELOPMENT, seeder=seeder)

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True
        seeder.clear_all_data.assert_awaited_once()
        seeder.seed_all_data.assert_awaited_once_with(force=True)

    def test_flag_defaults_off(self):
        from config.settings import Settings

        assert Settings.model_fields["allow_data_cleanup"].default is False


# ---------------------------------------------------------------------------
# /api/demo/reset + /api/demo/status
# ---------------------------------------------------------------------------


class TestDemoEndpointsRemoved:
    """The destructive demo reset/status endpoints must not be mounted."""

    def test_demo_reset_is_not_registered(self):
        import inline_endpoints

        paths = {route.path for route in inline_endpoints.router.routes}
        assert "/api/demo/reset" not in paths

    def test_demo_status_is_not_registered(self):
        import inline_endpoints

        paths = {route.path for route in inline_endpoints.router.routes}
        assert "/api/demo/status" not in paths


# ---------------------------------------------------------------------------
# Legacy fake upload endpoints were removed entirely
# ---------------------------------------------------------------------------


class TestLegacyUploadHandlersRemoved:
    """``POST /api/data/upload/csv`` / ``/api/data/upload/sheets`` were
    demo stubs that returned ``random.randint`` as the record count and
    ignored the payload. They must no longer be mounted."""

    def test_legacy_upload_csv_is_not_registered(self):
        import data_endpoints

        paths = {route.path for route in data_endpoints.router.routes}
        assert "/api/data/upload/csv" not in paths, paths

    def test_legacy_upload_sheets_is_not_registered(self):
        import data_endpoints

        paths = {route.path for route in data_endpoints.router.routes}
        assert "/api/data/upload/sheets" not in paths, paths
