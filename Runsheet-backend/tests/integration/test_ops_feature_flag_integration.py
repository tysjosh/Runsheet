"""
Integration tests for feature flag gating across all surfaces.

Tests that disabling a tenant's feature flag correctly gates:
- Webhook processing (accept but skip)
- API endpoints (404 TENANT_DISABLED)
- WebSocket connections (close code 4403)
- AI tools (structured disabled response)
- Re-enabling restores all surfaces

Validates: Requirements 27.1-27.4
"""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Patch elasticsearch_service BEFORE any ops imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

# ---------------------------------------------------------------------------
# Patch strands BEFORE any Agents.tools imports (strands may not be installed)
# ---------------------------------------------------------------------------
if "strands" not in sys.modules:
    _mock_strands = MagicMock()
    _mock_strands.tool = lambda f=None, **kwargs: f if f else (lambda fn: fn)
    sys.modules["strands"] = _mock_strands

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from ops.api.endpoints import router as ops_router, configure_ops_api
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from ops.websocket.ops_ws import OpsWebSocketManager
from ops.services.ops_es_service import OpsElasticsearchService
from tests.fixtures import load_fixture

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def enable_legacy_ng_delivery(monkeypatch):
    """This module asserts *per-tenant* ops flag behaviour; the deployment-wide
    ``legacy_ng_delivery`` gate (default OFF) is enabled explicitly so the
    assertions still exercise the per-tenant path.

    Audit reference: product-owner-audit-2026-05-08 recommendation #1.
    """
    monkeypatch.setenv("LEGACY_NG_DELIVERY_ENABLED", "true")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WEBHOOK_SECRET = "ff-integration-secret"
TENANT_ID = "tenant-test-1"
DISABLED_TENANT = "tenant-disabled"


# ---------------------------------------------------------------------------
# Fake in-memory feature flag service
# ---------------------------------------------------------------------------

class FakeFeatureFlagService:
    """In-memory feature flag service for testing."""

    def __init__(self):
        self._flags: dict[str, bool] = {}

    async def is_enabled(self, tenant_id: str) -> bool:
        return self._flags.get(tenant_id, False)

    async def enable(self, tenant_id: str, user_id: str) -> None:
        self._flags[tenant_id] = True

    async def disable(self, tenant_id: str, user_id: str) -> None:
        self._flags[tenant_id] = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ===========================================================================
# 23.4 — Feature flag integration tests
# ===========================================================================


# NB: TestWebhookFeatureFlagGating stood here. It asserted that a disabled
# tenant's webhook was accepted but not processed. That gate lived inside the
# ``POST /webhooks/dinee`` handler, which has been removed with its module; the
# surviving ops ingestion entry point (``ReplayService._process_record``) does
# not consult the feature flag service, so the behaviour no longer exists to
# assert. Flag gating remains covered below for the API, WebSocket and AI-tool
# surfaces, which do still enforce it.


class TestApiFeatureFlagGating:
    """
    Test API 404 for disabled tenant.

    When a tenant is disabled, all /ops/* endpoints should return 404
    with TENANT_DISABLED error code.

    Validates: Requirement 27.3
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.ff_service = FakeFeatureFlagService()

        app = FastAPI()

        mock_es_client = MagicMock()
        mock_es_client.search = MagicMock(return_value={
            "hits": {"hits": [], "total": {"value": 0}},
        })
        mock_ops_es = MagicMock(spec=OpsElasticsearchService)
        mock_ops_es.client = mock_es_client

        configure_ops_api(
            ops_es_service=mock_ops_es,
            feature_flag_service=self.ff_service,
        )

        async def _override_tenant():
            return TenantContext(
                tenant_id=DISABLED_TENANT, user_id="user-1", has_pii_access=False
            )

        app.dependency_overrides[get_tenant_context] = _override_tenant

        class FakeRequestID(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.request_id = "req-ff-test"
                return await call_next(request)

        app.add_middleware(FakeRequestID)
        app.include_router(ops_router)
        self.client = TestClient(app)

    def test_disabled_tenant_shipments_returns_404(self):
        resp = self.client.get("/api/ops/shipments")
        assert resp.status_code == 404
        body = resp.json()
        assert body["detail"]["error_code"] == "TENANT_DISABLED"

    def test_disabled_tenant_riders_returns_404(self):
        resp = self.client.get("/api/ops/riders")
        assert resp.status_code == 404

    def test_disabled_tenant_events_returns_404(self):
        resp = self.client.get("/api/ops/events")
        assert resp.status_code == 404

    def test_disabled_tenant_single_shipment_returns_404(self):
        resp = self.client.get("/api/ops/shipments/SHP-001")
        assert resp.status_code == 404

    def test_disabled_tenant_sla_breaches_returns_404(self):
        resp = self.client.get("/api/ops/shipments/sla-breaches")
        assert resp.status_code == 404

    def test_disabled_tenant_failures_returns_404(self):
        resp = self.client.get("/api/ops/shipments/failures")
        assert resp.status_code == 404

    def test_enabled_tenant_shipments_returns_200(self):
        """After enabling, endpoints return 200."""
        self.ff_service._flags[DISABLED_TENANT] = True
        resp = self.client.get("/api/ops/shipments")
        assert resp.status_code == 200


class TestWebSocketFeatureFlagGating:
    """
    Test WebSocket rejection (close code 4403) for disabled tenant.

    Validates: Requirement 27.3
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.ff_service = FakeFeatureFlagService()
        self.manager = OpsWebSocketManager()
        self.manager.set_feature_flag_service(self.ff_service)

    @pytest.mark.asyncio
    async def test_disabled_tenant_connection_rejected_with_4403(self):
        """Disabled tenant gets close code 4403 immediately after accept."""
        from tests.integration.test_ops_websocket_integration import FakeWebSocket

        ws = FakeWebSocket()
        await self.manager.connect(ws, tenant_id=DISABLED_TENANT)

        assert ws.accepted  # accept() is called first
        assert ws.closed
        assert ws.close_code == 4403
        assert ws.close_reason == "tenant_disabled"
        # Client should NOT be registered
        assert self.manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_enabled_tenant_connection_accepted(self):
        from tests.integration.test_ops_websocket_integration import FakeWebSocket

        self.ff_service._flags["tenant-ok"] = True
        ws = FakeWebSocket()
        await self.manager.connect(ws, tenant_id="tenant-ok")

        assert ws.accepted
        assert not ws.closed
        assert self.manager.get_connection_count() == 1

    @pytest.mark.asyncio
    async def test_disconnect_tenant_closes_existing_connections(self):
        """disconnect_tenant() closes all connections for a specific tenant."""
        from tests.integration.test_ops_websocket_integration import FakeWebSocket

        self.ff_service._flags["tenant-x"] = True
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()
        await self.manager.connect(ws1, tenant_id="tenant-x")
        await self.manager.connect(ws2, tenant_id="tenant-x")
        assert self.manager.get_connection_count() == 2

        count = await self.manager.disconnect_tenant("tenant-x")
        assert count == 2
        assert ws1.closed
        assert ws1.close_code == 4403
        assert ws2.closed
        assert self.manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_re_enable_allows_new_connections(self):
        """After re-enabling, new connections are accepted."""
        from tests.integration.test_ops_websocket_integration import FakeWebSocket

        # Disabled → rejected
        ws1 = FakeWebSocket()
        await self.manager.connect(ws1, tenant_id="tenant-re")
        assert ws1.closed

        # Enable
        self.ff_service._flags["tenant-re"] = True

        # Now accepted
        ws2 = FakeWebSocket()
        await self.manager.connect(ws2, tenant_id="tenant-re")
        assert not ws2.closed
        assert self.manager.get_connection_count() == 1


class TestAiToolsFeatureFlagGating:
    """
    Test AI tools return disabled response for disabled tenant.

    Validates: Requirement 27.3
    """

    @pytest.mark.asyncio
    async def test_disabled_tenant_returns_disabled_response(self):
        """check_ops_feature_flag returns disabled JSON for disabled tenant."""
        ff_service = FakeFeatureFlagService()

        from Agents.tools.ops_feature_guard import (
            check_ops_feature_flag,
            configure_ops_feature_guard,
            DISABLED_RESPONSE,
        )

        configure_ops_feature_guard(ff_service)

        result = await check_ops_feature_flag("tenant-disabled")
        assert result is not None
        parsed = json.loads(result)
        assert parsed["status"] == "disabled"
        assert "not enabled" in parsed["message"]

    @pytest.mark.asyncio
    async def test_enabled_tenant_returns_none(self):
        """check_ops_feature_flag returns None for enabled tenant (proceed)."""
        ff_service = FakeFeatureFlagService()
        ff_service._flags["tenant-enabled"] = True

        from Agents.tools.ops_feature_guard import (
            check_ops_feature_flag,
            configure_ops_feature_guard,
        )

        configure_ops_feature_guard(ff_service)

        result = await check_ops_feature_flag("tenant-enabled")
        assert result is None

    @pytest.mark.asyncio
    async def test_re_enable_restores_ai_tools(self):
        """After re-enabling, AI tools return None (proceed)."""
        ff_service = FakeFeatureFlagService()

        from Agents.tools.ops_feature_guard import (
            check_ops_feature_flag,
            configure_ops_feature_guard,
        )

        configure_ops_feature_guard(ff_service)

        # Disabled
        result1 = await check_ops_feature_flag("tenant-toggle")
        assert result1 is not None

        # Enable
        ff_service._flags["tenant-toggle"] = True

        # Now enabled
        result2 = await check_ops_feature_flag("tenant-toggle")
        assert result2 is None


class TestFeatureFlagReEnableRestoresAllSurfaces:
    """
    End-to-end test: disable → verify all surfaces gated → re-enable → verify restored.

    Validates: Requirements 27.1-27.4
    """

    @pytest.mark.asyncio
    async def test_full_disable_enable_cycle(self):
        from tests.integration.test_ops_websocket_integration import FakeWebSocket
        from Agents.tools.ops_feature_guard import (
            check_ops_feature_flag,
            configure_ops_feature_guard,
        )

        ff_service = FakeFeatureFlagService()
        tenant = "tenant-cycle"

        # --- Set up WebSocket manager ---
        ws_manager = OpsWebSocketManager()
        ws_manager.set_feature_flag_service(ff_service)

        # --- Set up AI tools guard ---
        configure_ops_feature_guard(ff_service)

        # === DISABLED STATE ===

        # WebSocket: rejected
        ws_disabled = FakeWebSocket()
        await ws_manager.connect(ws_disabled, tenant_id=tenant)
        assert ws_disabled.closed
        assert ws_disabled.close_code == 4403

        # AI tools: disabled response
        ai_result = await check_ops_feature_flag(tenant)
        assert ai_result is not None
        assert json.loads(ai_result)["status"] == "disabled"

        # === ENABLE ===
        ff_service._flags[tenant] = True

        # WebSocket: accepted
        ws_enabled = FakeWebSocket()
        await ws_manager.connect(ws_enabled, tenant_id=tenant)
        assert not ws_enabled.closed
        assert ws_manager.get_connection_count() == 1

        # AI tools: proceed
        ai_result2 = await check_ops_feature_flag(tenant)
        assert ai_result2 is None
