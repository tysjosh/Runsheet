"""
Unit test — ``/ws/orders`` broadcasts never cross tenant boundaries
even when both clients subscribe to the same event type.

This test proves the OrdersWSManager's tenant isolation is enforced
at the broadcast level: a message for tenant A is NEVER delivered to
a client connected as tenant B, regardless of subscription overlap.

Validates: Requirement 9.1.4
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Set
from unittest.mock import AsyncMock, MagicMock

import pytest

from fuel.websocket.orders_ws import OrdersWSManager, VALID_SUBSCRIPTIONS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_websocket(*, fail_send: bool = False) -> MagicMock:
    """Create a mock WebSocket client.

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


def _get_broadcast_messages(ws: MagicMock) -> List[Dict[str, Any]]:
    """Extract all messages sent to a WebSocket mock (excluding handshake).

    The first send_json call is always the connection handshake message.
    Subsequent calls are broadcast messages.
    """
    calls = ws.send_json.call_args_list
    if not calls:
        return []
    # Skip the first call (handshake)
    return [call[0][0] for call in calls[1:]]


# ---------------------------------------------------------------------------
# Tests — Tenant isolation in broadcasts (Req 9.1.4)
# ---------------------------------------------------------------------------


class TestOrdersWSTenantFilter:
    """Prove /ws/orders broadcasts never cross tenant boundaries even
    when both clients subscribe to the same event type.

    Validates: Requirement 9.1.4
    """

    @pytest.mark.asyncio
    async def test_order_placed_isolated_between_tenants(self):
        """An order_placed broadcast for tenant A never reaches tenant B."""
        manager = OrdersWSManager()
        ws_a = _make_websocket()
        ws_b = _make_websocket()

        await manager.connect(ws_a, subscriptions=["order_placed"], tenant_id="tenant-A")
        await manager.connect(ws_b, subscriptions=["order_placed"], tenant_id="tenant-B")

        # Broadcast for tenant A
        count = await manager.broadcast_order_placed({
            "tenant_id": "tenant-A",
            "order_id": "ord_001",
            "status": "placed",
        })

        assert count == 1
        # Tenant A received the broadcast
        msgs_a = _get_broadcast_messages(ws_a)
        assert len(msgs_a) == 1
        assert msgs_a[0]["type"] == "order_placed"
        assert msgs_a[0]["tenant_id"] == "tenant-A"

        # Tenant B received NOTHING
        msgs_b = _get_broadcast_messages(ws_b)
        assert len(msgs_b) == 0

    @pytest.mark.asyncio
    async def test_order_status_changed_isolated_between_tenants(self):
        """An order_status_changed broadcast for tenant B never reaches tenant A."""
        manager = OrdersWSManager()
        ws_a = _make_websocket()
        ws_b = _make_websocket()

        await manager.connect(ws_a, subscriptions=["order_status_changed"], tenant_id="tenant-A")
        await manager.connect(ws_b, subscriptions=["order_status_changed"], tenant_id="tenant-B")

        # Broadcast for tenant B
        count = await manager.broadcast_order_status_changed({
            "tenant_id": "tenant-B",
            "order_id": "ord_002",
            "old_status": "placed",
            "new_status": "confirmed",
        })

        assert count == 1
        # Tenant A received NOTHING
        msgs_a = _get_broadcast_messages(ws_a)
        assert len(msgs_a) == 0

        # Tenant B received the broadcast
        msgs_b = _get_broadcast_messages(ws_b)
        assert len(msgs_b) == 1
        assert msgs_b[0]["type"] == "order_status_changed"
        assert msgs_b[0]["tenant_id"] == "tenant-B"

    @pytest.mark.asyncio
    async def test_driver_update_isolated_between_tenants(self):
        """A driver_update broadcast for tenant C never reaches tenants A or B."""
        manager = OrdersWSManager()
        ws_a = _make_websocket()
        ws_b = _make_websocket()
        ws_c = _make_websocket()

        await manager.connect(ws_a, subscriptions=["driver_update"], tenant_id="tenant-A")
        await manager.connect(ws_b, subscriptions=["driver_update"], tenant_id="tenant-B")
        await manager.connect(ws_c, subscriptions=["driver_update"], tenant_id="tenant-C")

        # Broadcast for tenant C
        count = await manager.broadcast_driver_update({
            "tenant_id": "tenant-C",
            "driver_id": "drv_001",
            "status": "active",
        })

        assert count == 1
        # Only tenant C received the broadcast
        assert len(_get_broadcast_messages(ws_a)) == 0
        assert len(_get_broadcast_messages(ws_b)) == 0
        msgs_c = _get_broadcast_messages(ws_c)
        assert len(msgs_c) == 1
        assert msgs_c[0]["tenant_id"] == "tenant-C"

    @pytest.mark.asyncio
    async def test_all_event_types_isolated_same_subscription(self):
        """All event types are isolated even when both tenants subscribe
        to the exact same set of events."""
        manager = OrdersWSManager()
        ws_a = _make_websocket()
        ws_b = _make_websocket()

        # Both subscribe to ALL event types
        all_subs = list(VALID_SUBSCRIPTIONS)
        await manager.connect(ws_a, subscriptions=all_subs, tenant_id="tenant-A")
        await manager.connect(ws_b, subscriptions=all_subs, tenant_id="tenant-B")

        # Broadcast each event type for tenant A
        await manager.broadcast_order_placed({
            "tenant_id": "tenant-A", "order_id": "ord_1", "status": "placed",
        })
        await manager.broadcast_order_status_changed({
            "tenant_id": "tenant-A", "order_id": "ord_1",
            "old_status": "placed", "new_status": "confirmed",
        })
        await manager.broadcast_order_assigned({
            "tenant_id": "tenant-A", "order_id": "ord_1", "driver_id": "drv_1",
        })
        await manager.broadcast_driver_update({
            "tenant_id": "tenant-A", "driver_id": "drv_1", "status": "active",
        })
        await manager.broadcast_sla_breach({
            "tenant_id": "tenant-A", "order_id": "ord_1", "breach_type": "late",
        })

        # Tenant A received all 5 broadcasts
        msgs_a = _get_broadcast_messages(ws_a)
        assert len(msgs_a) == 5

        # Tenant B received NONE
        msgs_b = _get_broadcast_messages(ws_b)
        assert len(msgs_b) == 0

    @pytest.mark.asyncio
    async def test_multiple_clients_same_tenant_all_receive(self):
        """Multiple clients for the same tenant all receive the broadcast."""
        manager = OrdersWSManager()
        ws_a1 = _make_websocket()
        ws_a2 = _make_websocket()
        ws_b = _make_websocket()

        await manager.connect(ws_a1, subscriptions=["order_placed"], tenant_id="tenant-A")
        await manager.connect(ws_a2, subscriptions=["order_placed"], tenant_id="tenant-A")
        await manager.connect(ws_b, subscriptions=["order_placed"], tenant_id="tenant-B")

        count = await manager.broadcast_order_placed({
            "tenant_id": "tenant-A",
            "order_id": "ord_multi",
            "status": "placed",
        })

        # Both tenant A clients received the broadcast
        assert count == 2
        assert len(_get_broadcast_messages(ws_a1)) == 1
        assert len(_get_broadcast_messages(ws_a2)) == 1

        # Tenant B received nothing
        assert len(_get_broadcast_messages(ws_b)) == 0

    @pytest.mark.asyncio
    async def test_broadcast_without_tenant_id_raises_error(self):
        """A broadcast payload missing tenant_id raises ValueError."""
        manager = OrdersWSManager()
        ws = _make_websocket()
        await manager.connect(ws, subscriptions=["order_placed"], tenant_id="tenant-A")

        with pytest.raises(ValueError, match="tenant_id"):
            await manager.broadcast_order_placed({
                "order_id": "ord_no_tenant",
                "status": "placed",
            })

    @pytest.mark.asyncio
    async def test_empty_tenant_id_in_payload_raises_error(self):
        """A broadcast payload with empty string tenant_id raises ValueError."""
        manager = OrdersWSManager()
        ws = _make_websocket()
        await manager.connect(ws, subscriptions=["order_placed"], tenant_id="tenant-A")

        with pytest.raises(ValueError, match="tenant_id"):
            await manager.broadcast_order_placed({
                "tenant_id": "",
                "order_id": "ord_empty_tenant",
                "status": "placed",
            })

    @pytest.mark.asyncio
    async def test_subscription_filter_combined_with_tenant_filter(self):
        """A client subscribed to order_placed for tenant A does NOT receive
        order_status_changed for tenant A — both filters apply."""
        manager = OrdersWSManager()
        ws_a_placed = _make_websocket()
        ws_a_status = _make_websocket()
        ws_b_placed = _make_websocket()

        await manager.connect(ws_a_placed, subscriptions=["order_placed"], tenant_id="tenant-A")
        await manager.connect(ws_a_status, subscriptions=["order_status_changed"], tenant_id="tenant-A")
        await manager.connect(ws_b_placed, subscriptions=["order_placed"], tenant_id="tenant-B")

        # Broadcast order_placed for tenant A
        count = await manager.broadcast_order_placed({
            "tenant_id": "tenant-A",
            "order_id": "ord_sub_test",
            "status": "placed",
        })

        # Only ws_a_placed receives it (correct tenant + correct subscription)
        assert count == 1
        assert len(_get_broadcast_messages(ws_a_placed)) == 1
        assert len(_get_broadcast_messages(ws_a_status)) == 0  # wrong subscription
        assert len(_get_broadcast_messages(ws_b_placed)) == 0  # wrong tenant

    @pytest.mark.asyncio
    async def test_rapid_broadcasts_maintain_isolation(self):
        """Rapid sequential broadcasts for different tenants maintain
        strict isolation — no message leaks under concurrency."""
        manager = OrdersWSManager()
        ws_a = _make_websocket()
        ws_b = _make_websocket()

        await manager.connect(ws_a, subscriptions=["order_placed"], tenant_id="tenant-A")
        await manager.connect(ws_b, subscriptions=["order_placed"], tenant_id="tenant-B")

        # Rapid-fire broadcasts alternating between tenants
        for i in range(10):
            tenant = "tenant-A" if i % 2 == 0 else "tenant-B"
            await manager.broadcast_order_placed({
                "tenant_id": tenant,
                "order_id": f"ord_{i:03d}",
                "status": "placed",
            })

        # Tenant A received exactly 5 messages (indices 0, 2, 4, 6, 8)
        msgs_a = _get_broadcast_messages(ws_a)
        assert len(msgs_a) == 5
        for msg in msgs_a:
            assert msg["tenant_id"] == "tenant-A"

        # Tenant B received exactly 5 messages (indices 1, 3, 5, 7, 9)
        msgs_b = _get_broadcast_messages(ws_b)
        assert len(msgs_b) == 5
        for msg in msgs_b:
            assert msg["tenant_id"] == "tenant-B"
