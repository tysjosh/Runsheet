"""
Integration tests for the Ops WebSocket manager.

Tests connection lifecycle, subscription filtering, and broadcast on upsert.

Validates: Requirements 16.1-16.6
"""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Patch elasticsearch_service BEFORE any ops imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from ops.websocket.ops_ws import OpsWebSocketManager, VALID_SUBSCRIPTIONS

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers — fake WebSocket for testing
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


# ===========================================================================
# 23.3 — WebSocket integration tests
# ===========================================================================


class TestWebSocketConnectionLifecycle:
    """
    Test connection accept, registration, and disconnect.

    Validates: Requirements 16.1, 16.6
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.manager = OpsWebSocketManager()

    @pytest.mark.asyncio
    async def test_connect_accepts_and_sends_connection_message(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, tenant_id="tenant-1")

        assert ws.accepted
        assert len(ws.messages) == 1
        msg = ws.messages[0]
        assert msg["type"] == "connection"
        assert msg["status"] == "connected"
        assert self.manager.get_connection_count() == 1

    @pytest.mark.asyncio
    async def test_connect_with_subscriptions(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, subscriptions=["shipment_update"], tenant_id="tenant-1")

        msg = ws.messages[0]
        assert "shipment_update" in msg["subscriptions"]
        assert self.manager.get_connection_count() == 1

    @pytest.mark.asyncio
    async def test_connect_with_no_subscriptions_gets_all(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, tenant_id="tenant-1")

        msg = ws.messages[0]
        assert set(msg["subscriptions"]) == VALID_SUBSCRIPTIONS

    @pytest.mark.asyncio
    async def test_connect_ignores_invalid_subscriptions(self):
        ws = FakeWebSocket()
        await self.manager.connect(
            ws,
            subscriptions=["shipment_update", "invalid_sub"],
            tenant_id="tenant-1",
        )

        msg = ws.messages[0]
        assert "shipment_update" in msg["subscriptions"]
        assert "invalid_sub" not in msg["subscriptions"]

    @pytest.mark.asyncio
    async def test_disconnect_removes_client(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, tenant_id="tenant-1")
        assert self.manager.get_connection_count() == 1

        await self.manager.disconnect(ws)
        assert self.manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_multiple_clients_tracked(self):
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()
        await self.manager.connect(ws1, tenant_id="tenant-1")
        await self.manager.connect(ws2, tenant_id="tenant-2")

        assert self.manager.get_connection_count() == 2

        await self.manager.disconnect(ws1)
        assert self.manager.get_connection_count() == 1

    @pytest.mark.asyncio
    async def test_shutdown_closes_all_connections(self):
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()
        await self.manager.connect(ws1, tenant_id="tenant-1")
        await self.manager.connect(ws2, tenant_id="tenant-2")

        await self.manager.shutdown()
        assert self.manager.get_connection_count() == 0
        assert ws1.closed
        assert ws2.closed


class TestWebSocketSubscriptionFiltering:
    """
    Test that broadcasts are filtered by client subscriptions.

    Validates: Requirements 16.2-16.4
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.manager = OpsWebSocketManager()

    @pytest.mark.asyncio
    async def test_shipment_update_sent_to_subscribed_client(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, subscriptions=["shipment_update"], tenant_id="t1")
        ws.messages.clear()  # Clear connection message

        count = await self.manager.broadcast_shipment_update(
            {"shipment_id": "SHP-001", "status": "delivered", "tenant_id": "t1"}
        )

        assert count == 1
        assert len(ws.messages) == 1
        assert ws.messages[0]["type"] == "shipment_update"

    @pytest.mark.asyncio
    async def test_shipment_update_not_sent_to_rider_only_subscriber(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, subscriptions=["rider_update"], tenant_id="t1")
        ws.messages.clear()

        count = await self.manager.broadcast_shipment_update(
            {"shipment_id": "SHP-001", "status": "delivered", "tenant_id": "t1"}
        )

        assert count == 0
        assert len(ws.messages) == 0

    @pytest.mark.asyncio
    async def test_rider_update_sent_to_subscribed_client(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, subscriptions=["rider_update"], tenant_id="t1")
        ws.messages.clear()

        count = await self.manager.broadcast_rider_update(
            {"rider_id": "RDR-001", "status": "active", "tenant_id": "t1"}
        )

        assert count == 1
        assert ws.messages[0]["type"] == "rider_update"

    @pytest.mark.asyncio
    async def test_sla_breach_sent_to_subscribed_client(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, subscriptions=["sla_breach"], tenant_id="t1")
        ws.messages.clear()

        count = await self.manager.broadcast_sla_breach(
            {"shipment_id": "SHP-002", "breach_minutes": 30, "tenant_id": "t1"}
        )

        assert count == 1
        assert ws.messages[0]["type"] == "sla_breach"

    @pytest.mark.asyncio
    async def test_no_subscriptions_receives_all_events(self):
        """Client with no subscriptions (empty set) receives all event types."""
        ws = FakeWebSocket()
        await self.manager.connect(ws, subscriptions=[], tenant_id="t1")
        ws.messages.clear()

        await self.manager.broadcast_shipment_update({"tenant_id": "t1"})
        await self.manager.broadcast_rider_update({"tenant_id": "t1"})
        await self.manager.broadcast_sla_breach({"tenant_id": "t1"})

        assert len(ws.messages) == 3
        types = {m["type"] for m in ws.messages}
        assert types == {"shipment_update", "rider_update", "sla_breach"}

    @pytest.mark.asyncio
    async def test_multiple_clients_selective_delivery(self):
        """Two clients with different subscriptions receive only their events."""
        ws_ship = FakeWebSocket()
        ws_rider = FakeWebSocket()
        await self.manager.connect(ws_ship, subscriptions=["shipment_update"], tenant_id="t1")
        await self.manager.connect(ws_rider, subscriptions=["rider_update"], tenant_id="t1")
        ws_ship.messages.clear()
        ws_rider.messages.clear()

        await self.manager.broadcast_shipment_update({"tenant_id": "t1"})
        await self.manager.broadcast_rider_update({"tenant_id": "t1"})

        assert len(ws_ship.messages) == 1
        assert ws_ship.messages[0]["type"] == "shipment_update"
        assert len(ws_rider.messages) == 1
        assert ws_rider.messages[0]["type"] == "rider_update"


class TestWebSocketBroadcastOnUpsert:
    """
    Test that broadcast messages contain the correct data payload.

    Validates: Requirements 16.2, 16.3
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.manager = OpsWebSocketManager()

    @pytest.mark.asyncio
    async def test_broadcast_shipment_contains_data(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, tenant_id="t1")
        ws.messages.clear()

        shipment_data = {
            "shipment_id": "SHP-001",
            "status": "in_transit",
            "tenant_id": "t1",
            "rider_id": "RDR-001",
        }
        await self.manager.broadcast_shipment_update(shipment_data)

        msg = ws.messages[0]
        assert msg["type"] == "shipment_update"
        assert msg["data"]["shipment_id"] == "SHP-001"
        assert msg["data"]["status"] == "in_transit"
        assert "timestamp" in msg

    @pytest.mark.asyncio
    async def test_broadcast_rider_contains_data(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, tenant_id="t1")
        ws.messages.clear()

        rider_data = {
            "rider_id": "RDR-001",
            "status": "active",
            "tenant_id": "t1",
            "active_shipment_count": 3,
        }
        await self.manager.broadcast_rider_update(rider_data)

        msg = ws.messages[0]
        assert msg["type"] == "rider_update"
        assert msg["data"]["rider_id"] == "RDR-001"
        assert msg["data"]["active_shipment_count"] == 3

    @pytest.mark.asyncio
    async def test_broadcast_returns_count_of_recipients(self):
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()
        await self.manager.connect(ws1, tenant_id="t1")
        await self.manager.connect(ws2, tenant_id="t1")

        count = await self.manager.broadcast_shipment_update({"tenant_id": "t1"})
        assert count == 2

    @pytest.mark.asyncio
    async def test_broadcast_to_no_clients_returns_zero(self):
        count = await self.manager.broadcast_shipment_update({"tenant_id": "t1"})
        assert count == 0


class TestWebSocketClientMessageHandling:
    """
    Test client message handling (pong, subscribe).

    Validates: Requirement 16.4
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.manager = OpsWebSocketManager()

    @pytest.mark.asyncio
    async def test_subscribe_message_updates_subscriptions(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, subscriptions=["shipment_update"], tenant_id="t1")
        ws.messages.clear()

        await self.manager.handle_client_message(
            ws, json.dumps({"type": "subscribe", "subscriptions": ["rider_update", "sla_breach"]})
        )

        # Should receive a subscribed confirmation
        assert len(ws.messages) == 1
        assert ws.messages[0]["type"] == "subscribed"
        assert "rider_update" in ws.messages[0]["subscriptions"]
        assert "sla_breach" in ws.messages[0]["subscriptions"]

    @pytest.mark.asyncio
    async def test_pong_message_marks_client_alive(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, tenant_id="t1")

        # Manually mark pending (simulating heartbeat cycle)
        client = self.manager._clients[ws]
        client.mark_pending()
        assert not client.is_alive

        await self.manager.handle_client_message(ws, json.dumps({"type": "pong"}))
        assert client.is_alive

    @pytest.mark.asyncio
    async def test_invalid_json_message_ignored(self):
        ws = FakeWebSocket()
        await self.manager.connect(ws, tenant_id="t1")
        initial_count = len(ws.messages)

        await self.manager.handle_client_message(ws, "not-json")
        # No crash, no extra messages
        assert len(ws.messages) == initial_count
