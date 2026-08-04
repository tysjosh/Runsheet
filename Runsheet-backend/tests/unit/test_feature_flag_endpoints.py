"""
Unit tests for feature flag management endpoints in ops/api/endpoints.py.

Tests cover:
- POST /ops/admin/feature-flags/{tenant_id}/enable
- POST /ops/admin/feature-flags/{tenant_id}/disable
- POST /ops/admin/feature-flags/{tenant_id}/rollback
- WebSocket disconnect on disable/rollback
- Service not configured returns 503
- Cross-tenant authorization (see note below)

Authorization note
------------------

These endpoints take the target tenant as a **path parameter**. Until the
tenant-scope guard was added they had no authorization at all beyond "is
authenticated", and this test module was itself demonstrating the hole: the
caller was ``tenant-1`` with an empty ``roles`` list, POSTing to
``/feature-flags/tenant-abc/enable`` — a different tenant — and asserting
success.

The happy-path callers below now hold ``platform_admin``, which is the role that
legitimately permits acting on another tenant, so the tests keep exercising
"the path parameter is honoured" while being authorized. The denial cases at the
bottom pin the attacks that used to succeed.

Validates: Requirements 27.1, 27.5
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Patch ES module before ops imports
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from ops.api.endpoints import router, configure_ops_api
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from ops.services.ops_es_service import OpsElasticsearchService


def _make_tenant(
    tenant_id: str = "tenant-1",
    roles: list[str] | None = None,
) -> TenantContext:
    """Caller context. Defaults to Runsheet staff.

    ``platform_admin`` is the default because every happy-path test here targets
    ``tenant-abc`` from a caller homed in ``tenant-1``; that is a cross-tenant
    action and only staff may do it.
    """
    return TenantContext(
        tenant_id=tenant_id,
        user_id="user-1",
        has_pii_access=False,
        roles=list(roles) if roles is not None else ["platform_admin"],
    )


@pytest.fixture()
def mock_ff_service():
    svc = AsyncMock()
    svc.enable = AsyncMock()
    svc.disable = AsyncMock()
    svc.rollback = AsyncMock()
    return svc


@pytest.fixture()
def mock_ws_manager():
    mgr = AsyncMock()
    mgr.disconnect_tenant = AsyncMock(return_value=2)
    return mgr


@pytest.fixture()
def app(mock_ff_service, mock_ws_manager):
    test_app = FastAPI()

    mock_ops_es = MagicMock(spec=OpsElasticsearchService)
    mock_ops_es.client = MagicMock()

    configure_ops_api(ops_es_service=mock_ops_es, feature_flag_service=mock_ff_service)

    async def _override_tenant():
        return _make_tenant()

    test_app.dependency_overrides[get_tenant_context] = _override_tenant

    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = "req-test-456"
            return await call_next(request)

    test_app.add_middleware(FakeRequestID)
    test_app.include_router(router)
    return test_app


@pytest.fixture()
def client(app):
    return TestClient(app)


class TestEnableFeatureFlag:
    def test_enable_returns_success(self, client, mock_ff_service):
        resp = client.post("/api/ops/admin/feature-flags/tenant-abc/enable")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["tenant_id"] == "tenant-abc"
        assert body["data"]["status"] == "enabled"
        assert "request_id" in body

    def test_enable_calls_service(self, client, mock_ff_service):
        client.post("/api/ops/admin/feature-flags/tenant-abc/enable")
        mock_ff_service.enable.assert_awaited_once_with("tenant-abc", "user-1")


class TestDisableFeatureFlag:
    @patch("ops.websocket.ops_ws.get_ops_ws_manager")
    def test_disable_returns_success(self, mock_get_ws, client, mock_ff_service, mock_ws_manager):
        mock_get_ws.return_value = mock_ws_manager
        resp = client.post("/api/ops/admin/feature-flags/tenant-abc/disable")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["tenant_id"] == "tenant-abc"
        assert body["data"]["status"] == "disabled"
        assert body["data"]["ws_clients_disconnected"] == 2

    @patch("ops.websocket.ops_ws.get_ops_ws_manager")
    def test_disable_calls_service_and_ws(self, mock_get_ws, client, mock_ff_service, mock_ws_manager):
        mock_get_ws.return_value = mock_ws_manager
        client.post("/api/ops/admin/feature-flags/tenant-abc/disable")
        mock_ff_service.disable.assert_awaited_once_with("tenant-abc", "user-1")
        mock_ws_manager.disconnect_tenant.assert_awaited_once_with("tenant-abc")


class TestRollbackFeatureFlag:
    @patch("ops.websocket.ops_ws.get_ops_ws_manager")
    def test_rollback_default_no_purge(self, mock_get_ws, client, mock_ff_service, mock_ws_manager):
        mock_get_ws.return_value = mock_ws_manager
        resp = client.post("/api/ops/admin/feature-flags/tenant-abc/rollback")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["tenant_id"] == "tenant-abc"
        assert body["data"]["status"] == "rolled_back"
        assert body["data"]["purge_data"] is False
        mock_ff_service.rollback.assert_awaited_once_with("tenant-abc", "user-1", purge_data=False)

    @patch("ops.websocket.ops_ws.get_ops_ws_manager")
    def test_rollback_with_purge(self, mock_get_ws, client, mock_ff_service, mock_ws_manager):
        mock_get_ws.return_value = mock_ws_manager
        resp = client.post("/api/ops/admin/feature-flags/tenant-abc/rollback?purge_data=true")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["purge_data"] is True
        mock_ff_service.rollback.assert_awaited_once_with("tenant-abc", "user-1", purge_data=True)

    @patch("ops.websocket.ops_ws.get_ops_ws_manager")
    def test_rollback_disconnects_ws(self, mock_get_ws, client, mock_ff_service, mock_ws_manager):
        mock_get_ws.return_value = mock_ws_manager
        resp = client.post("/api/ops/admin/feature-flags/tenant-abc/rollback")
        body = resp.json()
        assert body["data"]["ws_clients_disconnected"] == 2
        mock_ws_manager.disconnect_tenant.assert_awaited_once_with("tenant-abc")


class TestServiceNotConfigured:
    def test_enable_503_when_no_service(self):
        test_app = FastAPI()
        configure_ops_api(
            ops_es_service=MagicMock(spec=OpsElasticsearchService),
            feature_flag_service=None,
        )

        async def _override_tenant():
            return _make_tenant()

        test_app.dependency_overrides[get_tenant_context] = _override_tenant

        from starlette.middleware.base import BaseHTTPMiddleware

        class FakeRequestID(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.request_id = "req-test"
                return await call_next(request)

        test_app.add_middleware(FakeRequestID)
        test_app.include_router(router)
        c = TestClient(test_app)

        resp = c.post("/api/ops/admin/feature-flags/tenant-abc/enable")
        assert resp.status_code == 503


class TestCrossTenantAuthorization:
    """Requests that used to succeed and must now be refused.

    Each of these was reachable before the tenant-scope guard existed. They are
    expressed at the HTTP layer, rather than against the guard helper, because
    the defect was that the handler never called the helper at all — a unit test
    of the helper alone would have passed throughout.
    """

    def _client(self, mock_ff_service, mock_ws_manager, caller: TenantContext):
        test_app = FastAPI()
        mock_ops_es = MagicMock(spec=OpsElasticsearchService)
        mock_ops_es.client = MagicMock()
        configure_ops_api(
            ops_es_service=mock_ops_es, feature_flag_service=mock_ff_service
        )

        async def _override_tenant():
            return caller

        test_app.dependency_overrides[get_tenant_context] = _override_tenant

        from starlette.middleware.base import BaseHTTPMiddleware

        class FakeRequestID(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.request_id = "req-authz"
                return await call_next(request)

        test_app.add_middleware(FakeRequestID)
        # Without these, an AppException surfaces as a bare 500 and the test
        # would pass for the wrong reason — "the request failed" rather than
        # "the request was refused with the right status".
        from errors.handlers import register_exception_handlers

        register_exception_handlers(test_app)
        test_app.include_router(router)
        return TestClient(test_app, raise_server_exceptions=False)

    @pytest.mark.parametrize("action", ["enable", "disable", "rollback"])
    def test_driver_cannot_touch_any_tenant_flag(
        self, mock_ff_service, mock_ws_manager, action
    ):
        """A driver could previously disable a whole tenant's ops layer."""
        caller = _make_tenant(tenant_id="tenant-1", roles=["driver"])
        client = self._client(mock_ff_service, mock_ws_manager, caller)

        resp = client.post(f"/api/ops/admin/feature-flags/tenant-1/{action}")

        assert resp.status_code in (401, 403), resp.text
        mock_ff_service.enable.assert_not_awaited()
        mock_ff_service.disable.assert_not_awaited()
        mock_ff_service.rollback.assert_not_awaited()

    @pytest.mark.parametrize("action", ["enable", "disable", "rollback"])
    def test_tenant_admin_cannot_touch_another_tenants_flag(
        self, mock_ff_service, mock_ws_manager, action
    ):
        """The core cross-tenant case: right role, wrong company."""
        caller = _make_tenant(tenant_id="acme", roles=["admin"])
        client = self._client(mock_ff_service, mock_ws_manager, caller)

        resp = client.post(f"/api/ops/admin/feature-flags/globex/{action}")

        assert resp.status_code in (401, 403), resp.text
        mock_ff_service.enable.assert_not_awaited()
        mock_ff_service.disable.assert_not_awaited()
        mock_ff_service.rollback.assert_not_awaited()

    def test_tenant_admin_cannot_purge_even_own_tenant(
        self, mock_ff_service, mock_ws_manager
    ):
        """``purge_data=true`` deletes ops history, so it is staff-only.

        A customer administrator may disable their own layer but must not be
        able to destroy the audit trail behind it.
        """
        caller = _make_tenant(tenant_id="acme", roles=["admin"])
        client = self._client(mock_ff_service, mock_ws_manager, caller)

        resp = client.post(
            "/api/ops/admin/feature-flags/acme/rollback?purge_data=true"
        )

        assert resp.status_code in (401, 403), resp.text
        mock_ff_service.rollback.assert_not_awaited()

    def test_tenant_admin_may_rollback_own_tenant_without_purge(
        self, mock_ff_service, mock_ws_manager
    ):
        """The counterweight: don't over-tighten into "deny everything"."""
        caller = _make_tenant(tenant_id="acme", roles=["admin"])
        client = self._client(mock_ff_service, mock_ws_manager, caller)

        resp = client.post("/api/ops/admin/feature-flags/acme/rollback")

        assert resp.status_code == 200, resp.text
        mock_ff_service.rollback.assert_awaited_once()

    def test_platform_admin_may_purge_cross_tenant(
        self, mock_ff_service, mock_ws_manager
    ):
        """Staff retain the destructive path they need for offboarding."""
        caller = _make_tenant(tenant_id="runsheet", roles=["platform_admin"])
        client = self._client(mock_ff_service, mock_ws_manager, caller)

        resp = client.post(
            "/api/ops/admin/feature-flags/globex/rollback?purge_data=true"
        )

        assert resp.status_code == 200, resp.text
        mock_ff_service.rollback.assert_awaited_once()
