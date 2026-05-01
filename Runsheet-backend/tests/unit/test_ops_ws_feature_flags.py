"""
Unit tests for feature flag integration in the OpsWebSocketManager.

Verifies:
- New connections for disabled tenants are rejected with close code 4403
- Existing clients are disconnected when their tenant is disabled (via heartbeat)
- Disabled tenant data is excluded from broadcasts

Validates: Requirement 27.3
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops.websocket.ops_ws import OpsWebSocketManager


def _make_ws(accepted: bool = True) -> MagicMock:
    """Create a mock WebSocket."""
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


def _make_ff_service(enabled: bool = True) -> AsyncMock:
    """Create a mock FeatureFlagService."""
    svc = AsyncMock()
    svc.is_enabled = AsyncMock(return_value=enabled)
    return svc


def _make_client_meta(tenant_id: str = "", subscriptions=None):
    """Create a client metadata dict matching BaseWSManager format."""
    return {
        "connected_at": datetime.now(timezone.utc),
        "last_send": None,
        "tenant_id": tenant_id,
        "pending_count": 0,
        "subscriptions": subscriptions or set(),
        "_alive": True,
    }


class TestConnectRejectsDisabledTenant:
    """New /ws/ops connections for disabled tenants are rejected with 4403."""

    @pytest.mark.asyncio
    async def test_reject_disabled_tenant_with_4403(self):
        mgr = OpsWebSocketManager()
        mgr.set_feature_flag_service(_make_ff_service(enabled=False))

        ws = _make_ws()
        await mgr.connect(ws, tenant_id="disabled-tenant")

        # Should have accepted then immediately closed with 4403
        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=4403, reason="tenant_disabled")

        # Client should NOT be registered
        assert ws not in mgr._clients

    @pytest.mark.asyncio
    async def test_accept_enabled_tenant(self):
        mgr = OpsWebSocketManager()
        mgr.set_feature_flag_service(_make_ff_service(enabled=True))

        ws = _make_ws()
        await mgr.connect(ws, tenant_id="enabled-tenant")

        ws.accept.assert_awaited_once()
        ws.close.assert_not_awaited()
        assert ws in mgr._clients
        assert mgr._clients[ws]["tenant_id"] == "enabled-tenant"

        # Cleanup
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_accept_when_no_ff_service(self):
        """Without a feature flag service, all connections are accepted."""
        mgr = OpsWebSocketManager()

        ws = _make_ws()
        await mgr.connect(ws, tenant_id="any-tenant")

        ws.accept.assert_awaited_once()
        ws.close.assert_not_awaited()
        assert ws in mgr._clients

        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_accept_when_ff_check_fails(self):
        """If the feature flag check raises, fail-open and allow connection."""
        ff = AsyncMock()
        ff.is_enabled = AsyncMock(side_effect=RuntimeError("Redis down"))

        mgr = OpsWebSocketManager()
        mgr.set_feature_flag_service(ff)

        ws = _make_ws()
        await mgr.connect(ws, tenant_id="some-tenant")

        ws.accept.assert_awaited_once()
        ws.close.assert_not_awaited()
        assert ws in mgr._clients

        await mgr.shutdown()


class TestDisconnectTenant:
    """disconnect_tenant() closes all connections for a specific tenant."""

    @pytest.mark.asyncio
    async def test_disconnect_all_clients_for_tenant(self):
        mgr = OpsWebSocketManager()
        mgr.set_feature_flag_service(_make_ff_service(enabled=True))

        ws1 = _make_ws()
        ws2 = _make_ws()
        ws3 = _make_ws()

        await mgr.connect(ws1, tenant_id="tenant-a")
        await mgr.connect(ws2, tenant_id="tenant-a")
        await mgr.connect(ws3, tenant_id="tenant-b")

        count = await mgr.disconnect_tenant("tenant-a")

        assert count == 2
        assert ws1 not in mgr._clients
        assert ws2 not in mgr._clients
        # tenant-b client should remain
        assert ws3 in mgr._clients

        ws1.close.assert_awaited_once_with(code=4403, reason="tenant_disabled")
        ws2.close.assert_awaited_once_with(code=4403, reason="tenant_disabled")
        ws3.close.assert_not_awaited()

        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_disconnect_tenant_no_clients(self):
        mgr = OpsWebSocketManager()
        count = await mgr.disconnect_tenant("nonexistent")
        assert count == 0


class TestBroadcastExcludesDisabledTenants:
    """Broadcasts skip data belonging to disabled tenants."""

    @pytest.mark.asyncio
    async def test_broadcast_skips_disabled_tenant_data(self):
        mgr = OpsWebSocketManager()
        mgr.set_feature_flag_service(_make_ff_service(enabled=False))

        ws = _make_ws()
        # Manually register a client using metadata dict
        mgr._clients[ws] = _make_client_meta(tenant_id="tenant-x")

        sent = await mgr.broadcast_shipment_update({"tenant_id": "disabled-tenant", "status": "delivered"})

        assert sent == 0
        ws.send_json.assert_not_awaited()

        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_broadcast_sends_enabled_tenant_data(self):
        mgr = OpsWebSocketManager()
        mgr.set_feature_flag_service(_make_ff_service(enabled=True))

        ws = _make_ws()
        mgr._clients[ws] = _make_client_meta(tenant_id="tenant-x")

        sent = await mgr.broadcast_shipment_update({"tenant_id": "enabled-tenant", "status": "delivered"})

        assert sent == 1
        ws.send_json.assert_awaited_once()

        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_broadcast_without_tenant_id_in_data(self):
        """Data without tenant_id is broadcast normally (no flag check)."""
        mgr = OpsWebSocketManager()
        mgr.set_feature_flag_service(_make_ff_service(enabled=False))

        ws = _make_ws()
        mgr._clients[ws] = _make_client_meta(tenant_id="tenant-x")

        sent = await mgr.broadcast_rider_update({"status": "active"})

        assert sent == 1
        ws.send_json.assert_awaited_once()

        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_broadcast_when_ff_check_fails_sends_anyway(self):
        """If feature flag check fails during broadcast, fail-open and send."""
        ff = AsyncMock()
        ff.is_enabled = AsyncMock(side_effect=RuntimeError("Redis down"))

        mgr = OpsWebSocketManager()
        mgr.set_feature_flag_service(ff)

        ws = _make_ws()
        mgr._clients[ws] = _make_client_meta(tenant_id="tenant-x")

        sent = await mgr.broadcast_shipment_update({"tenant_id": "some-tenant", "status": "delivered"})

        assert sent == 1
        ws.send_json.assert_awaited_once()

        await mgr.shutdown()
