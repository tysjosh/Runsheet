"""
Unit tests for the Agent Activity WebSocket Manager.

Tests the AgentActivityWSManager class including connect, disconnect,
broadcast_activity, broadcast_approval_event, broadcast_event methods,
and dead client cleanup on broadcast failures.

Requirements: 2.7, 8.7
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from Agents.agent_ws_manager import AgentActivityWSManager, get_agent_ws_manager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_websocket(*, fail_send: bool = False, fail_accept: bool = False):
    """Create a mock WebSocket.

    Parameters
    ----------
    fail_send : bool
        If True, ``send_json`` raises an exception to simulate a dead client.
    fail_accept : bool
        If True, ``accept`` raises an exception.
    """
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()

    if fail_send:
        ws.send_json = AsyncMock(side_effect=RuntimeError("connection closed"))
    else:
        ws.send_json = AsyncMock()

    if fail_accept:
        ws.accept = AsyncMock(side_effect=RuntimeError("accept failed"))

    return ws


# ---------------------------------------------------------------------------
# Tests: connect
# ---------------------------------------------------------------------------


class TestConnect:
    """Tests for the connect method."""

    @pytest.mark.asyncio
    async def test_connect_accepts_websocket(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws)

        ws.accept.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_registers_client(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws)

        assert manager.get_connection_count() == 1

    @pytest.mark.asyncio
    async def test_connect_sends_confirmation_message(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws)

        # send_json called once for the connection confirmation
        ws.send_json.assert_awaited_once()
        msg = ws.send_json.call_args[0][0]
        assert msg["type"] == "connection"
        assert msg["status"] == "connected"
        assert "timestamp" in msg

    @pytest.mark.asyncio
    async def test_connect_multiple_clients(self):
        manager = AgentActivityWSManager()
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect(ws1)
        await manager.connect(ws2)

        assert manager.get_connection_count() == 2


# ---------------------------------------------------------------------------
# Tests: disconnect
# ---------------------------------------------------------------------------


class TestDisconnect:
    """Tests for the disconnect method."""

    @pytest.mark.asyncio
    async def test_disconnect_removes_client(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws)
        assert manager.get_connection_count() == 1

        await manager.disconnect(ws)
        assert manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_disconnect_unknown_client_is_noop(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        # Disconnecting a client that was never connected should not raise
        await manager.disconnect(ws)
        assert manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_disconnect_one_of_many(self):
        manager = AgentActivityWSManager()
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect(ws1)
        await manager.connect(ws2)
        assert manager.get_connection_count() == 2

        await manager.disconnect(ws1)
        assert manager.get_connection_count() == 1


# ---------------------------------------------------------------------------
# Tests: broadcast_activity
# ---------------------------------------------------------------------------


class TestBroadcastActivity:
    """Tests for the broadcast_activity method."""

    @pytest.mark.asyncio
    async def test_broadcast_activity_sends_to_all_clients(self):
        manager = AgentActivityWSManager()
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect(ws1)
        await manager.connect(ws2)

        data = {"agent_id": "test_agent", "action_type": "query"}
        count = await manager.broadcast_activity(data)

        assert count == 2
        # Each client received the connection message + the broadcast
        assert ws1.send_json.await_count == 2
        assert ws2.send_json.await_count == 2

    @pytest.mark.asyncio
    async def test_broadcast_activity_wraps_with_agent_activity_type(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws)

        data = {"agent_id": "test_agent", "action_type": "mutation"}
        await manager.broadcast_activity(data)

        # Second call is the broadcast (first is connection confirmation)
        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "agent_activity"
        assert msg["data"] == data
        assert "timestamp" in msg

    @pytest.mark.asyncio
    async def test_broadcast_activity_no_clients_returns_zero(self):
        manager = AgentActivityWSManager()

        count = await manager.broadcast_activity({"agent_id": "test"})
        assert count == 0

    @pytest.mark.asyncio
    async def test_broadcast_activity_removes_dead_clients(self):
        """Dead clients should be cleaned up during broadcast."""
        manager = AgentActivityWSManager()
        ws_alive = _make_websocket()
        ws_dead = _make_websocket(fail_send=True)

        await manager.connect(ws_alive)
        # Manually add the dead client with metadata dict (since connect sends a message which would fail)
        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "",
            "pending_count": 0,
        }

        data = {"agent_id": "test_agent"}
        count = await manager.broadcast_activity(data)

        assert count == 1  # Only the alive client received it
        assert manager.get_connection_count() == 1
        assert ws_dead not in manager._clients


# ---------------------------------------------------------------------------
# Tests: broadcast_approval_event
# ---------------------------------------------------------------------------


class TestBroadcastApprovalEvent:
    """Tests for the broadcast_approval_event method.

    Approval events are tenant-scoped (loading-plan-executor K9), so sockets
    connect with a tenant and the data carries it.
    """

    @pytest.mark.asyncio
    async def test_broadcast_approval_event_uses_event_type(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws, tenant_id="t1")

        data = {"action_id": "abc-123", "status": "pending", "tenant_id": "t1"}
        await manager.broadcast_approval_event("approval_created", data)

        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "approval_created"
        assert msg["data"] == data
        assert "timestamp" in msg

    @pytest.mark.asyncio
    async def test_broadcast_approval_event_sends_to_all(self):
        manager = AgentActivityWSManager()
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect(ws1, tenant_id="t1")
        await manager.connect(ws2, tenant_id="t1")

        data = {"action_id": "abc-123", "tenant_id": "t1"}
        count = await manager.broadcast_approval_event("approval_approved", data)

        assert count == 2

    @pytest.mark.asyncio
    async def test_broadcast_approval_event_removes_dead_clients(self):
        manager = AgentActivityWSManager()
        ws_alive = _make_websocket()
        ws_dead = _make_websocket(fail_send=True)

        await manager.connect(ws_alive, tenant_id="t1")
        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "t1",
            "pending_count": 0,
        }

        count = await manager.broadcast_approval_event(
            "approval_rejected", {"action_id": "x", "tenant_id": "t1"}
        )

        assert count == 1
        assert manager.get_connection_count() == 1
        assert ws_dead not in manager._clients


class TestTenantScopedBroadcasts:
    """T-U12b: approval and tenant-bearing activity events stay in their tenant."""

    async def _two_tenants(self):
        manager = AgentActivityWSManager()
        ws1, ws2 = _make_websocket(), _make_websocket()
        await manager.connect(ws1, tenant_id="t1")
        await manager.connect(ws2, tenant_id="t2")
        return manager, ws1, ws2

    @staticmethod
    def _received(ws):
        # Index 0 is the connection handshake.
        return [c[0][0] for c in ws.send_json.call_args_list[1:]]

    @pytest.mark.asyncio
    async def test_approval_event_reaches_only_its_tenant(self):
        manager, ws1, ws2 = await self._two_tenants()
        count = await manager.broadcast_approval_event(
            "approval_execution_updated",
            {"action_id": "a", "tenant_id": "t1", "execution_result": {"outcome": "applied"}},
        )
        assert count == 1
        assert [m["type"] for m in self._received(ws1)] == ["approval_execution_updated"]
        assert self._received(ws2) == []

    @pytest.mark.asyncio
    async def test_approval_event_without_tenant_is_dropped(self, caplog):
        manager, ws1, ws2 = await self._two_tenants()
        with caplog.at_level("WARNING", logger="Agents.agent_ws_manager"):
            count = await manager.broadcast_approval_event("approval_created", {"action_id": "a"})
        assert count == 0
        assert self._received(ws1) == [] and self._received(ws2) == []
        assert any("without tenant_id dropped" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_activity_with_tenant_is_scoped_and_without_reaches_all(self):
        manager, ws1, ws2 = await self._two_tenants()
        assert await manager.broadcast_activity({"action_type": "x", "tenant_id": "t2"}) == 1
        assert self._received(ws1) == []
        assert len(self._received(ws2)) == 1
        assert await manager.broadcast_activity({"action_type": "system"}) == 2
        assert len(self._received(ws1)) == 1
        assert len(self._received(ws2)) == 2

    @pytest.mark.asyncio
    async def test_tenant_broadcast_keeps_backpressure_and_dead_cleanup(self):
        manager = AgentActivityWSManager(max_pending_messages=1)
        busy, dead, ok = _make_websocket(), _make_websocket(fail_send=True), _make_websocket()
        await manager.connect(ok, tenant_id="t1")
        now = datetime.now(timezone.utc)
        manager._clients[busy] = {"connected_at": now, "last_send": None, "tenant_id": "t1", "pending_count": 1}
        manager._clients[dead] = {"connected_at": now, "last_send": None, "tenant_id": "t1", "pending_count": 0}

        count = await manager.broadcast_approval_event("approval_created", {"tenant_id": "t1"})

        assert count == 1
        assert dead not in manager._clients
        assert busy in manager._clients
        metrics = manager.get_metrics()
        assert metrics["messages_dropped_total"] == 1
        assert metrics["send_failures_total"] == 1
        busy.send_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_to_tenant_blank_tenant_reaches_nobody(self):
        manager, ws1, ws2 = await self._two_tenants()
        assert await manager.broadcast_to_tenant("", {"type": "x"}) == 0
        assert self._received(ws1) == [] and self._received(ws2) == []


# ---------------------------------------------------------------------------
# Tests: broadcast_event
# ---------------------------------------------------------------------------


class TestBroadcastEvent:
    """Tests for the generic broadcast_event method (tenant-scoped, OI-01)."""

    @pytest.mark.asyncio
    async def test_broadcast_event_delay_alert(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws, tenant_id="t1")

        data = {"job_id": "JOB-1", "reason": "no_alternative_available", "tenant_id": "t1"}
        await manager.broadcast_event("delay_alert", data)

        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "delay_alert"
        assert msg["data"] == data

    @pytest.mark.asyncio
    async def test_broadcast_event_fuel_alert(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws, tenant_id="t1")

        data = {"station_id": "S-12", "urgency": "critical", "tenant_id": "t1"}
        await manager.broadcast_event("fuel_alert", data)

        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "fuel_alert"
        assert msg["data"] == data

    @pytest.mark.asyncio
    async def test_broadcast_event_sla_breach(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws, tenant_id="t1")

        data = {"shipment_id": "SH-99", "rider_id": "R-5", "tenant_id": "t1"}
        await manager.broadcast_event("sla_breach", data)

        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "sla_breach"
        assert msg["data"] == data

    @pytest.mark.asyncio
    async def test_broadcast_event_without_tenant_reaches_nobody(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws, tenant_id="t1")

        count = await manager.broadcast_event("sla_breach", {"shipment_id": "SH-1"})

        assert count == 0
        assert ws.send_json.await_count == 1  # only the connection handshake

    @pytest.mark.asyncio
    async def test_broadcast_event_removes_dead_clients(self):
        manager = AgentActivityWSManager()
        ws_alive = _make_websocket()
        ws_dead = _make_websocket(fail_send=True)

        await manager.connect(ws_alive, tenant_id="t1")
        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "t1",
            "pending_count": 0,
        }

        count = await manager.broadcast_event(
            "sla_breach", {"shipment_id": "SH-1", "tenant_id": "t1"}
        )

        assert count == 1
        assert ws_dead not in manager._clients

    @pytest.mark.asyncio
    async def test_broadcast_event_no_clients_returns_zero(self):
        manager = AgentActivityWSManager()

        count = await manager.broadcast_event(
            "fuel_alert", {"station_id": "S-1", "tenant_id": "t1"}
        )
        assert count == 0


# ---------------------------------------------------------------------------
# Tests: shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    """Tests for the shutdown method."""

    @pytest.mark.asyncio
    async def test_shutdown_closes_all_connections(self):
        manager = AgentActivityWSManager()
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect(ws1)
        await manager.connect(ws2)
        assert manager.get_connection_count() == 2

        await manager.shutdown()

        assert manager.get_connection_count() == 0
        ws1.close.assert_awaited_once()
        ws2.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_close_errors_gracefully(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()
        ws.close = AsyncMock(side_effect=RuntimeError("already closed"))

        await manager.connect(ws)

        # Should not raise
        await manager.shutdown()
        assert manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_shutdown_empty_manager_is_noop(self):
        manager = AgentActivityWSManager()

        # Should not raise
        await manager.shutdown()
        assert manager.get_connection_count() == 0


# ---------------------------------------------------------------------------
# Tests: get_connection_count
# ---------------------------------------------------------------------------


class TestGetConnectionCount:
    """Tests for the get_connection_count method."""

    @pytest.mark.asyncio
    async def test_initial_count_is_zero(self):
        manager = AgentActivityWSManager()
        assert manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_count_reflects_connections(self):
        manager = AgentActivityWSManager()
        ws1 = _make_websocket()
        ws2 = _make_websocket()
        ws3 = _make_websocket()

        await manager.connect(ws1)
        assert manager.get_connection_count() == 1

        await manager.connect(ws2)
        await manager.connect(ws3)
        assert manager.get_connection_count() == 3

        await manager.disconnect(ws2)
        assert manager.get_connection_count() == 2


# ---------------------------------------------------------------------------
# Tests: get_agent_ws_manager singleton
# ---------------------------------------------------------------------------


class TestGetAgentWSManager:
    """Tests for the module-level singleton factory."""

    def test_returns_instance(self):
        with patch("Agents.agent_ws_manager._agent_ws_manager", None):
            mgr = get_agent_ws_manager()
            assert isinstance(mgr, AgentActivityWSManager)

    def test_returns_same_instance(self):
        with patch("Agents.agent_ws_manager._agent_ws_manager", None):
            mgr1 = get_agent_ws_manager()
            mgr2 = get_agent_ws_manager()
            assert mgr1 is mgr2
