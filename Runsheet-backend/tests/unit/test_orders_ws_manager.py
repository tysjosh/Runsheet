"""
Unit tests for OrdersWSManager.

Tests:
1. Two-tenant isolation — tenant A broadcast never reaches tenant B subscriber
   even when both subscribe to the same event type.
2. Backpressure — when a client exceeds max_pending_messages, messages are
   dropped (reuses the BaseWSManager contract pattern).
3. Dual-broadcast — during `active_gated`, legacy events still flow through
   the OpsWebSocketManager.

Validates: Requirements 4.1.2, 4.1.4, 10.2.1
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fuel.websocket.orders_ws import OrdersWSManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_websocket(*, fail_send: bool = False) -> MagicMock:
    """Create a mock WebSocket.

    Parameters
    ----------
    fail_send : bool
        If True, ``send_json`` raises an exception to simulate a dead client.
    """
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    if fail_send:
        ws.send_json = AsyncMock(side_effect=RuntimeError("connection closed"))
    else:
        ws.send_json = AsyncMock()
    return ws


# ``_make_ops_ws_manager`` and ``_make_feature_flag_service`` lived here.
# Both existed only for the legacy dual-broadcast / dual-write tests below,
# which went out with the legacy mirror.


# ---------------------------------------------------------------------------
# Tests: Two-tenant isolation (Req 4.1.2)
# ---------------------------------------------------------------------------


class TestTwoTenantIsolation:
    """Tenant A broadcast never reaches tenant B subscriber even when both
    subscribe to the same event type.

    Validates: Requirement 4.1.2
    """

    @pytest.mark.asyncio
    async def test_tenant_a_broadcast_never_reaches_tenant_b(self):
        """Cross-tenant payloads MUST never be delivered."""
        manager = OrdersWSManager()
        ws_a = _make_websocket()
        ws_b = _make_websocket()

        # Both tenants subscribe to the same event type
        await manager.connect(ws_a, subscriptions=["order_placed"], tenant_id="tenant-A")
        await manager.connect(ws_b, subscriptions=["order_placed"], tenant_id="tenant-B")

        # Broadcast an order_placed event for tenant A
        order_data = {
            "tenant_id": "tenant-A",
            "order_id": "ord_abc123",
            "status": "placed",
        }
        count = await manager.broadcast_order_placed(order_data)

        # Only tenant A's client should receive the message
        assert count == 1

        # ws_a received: handshake + broadcast = 2 calls
        assert ws_a.send_json.await_count == 2
        broadcast_msg = ws_a.send_json.call_args_list[1][0][0]
        assert broadcast_msg["type"] == "order_placed"
        assert broadcast_msg["tenant_id"] == "tenant-A"

        # ws_b received only the handshake (1 call), never the broadcast
        assert ws_b.send_json.await_count == 1

    @pytest.mark.asyncio
    async def test_tenant_b_broadcast_never_reaches_tenant_a(self):
        """Symmetric check: tenant B broadcast does not reach tenant A."""
        manager = OrdersWSManager()
        ws_a = _make_websocket()
        ws_b = _make_websocket()

        await manager.connect(ws_a, subscriptions=["order_status_changed"], tenant_id="tenant-A")
        await manager.connect(ws_b, subscriptions=["order_status_changed"], tenant_id="tenant-B")

        # Broadcast for tenant B
        order_data = {
            "tenant_id": "tenant-B",
            "order_id": "ord_xyz789",
            "status": "dispatched",
        }
        count = await manager.broadcast_order_status_changed(order_data)

        assert count == 1

        # ws_a only got handshake
        assert ws_a.send_json.await_count == 1

        # ws_b got handshake + broadcast
        assert ws_b.send_json.await_count == 2
        broadcast_msg = ws_b.send_json.call_args_list[1][0][0]
        assert broadcast_msg["type"] == "order_status_changed"
        assert broadcast_msg["tenant_id"] == "tenant-B"

    @pytest.mark.asyncio
    async def test_same_event_type_different_tenants_isolated(self):
        """Multiple tenants subscribing to the same event type are fully isolated."""
        manager = OrdersWSManager()
        ws_a = _make_websocket()
        ws_b = _make_websocket()
        ws_c = _make_websocket()

        await manager.connect(ws_a, subscriptions=["driver_update"], tenant_id="tenant-A")
        await manager.connect(ws_b, subscriptions=["driver_update"], tenant_id="tenant-B")
        await manager.connect(ws_c, subscriptions=["driver_update"], tenant_id="tenant-C")

        # Broadcast for tenant B only
        driver_data = {
            "tenant_id": "tenant-B",
            "driver_id": "drv_001",
            "status": "active",
        }
        count = await manager.broadcast_driver_update(driver_data)

        assert count == 1

        # Only ws_b got the broadcast (handshake + broadcast = 2)
        assert ws_a.send_json.await_count == 1  # handshake only
        assert ws_b.send_json.await_count == 2  # handshake + broadcast
        assert ws_c.send_json.await_count == 1  # handshake only

    @pytest.mark.asyncio
    async def test_broadcast_envelope_always_carries_tenant_id(self):
        """Req 4.1.4: Every broadcast envelope includes tenant_id."""
        manager = OrdersWSManager()
        ws = _make_websocket()

        await manager.connect(ws, subscriptions=["order_placed"], tenant_id="tenant-X")

        order_data = {
            "tenant_id": "tenant-X",
            "order_id": "ord_envelope_test",
        }
        await manager.broadcast_order_placed(order_data)

        broadcast_msg = ws.send_json.call_args_list[1][0][0]
        assert "tenant_id" in broadcast_msg
        assert broadcast_msg["tenant_id"] == "tenant-X"

    @pytest.mark.asyncio
    async def test_missing_tenant_id_in_payload_raises(self):
        """Broadcast without tenant_id in payload raises ValueError."""
        manager = OrdersWSManager()
        ws = _make_websocket()

        await manager.connect(ws, subscriptions=["order_placed"], tenant_id="tenant-A")

        with pytest.raises(ValueError, match="tenant_id"):
            await manager.broadcast_order_placed({"order_id": "ord_no_tenant"})

    @pytest.mark.asyncio
    async def test_wildcard_subscription_still_tenant_scoped(self):
        """A client with no subscription filter (receives all event types)
        still only gets events for its own tenant."""
        manager = OrdersWSManager()
        ws_a = _make_websocket()
        ws_b = _make_websocket()

        # ws_a subscribes to all events (empty subscriptions)
        await manager.connect(ws_a, subscriptions=None, tenant_id="tenant-A")
        # ws_b also subscribes to all events
        await manager.connect(ws_b, subscriptions=None, tenant_id="tenant-B")

        # Broadcast for tenant A
        order_data = {"tenant_id": "tenant-A", "order_id": "ord_wildcard"}
        count = await manager.broadcast_order_placed(order_data)

        assert count == 1
        # ws_a got handshake + broadcast
        assert ws_a.send_json.await_count == 2
        # ws_b got only handshake
        assert ws_b.send_json.await_count == 1


# ---------------------------------------------------------------------------
# Tests: Backpressure (Req 4.1.5 / BaseWSManager contract)
# ---------------------------------------------------------------------------


class TestBackpressure:
    """When a client exceeds max_pending_messages, messages are dropped.

    Reuses the BaseWSManager contract pattern applied to OrdersWSManager.

    Validates: Requirement 4.1.4 (backpressure from BaseWSManager)
    """

    @pytest.mark.asyncio
    async def test_backpressure_drops_messages_when_pending_exceeds_threshold(self):
        """Messages are dropped when pending_count >= max_pending_messages."""
        manager = OrdersWSManager(max_pending_messages=5)
        ws = _make_websocket()

        await manager.connect(ws, subscriptions=["order_placed"], tenant_id="tenant-A")

        # Manually set pending_count above threshold
        manager._clients[ws]["pending_count"] = 5

        order_data = {"tenant_id": "tenant-A", "order_id": "ord_bp_test"}
        count = await manager.broadcast_order_placed(order_data)

        assert count == 0
        metrics = manager.get_metrics()
        assert metrics["messages_dropped_total"] == 1

    @pytest.mark.asyncio
    async def test_backpressure_increments_dropped_counter(self):
        """messages_dropped_total increments on each drop."""
        manager = OrdersWSManager(max_pending_messages=2)
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect(ws1, subscriptions=["order_placed"], tenant_id="tenant-A")
        await manager.connect(ws2, subscriptions=["order_placed"], tenant_id="tenant-A")

        # Only ws1 is over threshold
        manager._clients[ws1]["pending_count"] = 2

        order_data = {"tenant_id": "tenant-A", "order_id": "ord_bp_counter"}
        count = await manager.broadcast_order_placed(order_data)

        # Only ws2 received the message
        assert count == 1
        assert manager.get_metrics()["messages_dropped_total"] == 1

    @pytest.mark.asyncio
    async def test_backpressure_does_not_drop_below_threshold(self):
        """Messages are delivered when pending_count < max_pending_messages."""
        manager = OrdersWSManager(max_pending_messages=100)
        ws = _make_websocket()

        await manager.connect(ws, subscriptions=["order_placed"], tenant_id="tenant-A")
        manager._clients[ws]["pending_count"] = 99

        order_data = {"tenant_id": "tenant-A", "order_id": "ord_bp_ok"}
        count = await manager.broadcast_order_placed(order_data)

        assert count == 1
        assert manager.get_metrics()["messages_dropped_total"] == 0

    @pytest.mark.asyncio
    async def test_backpressure_per_client_independent(self):
        """Backpressure is per-client — one client being over threshold
        does not affect other clients in the same tenant."""
        manager = OrdersWSManager(max_pending_messages=3)
        ws_ok = _make_websocket()
        ws_full = _make_websocket()

        await manager.connect(ws_ok, subscriptions=["sla_breach"], tenant_id="tenant-A")
        await manager.connect(ws_full, subscriptions=["sla_breach"], tenant_id="tenant-A")

        # ws_full is at capacity
        manager._clients[ws_full]["pending_count"] = 3

        breach_data = {"tenant_id": "tenant-A", "order_id": "ord_sla"}
        count = await manager.broadcast_sla_breach(breach_data)

        # Only ws_ok received
        assert count == 1
        assert manager.get_metrics()["messages_dropped_total"] == 1

    @pytest.mark.asyncio
    async def test_dead_client_cleaned_up_during_broadcast(self):
        """Dead clients are removed during broadcast (BaseWSManager contract)."""
        manager = OrdersWSManager()
        ws_alive = _make_websocket()
        ws_dead = _make_websocket(fail_send=True)

        await manager.connect(ws_alive, subscriptions=["order_placed"], tenant_id="tenant-A")

        # Manually add dead client to avoid handshake failure
        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "tenant-A",
            "pending_count": 0,
            "subscriptions": {"order_placed"},
            "_alive": True,
        }

        order_data = {"tenant_id": "tenant-A", "order_id": "ord_dead_test"}
        count = await manager.broadcast_order_placed(order_data)

        assert count == 1
        assert ws_dead not in manager._clients
        assert manager.get_metrics()["send_failures_total"] == 1


# ---------------------------------------------------------------------------
# Tests: Dual-broadcast during active_gated (Req 4.1.3)
# ---------------------------------------------------------------------------


class TestOrdersBroadcast:
    """The new OrdersWSManager broadcast fires on every write.

    This class used to also assert the legacy dual-broadcast /
    dual-write behaviour (``test_dual_broadcast_calls_legacy_ops_ws_during_active_gated``,
    ``test_legacy_broadcast_skipped_during_active_auto``,
    ``test_legacy_broadcast_active_during_shadow``,
    ``test_legacy_dual_write_failure_does_not_block_main_path``). All four
    exercised the retired LegacyDualWriter mirror into
    ``shipments_current`` and were removed with it — there is no longer an
    overlay state in which a write reaches the legacy surface.

    Validates: Requirement 4.1.3
    """

    @pytest.mark.asyncio
    async def test_new_orders_ws_broadcast_always_fires(self):
        """The new OrdersWSManager broadcast fires regardless of overlay state."""
        manager = OrdersWSManager()
        ws = _make_websocket()

        await manager.connect(ws, subscriptions=["order_placed"], tenant_id="tenant-A")

        order_data = {"tenant_id": "tenant-A", "order_id": "ord_always"}
        count = await manager.broadcast_order_placed(order_data)

        assert count == 1
        # Handshake + broadcast
        assert ws.send_json.await_count == 2
        broadcast_msg = ws.send_json.call_args_list[1][0][0]
        assert broadcast_msg["type"] == "order_placed"
