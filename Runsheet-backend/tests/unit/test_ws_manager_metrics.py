"""
Per-manager tests verifying metrics emission and backpressure.

Tests each migrated manager (fleet, ops, scheduling, agent activity)
emits correct metrics and enforces backpressure via BaseWSManager.

Requirements: 6.1, 6.2, 6.6
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from websocket.connection_manager import ConnectionManager
from ops.websocket.ops_ws import OpsWebSocketManager
from scheduling.websocket.scheduling_ws import SchedulingWebSocketManager
from Agents.agent_ws_manager import AgentActivityWSManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_websocket(*, fail_send: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    if fail_send:
        ws.send_json = AsyncMock(side_effect=RuntimeError("connection closed"))
    else:
        ws.send_json = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# Fleet ConnectionManager
# ---------------------------------------------------------------------------


class TestFleetManagerMetrics:
    """Tests for ConnectionManager (fleet) metrics and backpressure."""

    @pytest.mark.asyncio
    async def test_fleet_extends_base_ws_manager(self):
        from websocket.base_ws_manager import BaseWSManager
        manager = ConnectionManager()
        assert isinstance(manager, BaseWSManager)

    @pytest.mark.asyncio
    async def test_fleet_manager_name(self):
        manager = ConnectionManager()
        assert manager.manager_name == "fleet"

    @pytest.mark.asyncio
    async def test_fleet_connect_increments_metrics(self):
        manager = ConnectionManager()
        ws = _make_websocket()

        await manager.connect(ws)

        metrics = manager.get_metrics()
        assert metrics["connections_total"] == 1
        assert metrics["active_connections"] == 1

    @pytest.mark.asyncio
    async def test_fleet_broadcast_location_update_metrics(self):
        manager = ConnectionManager()
        ws = _make_websocket()

        await manager.connect(ws)
        count = await manager.broadcast_location_update(
            truck_id="T-1", latitude=1.0, longitude=2.0
        )

        assert count == 1
        metrics = manager.get_metrics()
        assert metrics["messages_sent_total"] >= 1

    @pytest.mark.asyncio
    async def test_fleet_backpressure(self):
        manager = ConnectionManager(max_pending_messages=3)
        ws = _make_websocket()

        await manager.connect(ws)
        manager._clients[ws]["pending_count"] = 3

        count = await manager.broadcast({"type": "test"})

        assert count == 0
        assert manager.get_metrics()["messages_dropped_total"] == 1

    @pytest.mark.asyncio
    async def test_fleet_dead_client_cleanup(self):
        manager = ConnectionManager()
        ws_alive = _make_websocket()
        ws_dead = _make_websocket(fail_send=True)

        await manager.connect(ws_alive)
        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "",
            "pending_count": 0,
        }

        count = await manager.broadcast({"type": "test"})

        assert count == 1
        assert ws_dead not in manager._clients
        assert manager.get_metrics()["send_failures_total"] == 1

    @pytest.mark.asyncio
    async def test_fleet_send_heartbeat(self):
        manager = ConnectionManager()
        ws = _make_websocket()

        await manager.connect(ws)
        count = await manager.send_heartbeat()

        assert count == 1

    @pytest.mark.asyncio
    async def test_fleet_broadcast_batch_update(self):
        manager = ConnectionManager()
        ws = _make_websocket()

        await manager.connect(ws)
        count = await manager.broadcast_batch_update([{"truck_id": "T-1"}])

        assert count == 1


# ---------------------------------------------------------------------------
# Ops WebSocket Manager
# ---------------------------------------------------------------------------


class TestOpsManagerMetrics:
    """Tests for OpsWebSocketManager metrics and backpressure."""

    @pytest.mark.asyncio
    async def test_ops_extends_base_ws_manager(self):
        from websocket.base_ws_manager import BaseWSManager
        manager = OpsWebSocketManager()
        assert isinstance(manager, BaseWSManager)

    @pytest.mark.asyncio
    async def test_ops_manager_name(self):
        manager = OpsWebSocketManager()
        assert manager.manager_name == "ops"

    @pytest.mark.asyncio
    async def test_ops_connect_increments_metrics(self):
        manager = OpsWebSocketManager()
        ws = _make_websocket()

        await manager.connect(ws)

        metrics = manager.get_metrics()
        assert metrics["connections_total"] == 1
        assert metrics["active_connections"] == 1

    @pytest.mark.asyncio
    async def test_ops_broadcast_shipment_update_metrics(self):
        manager = OpsWebSocketManager()
        ws = _make_websocket()

        await manager.connect(ws)
        count = await manager.broadcast_shipment_update({"shipment_id": "S-1"})

        assert count == 1
        metrics = manager.get_metrics()
        assert metrics["messages_sent_total"] >= 1

    @pytest.mark.asyncio
    async def test_ops_backpressure(self):
        manager = OpsWebSocketManager(max_pending_messages=3)
        ws = _make_websocket()

        await manager.connect(ws)
        manager._clients[ws]["pending_count"] = 3

        count = await manager.broadcast_shipment_update({"shipment_id": "S-1"})

        assert count == 0
        assert manager.get_metrics()["messages_dropped_total"] == 1

    @pytest.mark.asyncio
    async def test_ops_dead_client_cleanup(self):
        manager = OpsWebSocketManager()
        ws_alive = _make_websocket()
        ws_dead = _make_websocket(fail_send=True)

        await manager.connect(ws_alive)
        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "",
            "pending_count": 0,
            "subscriptions": set(),
            "_alive": True,
        }

        count = await manager.broadcast_rider_update({"rider_id": "R-1"})

        assert count == 1
        assert ws_dead not in manager._clients

    @pytest.mark.asyncio
    async def test_ops_handshake_includes_manager_name(self):
        manager = OpsWebSocketManager()
        ws = _make_websocket()

        await manager.connect(ws)

        msg = ws.send_json.call_args[0][0]
        assert msg["type"] == "connection"
        assert msg["status"] == "connected"
        assert msg["manager"] == "ops"


# ---------------------------------------------------------------------------
# Scheduling WebSocket Manager
# ---------------------------------------------------------------------------


class TestSchedulingManagerMetrics:
    """Tests for SchedulingWebSocketManager metrics and backpressure."""

    @pytest.mark.asyncio
    async def test_scheduling_extends_base_ws_manager(self):
        from websocket.base_ws_manager import BaseWSManager
        manager = SchedulingWebSocketManager()
        assert isinstance(manager, BaseWSManager)

    @pytest.mark.asyncio
    async def test_scheduling_manager_name(self):
        manager = SchedulingWebSocketManager()
        assert manager.manager_name == "scheduling"

    @pytest.mark.asyncio
    async def test_scheduling_connect_increments_metrics(self):
        manager = SchedulingWebSocketManager()
        ws = _make_websocket()

        await manager.connect(ws)

        metrics = manager.get_metrics()
        assert metrics["connections_total"] == 1

    @pytest.mark.asyncio
    async def test_scheduling_broadcast_job_created_metrics(self):
        manager = SchedulingWebSocketManager()
        ws = _make_websocket()

        await manager.connect(ws)
        count = await manager.broadcast_job_created({"job_id": "J-1"})

        assert count == 1
        metrics = manager.get_metrics()
        assert metrics["messages_sent_total"] >= 1

    @pytest.mark.asyncio
    async def test_scheduling_backpressure(self):
        manager = SchedulingWebSocketManager(max_pending_messages=3)
        ws = _make_websocket()

        await manager.connect(ws)
        manager._clients[ws]["pending_count"] = 3

        count = await manager.broadcast_job_created({"job_id": "J-1"})

        assert count == 0
        assert manager.get_metrics()["messages_dropped_total"] == 1

    @pytest.mark.asyncio
    async def test_scheduling_dead_client_cleanup(self):
        manager = SchedulingWebSocketManager()
        ws_alive = _make_websocket()
        ws_dead = _make_websocket(fail_send=True)

        await manager.connect(ws_alive)
        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "",
            "pending_count": 0,
            "subscriptions": set(),
            "_alive": True,
        }

        count = await manager.broadcast_status_changed(
            {"job_id": "J-1"}, "pending", "in_progress"
        )

        assert count == 1
        assert ws_dead not in manager._clients

    @pytest.mark.asyncio
    async def test_scheduling_handshake_includes_manager_name(self):
        manager = SchedulingWebSocketManager()
        ws = _make_websocket()

        await manager.connect(ws)

        msg = ws.send_json.call_args[0][0]
        assert msg["manager"] == "scheduling"


# ---------------------------------------------------------------------------
# Agent Activity WebSocket Manager
# ---------------------------------------------------------------------------


class TestAgentActivityManagerMetrics:
    """Tests for AgentActivityWSManager metrics and backpressure."""

    @pytest.mark.asyncio
    async def test_agent_extends_base_ws_manager(self):
        from websocket.base_ws_manager import BaseWSManager
        manager = AgentActivityWSManager()
        assert isinstance(manager, BaseWSManager)

    @pytest.mark.asyncio
    async def test_agent_manager_name(self):
        manager = AgentActivityWSManager()
        assert manager.manager_name == "agent_activity"

    @pytest.mark.asyncio
    async def test_agent_connect_increments_metrics(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws)

        metrics = manager.get_metrics()
        assert metrics["connections_total"] == 1

    @pytest.mark.asyncio
    async def test_agent_broadcast_activity_metrics(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws)
        count = await manager.broadcast_activity({"agent_id": "test"})

        assert count == 1
        metrics = manager.get_metrics()
        assert metrics["messages_sent_total"] >= 1

    @pytest.mark.asyncio
    async def test_agent_backpressure(self):
        manager = AgentActivityWSManager(max_pending_messages=3)
        ws = _make_websocket()

        await manager.connect(ws)
        manager._clients[ws]["pending_count"] = 3

        count = await manager.broadcast_activity({"agent_id": "test"})

        assert count == 0
        assert manager.get_metrics()["messages_dropped_total"] == 1

    @pytest.mark.asyncio
    async def test_agent_dead_client_cleanup(self):
        manager = AgentActivityWSManager()
        ws_alive = _make_websocket()
        ws_dead = _make_websocket(fail_send=True)

        await manager.connect(ws_alive)
        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "",
            "pending_count": 0,
        }

        count = await manager.broadcast_event("fuel_alert", {"station_id": "S-1"})

        assert count == 1
        assert ws_dead not in manager._clients

    @pytest.mark.asyncio
    async def test_agent_handshake_includes_manager_name(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws)

        msg = ws.send_json.call_args[0][0]
        assert msg["manager"] == "agent_activity"

    @pytest.mark.asyncio
    async def test_agent_broadcast_approval_event(self):
        manager = AgentActivityWSManager()
        ws = _make_websocket()

        await manager.connect(ws)
        count = await manager.broadcast_approval_event(
            "approval_created", {"action_id": "abc"}
        )

        assert count == 1
