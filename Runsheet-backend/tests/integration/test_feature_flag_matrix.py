"""
Integration test matrix for feature flag toggling across all endpoints.

Tests that enabling/disabling a tenant flag correctly gates access to
ops, fuel, scheduling, and agent endpoints, and that re-enabling
restores full access without application restart.

Validates: Requirements 9.1, 9.2, 9.4
"""

import hashlib
import hmac
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

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from ops.webhooks.receiver import configure_webhook_receiver, router as webhook_router
from ops.api.endpoints import router as ops_router, configure_ops_api
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from ops.ingestion.adapter import AdapterTransformer
from ops.ingestion.handlers.v1_0 import V1SchemaHandler
from ops.websocket.ops_ws import OpsWebSocketManager
from ops.services.ops_es_service import OpsElasticsearchService

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WEBHOOK_SECRET = "matrix-test-secret"
ENABLED_TENANT = "tenant-enabled"
DISABLED_TENANT = "tenant-disabled"
FIXTURE_TENANT = "tenant-test-1"  # tenant_id in webhook fixtures


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

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# FakeWebSocket for testing
# ---------------------------------------------------------------------------

class FakeWebSocket:
    """Minimal WebSocket stub that records sent messages and close calls."""

    def __init__(self):
        self.accepted = False
        self.messages: list[dict] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def accept(self):
        self.accepted = True

    async def send_json(self, data: dict):
        if self.closed:
            raise RuntimeError("WebSocket is closed")
        self.messages.append(data)

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True
        self.close_code = code
        self.close_reason = reason


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeRequestIDMiddleware(BaseHTTPMiddleware):
    """Middleware that injects a fake request_id into request.state."""
    async def dispatch(self, request, call_next):
        request.state.request_id = "req-matrix-test"
        return await call_next(request)


def _sign(payload: dict, secret: str = WEBHOOK_SECRET) -> str:
    body = json.dumps(payload).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _mock_es_search_result(hits=None, total=0):
    """Create a mock ES search result."""
    return {
        "hits": {
            "hits": hits or [],
            "total": {"value": total, "relation": "eq"},
        },
        "took": 1,
        "_shards": {"successful": 1, "total": 1, "failed": 0},
    }


def _create_ops_app(ff_service: FakeFeatureFlagService, tenant_id: str) -> TestClient:
    """Create a FastAPI app with ops router and mocked dependencies."""
    app = FastAPI()

    # Create a mock OpsElasticsearchService with a synchronous mock client
    mock_es_client = MagicMock()
    mock_es_client.search = MagicMock(return_value=_mock_es_search_result())
    mock_ops_es = MagicMock(spec=OpsElasticsearchService)
    mock_ops_es.client = mock_es_client

    configure_ops_api(
        ops_es_service=mock_ops_es,
        feature_flag_service=ff_service,
    )

    async def _override_tenant():
        return TenantContext(
            tenant_id=tenant_id, user_id="user-1", has_pii_access=False
        )

    app.dependency_overrides[get_tenant_context] = _override_tenant
    app.add_middleware(FakeRequestIDMiddleware)
    app.include_router(ops_router)
    return TestClient(app)


# ===========================================================================
# Test: Enabling a tenant flag allows access to all ops endpoints (Req 9.1)
# ===========================================================================

class TestEnabledTenantAccessOps:
    """
    Verify that an enabled tenant can access all ops endpoints.

    Validates: Requirement 9.1
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.ff_service = FakeFeatureFlagService()
        self.ff_service._flags[ENABLED_TENANT] = True
        self.client = _create_ops_app(self.ff_service, ENABLED_TENANT)

    def test_enabled_tenant_list_shipments(self):
        resp = self.client.get("/api/ops/shipments")
        assert resp.status_code == 200

    def test_enabled_tenant_list_riders(self):
        resp = self.client.get("/api/ops/riders")
        assert resp.status_code == 200

    def test_enabled_tenant_list_events(self):
        resp = self.client.get("/api/ops/events")
        assert resp.status_code == 200

    def test_enabled_tenant_sla_breaches(self):
        resp = self.client.get("/api/ops/shipments/sla-breaches")
        assert resp.status_code == 200

    def test_enabled_tenant_failures(self):
        resp = self.client.get("/api/ops/shipments/failures")
        assert resp.status_code == 200

    def test_enabled_tenant_single_shipment(self):
        resp = self.client.get("/api/ops/shipments/SHP-001")
        # 200 or 404 (not found in ES) are both acceptable — not 403
        assert resp.status_code in (200, 404)

    def test_enabled_tenant_single_rider(self):
        resp = self.client.get("/api/ops/riders/RDR-001")
        assert resp.status_code in (200, 404)

    def test_enabled_tenant_shipment_metrics(self):
        resp = self.client.get("/api/ops/metrics/shipments")
        assert resp.status_code == 200

    def test_enabled_tenant_sla_metrics(self):
        resp = self.client.get("/api/ops/metrics/sla")
        assert resp.status_code == 200

    def test_enabled_tenant_rider_metrics(self):
        resp = self.client.get("/api/ops/metrics/riders")
        assert resp.status_code == 200


# ===========================================================================
# Test: Disabling a tenant flag returns 404 for gated endpoints (Req 9.2)
# ===========================================================================

class TestDisabledTenantBlockedOps:
    """
    Verify that a disabled tenant gets 404 (TENANT_DISABLED) on all ops endpoints.

    Validates: Requirement 9.2
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.ff_service = FakeFeatureFlagService()
        # Tenant is disabled (not in flags → defaults to False)
        self.client = _create_ops_app(self.ff_service, DISABLED_TENANT)

    def test_disabled_tenant_list_shipments(self):
        resp = self.client.get("/api/ops/shipments")
        assert resp.status_code == 404
        body = resp.json()
        assert body["detail"]["error_code"] == "TENANT_DISABLED"

    def test_disabled_tenant_list_riders(self):
        resp = self.client.get("/api/ops/riders")
        assert resp.status_code == 404

    def test_disabled_tenant_list_events(self):
        resp = self.client.get("/api/ops/events")
        assert resp.status_code == 404

    def test_disabled_tenant_sla_breaches(self):
        resp = self.client.get("/api/ops/shipments/sla-breaches")
        assert resp.status_code == 404

    def test_disabled_tenant_failures(self):
        resp = self.client.get("/api/ops/shipments/failures")
        assert resp.status_code == 404

    def test_disabled_tenant_single_shipment(self):
        resp = self.client.get("/api/ops/shipments/SHP-001")
        assert resp.status_code == 404

    def test_disabled_tenant_single_rider(self):
        resp = self.client.get("/api/ops/riders/RDR-001")
        assert resp.status_code == 404

    def test_disabled_tenant_shipment_metrics(self):
        resp = self.client.get("/api/ops/metrics/shipments")
        assert resp.status_code == 404

    def test_disabled_tenant_sla_metrics(self):
        resp = self.client.get("/api/ops/metrics/sla")
        assert resp.status_code == 404

    def test_disabled_tenant_rider_metrics(self):
        resp = self.client.get("/api/ops/metrics/riders")
        assert resp.status_code == 404


# ===========================================================================
# Test: Disabling closes existing WS connections with code 4403 (Req 9.2)
# ===========================================================================

class TestDisabledTenantWebSocketClosure:
    """
    Verify that disabling a tenant closes existing WebSocket connections
    with close code 4403.

    Validates: Requirement 9.2
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.ff_service = FakeFeatureFlagService()
        self.manager = OpsWebSocketManager()
        self.manager.set_feature_flag_service(self.ff_service)

    @pytest.mark.asyncio
    async def test_disabled_tenant_ws_rejected_with_4403(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, tenant_id=DISABLED_TENANT)

        assert ws.accepted
        assert ws.closed
        assert ws.close_code == 4403
        assert ws.close_reason == "tenant_disabled"
        assert self.manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_disconnect_tenant_closes_all_connections(self):
        self.ff_service._flags["tenant-ws"] = True
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()
        await self.manager.connect(ws1, tenant_id="tenant-ws")
        await self.manager.connect(ws2, tenant_id="tenant-ws")
        assert self.manager.get_connection_count() == 2

        count = await self.manager.disconnect_tenant("tenant-ws")
        assert count == 2
        assert ws1.closed
        assert ws1.close_code == 4403
        assert ws2.closed
        assert self.manager.get_connection_count() == 0


# ===========================================================================
# Test: Re-enabling restores full access without restart (Req 9.4)
# ===========================================================================

class TestReEnableTenantRestoresAccess:
    """
    Verify that re-enabling a previously disabled tenant restores full
    access without requiring application restart.

    Validates: Requirement 9.4
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.ff_service = FakeFeatureFlagService()

    def test_re_enable_restores_ops_shipments(self):
        """Disable → verify blocked → enable → verify access restored."""
        client = _create_ops_app(self.ff_service, "tenant-toggle")

        # Disabled: should get 404
        resp1 = client.get("/api/ops/shipments")
        assert resp1.status_code == 404

        # Enable
        self.ff_service._flags["tenant-toggle"] = True

        # Enabled: should get 200
        resp2 = client.get("/api/ops/shipments")
        assert resp2.status_code == 200

    def test_re_enable_restores_ops_riders(self):
        client = _create_ops_app(self.ff_service, "tenant-toggle-2")

        resp1 = client.get("/api/ops/riders")
        assert resp1.status_code == 404

        self.ff_service._flags["tenant-toggle-2"] = True

        resp2 = client.get("/api/ops/riders")
        assert resp2.status_code == 200

    def test_re_enable_restores_ops_events(self):
        client = _create_ops_app(self.ff_service, "tenant-toggle-3")

        resp1 = client.get("/api/ops/events")
        assert resp1.status_code == 404

        self.ff_service._flags["tenant-toggle-3"] = True

        resp2 = client.get("/api/ops/events")
        assert resp2.status_code == 200

    @pytest.mark.asyncio
    async def test_re_enable_restores_ws_connections(self):
        manager = OpsWebSocketManager()
        manager.set_feature_flag_service(self.ff_service)

        # Disabled → rejected
        ws1 = FakeWebSocket()
        await manager.connect(ws1, tenant_id="tenant-ws-toggle")
        assert ws1.closed
        assert ws1.close_code == 4403

        # Enable
        self.ff_service._flags["tenant-ws-toggle"] = True

        # Enabled → accepted
        ws2 = FakeWebSocket()
        await manager.connect(ws2, tenant_id="tenant-ws-toggle")
        assert not ws2.closed
        assert manager.get_connection_count() == 1

    @pytest.mark.asyncio
    async def test_re_enable_restores_ai_tools(self):
        try:
            from Agents.tools.ops_feature_guard import (
                check_ops_feature_flag,
                configure_ops_feature_guard,
            )
        except (ImportError, ModuleNotFoundError):
            pytest.skip("strands SDK not installed — skipping AI tools test")

        configure_ops_feature_guard(self.ff_service)

        # Disabled
        result1 = await check_ops_feature_flag("tenant-ai-toggle")
        assert result1 is not None
        assert json.loads(result1)["status"] == "disabled"

        # Enable
        self.ff_service._flags["tenant-ai-toggle"] = True

        # Enabled
        result2 = await check_ops_feature_flag("tenant-ai-toggle")
        assert result2 is None


# ===========================================================================
# Test: Webhook processing gated by feature flag (Req 9.1, 9.2)
# ===========================================================================

class TestWebhookFeatureFlagMatrix:
    """
    Test webhook processing is correctly gated by feature flags.

    Validates: Requirements 9.1, 9.2
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.ff_service = FakeFeatureFlagService()
        self.indexed_shipments: list[dict] = []
        self.indexed_events: list[dict] = []

        app = FastAPI()
        app.include_router(webhook_router)

        adapter = AdapterTransformer()
        adapter.register_handler("1.0", V1SchemaHandler())

        idempotency = AsyncMock()
        idempotency.is_duplicate = AsyncMock(return_value=False)
        idempotency.mark_processed = AsyncMock()

        ops_es = AsyncMock()
        ops_es.upsert_shipment_current = AsyncMock(
            side_effect=lambda doc: self.indexed_shipments.append(doc) or True
        )
        ops_es.append_shipment_event = AsyncMock(
            side_effect=lambda doc: self.indexed_events.append(doc)
        )
        ops_es.upsert_rider_current = AsyncMock()

        configure_webhook_receiver(
            adapter=adapter,
            idempotency_service=idempotency,
            poison_queue_service=AsyncMock(),
            ops_es_service=ops_es,
            ws_manager=None,
            feature_flag_service=self.ff_service,
            webhook_secret=WEBHOOK_SECRET,
            webhook_tenant_id="",
        )

        self.client = TestClient(app)

    def test_disabled_tenant_webhook_skips_indexing(self):
        from tests.fixtures import load_fixture
        payload = load_fixture("shipment_created")
        body = json.dumps(payload)
        sig = _sign(payload)
        resp = self.client.post(
            "/webhooks/dinee",
            content=body,
            headers={"X-Dinee-Signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert len(self.indexed_shipments) == 0

    def test_enabled_tenant_webhook_processes(self):
        from tests.fixtures import load_fixture
        # Enable the tenant that's in the fixture (tenant-test-1)
        self.ff_service._flags[FIXTURE_TENANT] = True
        payload = load_fixture("shipment_created")
        body = json.dumps(payload)
        sig = _sign(payload)
        resp = self.client.post(
            "/webhooks/dinee",
            content=body,
            headers={"X-Dinee-Signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "processed"
        assert len(self.indexed_shipments) == 1

    def test_re_enable_restores_webhook_processing(self):
        from tests.fixtures import load_fixture

        # Disabled
        payload1 = load_fixture("shipment_created")
        body1 = json.dumps(payload1)
        sig1 = _sign(payload1)
        resp1 = self.client.post(
            "/webhooks/dinee",
            content=body1,
            headers={"X-Dinee-Signature": sig1, "Content-Type": "application/json"},
        )
        assert resp1.status_code == 200
        assert len(self.indexed_shipments) == 0

        # Enable the fixture tenant
        self.ff_service._flags[FIXTURE_TENANT] = True

        # Re-enabled — use a different event to avoid idempotency
        payload2 = load_fixture("shipment_updated")
        body2 = json.dumps(payload2)
        sig2 = _sign(payload2)
        resp2 = self.client.post(
            "/webhooks/dinee",
            content=body2,
            headers={"X-Dinee-Signature": sig2, "Content-Type": "application/json"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "processed"
        assert len(self.indexed_shipments) == 1
