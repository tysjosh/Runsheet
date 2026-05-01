"""
Unit tests for BaseWSManager.

Tests the base WebSocket manager class including connect, disconnect,
broadcast with backpressure, stale client detection, dead client cleanup,
handshake confirmation, shutdown, and metrics.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.7, 6.9
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock

from websocket.base_ws_manager import BaseWSManager


# ---------------------------------------------------------------------------
# Concrete subclass for testing (BaseWSManager is abstract)
# ---------------------------------------------------------------------------


class ConcreteWSManager(BaseWSManager):
    """Minimal concrete subclass for testing the base class."""
    pass


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


# ---------------------------------------------------------------------------
# Tests: connect (Req 6.1, 6.9)
# ---------------------------------------------------------------------------


class TestConnect:
    """Tests for the connect method."""

    @pytest.mark.asyncio
    async def test_connect_accepts_websocket(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws)

        ws.accept.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_registers_client(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws)

        assert manager.get_connection_count() == 1

    @pytest.mark.asyncio
    async def test_connect_sends_handshake_confirmation(self):
        """Req 6.9: Standard handshake with manager name and timestamp."""
        manager = ConcreteWSManager("test_mgr")
        ws = _make_websocket()

        await manager.connect(ws)

        ws.send_json.assert_awaited_once()
        msg = ws.send_json.call_args[0][0]
        assert msg["type"] == "connection"
        assert msg["status"] == "connected"
        assert msg["manager"] == "test_mgr"
        assert "timestamp" in msg

    @pytest.mark.asyncio
    async def test_connect_increments_connections_total(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws)

        metrics = manager.get_metrics()
        assert metrics["connections_total"] == 1

    @pytest.mark.asyncio
    async def test_connect_stores_metadata(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws, tenant_id="tenant-1")

        meta = manager.get_client_metadata(ws)
        assert meta is not None
        assert meta["tenant_id"] == "tenant-1"
        assert meta["pending_count"] == 0
        assert meta["last_send"] is None
        assert isinstance(meta["connected_at"], datetime)

    @pytest.mark.asyncio
    async def test_connect_with_extra_metadata(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws, metadata={"custom_key": "custom_val"})

        meta = manager.get_client_metadata(ws)
        assert meta["custom_key"] == "custom_val"

    @pytest.mark.asyncio
    async def test_connect_multiple_clients(self):
        manager = ConcreteWSManager("test")
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect(ws1)
        await manager.connect(ws2)

        assert manager.get_connection_count() == 2
        assert manager.get_metrics()["connections_total"] == 2


# ---------------------------------------------------------------------------
# Tests: disconnect (Req 6.1)
# ---------------------------------------------------------------------------


class TestDisconnect:
    """Tests for the disconnect method."""

    @pytest.mark.asyncio
    async def test_disconnect_removes_client(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws)
        assert manager.get_connection_count() == 1

        await manager.disconnect(ws)
        assert manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_disconnect_increments_disconnections_total(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws)
        await manager.disconnect(ws)

        metrics = manager.get_metrics()
        assert metrics["disconnections_total"] == 1

    @pytest.mark.asyncio
    async def test_disconnect_unknown_client_is_noop(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.disconnect(ws)

        assert manager.get_connection_count() == 0
        assert manager.get_metrics()["disconnections_total"] == 0


# ---------------------------------------------------------------------------
# Tests: broadcast (Req 6.1, 6.2, 6.3, 6.7)
# ---------------------------------------------------------------------------


class TestBroadcast:
    """Tests for the broadcast method."""

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all_clients(self):
        manager = ConcreteWSManager("test")
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect(ws1)
        await manager.connect(ws2)

        count = await manager.broadcast({"type": "test", "data": "hello"})

        assert count == 2

    @pytest.mark.asyncio
    async def test_broadcast_increments_messages_sent_total(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws)
        await manager.broadcast({"type": "test"})

        metrics = manager.get_metrics()
        assert metrics["messages_sent_total"] == 1

    @pytest.mark.asyncio
    async def test_broadcast_no_clients_returns_zero(self):
        manager = ConcreteWSManager("test")

        count = await manager.broadcast({"type": "test"})
        assert count == 0

    @pytest.mark.asyncio
    async def test_broadcast_updates_last_send(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws)
        assert manager.get_client_metadata(ws)["last_send"] is None

        await manager.broadcast({"type": "test"})

        meta = manager.get_client_metadata(ws)
        assert meta["last_send"] is not None
        assert isinstance(meta["last_send"], datetime)


# ---------------------------------------------------------------------------
# Tests: backpressure (Req 6.2, 6.3)
# ---------------------------------------------------------------------------


class TestBackpressure:
    """Tests for backpressure enforcement."""

    @pytest.mark.asyncio
    async def test_backpressure_drops_messages_when_pending_exceeds_threshold(self):
        """Req 6.2: Drop messages when pending_count >= max_pending_messages."""
        manager = ConcreteWSManager("test", max_pending_messages=5)
        ws = _make_websocket()

        await manager.connect(ws)

        # Manually set pending_count above threshold
        manager._clients[ws]["pending_count"] = 5

        count = await manager.broadcast({"type": "test"})

        assert count == 0
        metrics = manager.get_metrics()
        assert metrics["messages_dropped_total"] == 1

    @pytest.mark.asyncio
    async def test_backpressure_increments_dropped_counter(self):
        """Req 6.3: Increment messages_dropped_total on drop."""
        manager = ConcreteWSManager("test", max_pending_messages=2)
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect(ws1)
        await manager.connect(ws2)

        # Only ws1 is over threshold
        manager._clients[ws1]["pending_count"] = 2

        count = await manager.broadcast({"type": "test"})

        assert count == 1  # Only ws2 received
        assert manager.get_metrics()["messages_dropped_total"] == 1

    @pytest.mark.asyncio
    async def test_backpressure_does_not_drop_below_threshold(self):
        manager = ConcreteWSManager("test", max_pending_messages=100)
        ws = _make_websocket()

        await manager.connect(ws)
        manager._clients[ws]["pending_count"] = 99

        count = await manager.broadcast({"type": "test"})

        assert count == 1
        assert manager.get_metrics()["messages_dropped_total"] == 0


# ---------------------------------------------------------------------------
# Tests: dead client cleanup (Req 6.7)
# ---------------------------------------------------------------------------


class TestDeadClientCleanup:
    """Tests for dead client cleanup during broadcast."""

    @pytest.mark.asyncio
    async def test_dead_client_removed_during_broadcast(self):
        """Req 6.7: Clean up dead clients within 5 seconds."""
        manager = ConcreteWSManager("test")
        ws_alive = _make_websocket()
        ws_dead = _make_websocket(fail_send=True)

        await manager.connect(ws_alive)
        # Manually add dead client to avoid handshake failure
        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "",
            "pending_count": 0,
        }

        count = await manager.broadcast({"type": "test"})

        assert count == 1
        assert manager.get_connection_count() == 1
        assert ws_dead not in manager._clients

    @pytest.mark.asyncio
    async def test_dead_client_increments_send_failures(self):
        manager = ConcreteWSManager("test")
        ws_dead = _make_websocket(fail_send=True)

        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "",
            "pending_count": 0,
        }

        await manager.broadcast({"type": "test"})

        metrics = manager.get_metrics()
        assert metrics["send_failures_total"] == 1

    @pytest.mark.asyncio
    async def test_dead_client_increments_disconnections(self):
        manager = ConcreteWSManager("test")
        ws_dead = _make_websocket(fail_send=True)

        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "",
            "pending_count": 0,
        }

        await manager.broadcast({"type": "test"})

        metrics = manager.get_metrics()
        assert metrics["disconnections_total"] == 1


# ---------------------------------------------------------------------------
# Tests: stale client detection (Req 6.4)
# ---------------------------------------------------------------------------


class TestStaleClientDetection:
    """Tests for get_stale_clients."""

    @pytest.mark.asyncio
    async def test_get_stale_clients_returns_stale(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws)

        # Set last_send to a time well in the past
        manager._clients[ws]["last_send"] = datetime.now(timezone.utc) - timedelta(seconds=200)

        stale = manager.get_stale_clients(stale_seconds=120.0)
        assert ws in stale

    @pytest.mark.asyncio
    async def test_get_stale_clients_excludes_recent(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws)
        manager._clients[ws]["last_send"] = datetime.now(timezone.utc) - timedelta(seconds=10)

        stale = manager.get_stale_clients(stale_seconds=120.0)
        assert ws not in stale

    @pytest.mark.asyncio
    async def test_get_stale_clients_excludes_never_sent(self):
        """Clients that have never received a message are not stale."""
        manager = ConcreteWSManager("test")
        ws = _make_websocket()

        await manager.connect(ws)
        # last_send is None by default

        stale = manager.get_stale_clients(stale_seconds=0.0)
        assert ws not in stale


# ---------------------------------------------------------------------------
# Tests: shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    """Tests for the shutdown method."""

    @pytest.mark.asyncio
    async def test_shutdown_closes_all_connections(self):
        manager = ConcreteWSManager("test")
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect(ws1)
        await manager.connect(ws2)

        await manager.shutdown()

        assert manager.get_connection_count() == 0
        ws1.close.assert_awaited_once()
        ws2.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_close_errors(self):
        manager = ConcreteWSManager("test")
        ws = _make_websocket()
        ws.close = AsyncMock(side_effect=RuntimeError("already closed"))

        await manager.connect(ws)

        await manager.shutdown()
        assert manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_shutdown_empty_manager_is_noop(self):
        manager = ConcreteWSManager("test")

        await manager.shutdown()
        assert manager.get_connection_count() == 0


# ---------------------------------------------------------------------------
# Tests: get_metrics
# ---------------------------------------------------------------------------


class TestGetMetrics:
    """Tests for the get_metrics method."""

    @pytest.mark.asyncio
    async def test_get_metrics_returns_correct_snapshot(self):
        manager = ConcreteWSManager("test_mgr")
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect(ws1)
        await manager.connect(ws2)
        await manager.broadcast({"type": "test"})
        await manager.disconnect(ws1)

        metrics = manager.get_metrics()

        assert metrics["manager"] == "test_mgr"
        assert metrics["active_connections"] == 1
        assert metrics["connections_total"] == 2
        assert metrics["disconnections_total"] == 1
        assert metrics["messages_sent_total"] == 2  # broadcast to 2 clients
        assert metrics["send_failures_total"] == 0
        assert metrics["messages_dropped_total"] == 0

    def test_get_metrics_initial_state(self):
        manager = ConcreteWSManager("fresh")

        metrics = manager.get_metrics()

        assert metrics["manager"] == "fresh"
        assert metrics["active_connections"] == 0
        assert metrics["connections_total"] == 0
        assert metrics["disconnections_total"] == 0
        assert metrics["messages_sent_total"] == 0
        assert metrics["send_failures_total"] == 0
        assert metrics["messages_dropped_total"] == 0


# ---------------------------------------------------------------------------
# Tests: active_connections property
# ---------------------------------------------------------------------------


class TestActiveConnections:
    """Tests for the active_connections gauge property."""

    @pytest.mark.asyncio
    async def test_active_connections_reflects_count(self):
        manager = ConcreteWSManager("test")
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        assert manager.active_connections == 0

        await manager.connect(ws1)
        assert manager.active_connections == 1

        await manager.connect(ws2)
        assert manager.active_connections == 2

        await manager.disconnect(ws1)
        assert manager.active_connections == 1
