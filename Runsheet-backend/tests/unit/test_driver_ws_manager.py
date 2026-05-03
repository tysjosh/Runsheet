"""
Unit tests for the Driver WebSocket Manager.

Tests the DriverWSManager class including connect_driver, disconnect,
send_to_driver, handle_driver_message, update_presence,
check_heartbeat_timeouts, and the module-level singleton.

Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from driver.ws.driver_ws_manager import (
    DriverWSManager,
    get_driver_ws_manager,
    HEARTBEAT_TIMEOUT_SECONDS,
    SERVER_TO_DRIVER_EVENTS,
    DRIVER_TO_SERVER_EVENTS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_websocket(*, fail_send: bool = False):
    """Create a mock WebSocket."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()

    if fail_send:
        ws.send_json = AsyncMock(side_effect=RuntimeError("connection closed"))
    else:
        ws.send_json = AsyncMock()

    return ws


def _make_es_service():
    """Create a mock ES service."""
    es = MagicMock()
    es.client = MagicMock()
    es.client.index = MagicMock()
    es.client.update = MagicMock()
    return es


# ---------------------------------------------------------------------------
# Tests: connect_driver
# ---------------------------------------------------------------------------


class TestConnectDriver:
    """Tests for the connect_driver method. Validates: Req 9.1, 9.2"""

    @pytest.mark.asyncio
    async def test_connect_driver_accepts_websocket(self):
        manager = DriverWSManager()
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")

        ws.accept.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_driver_registers_client(self):
        manager = DriverWSManager()
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")

        assert manager.get_connection_count() == 1

    @pytest.mark.asyncio
    async def test_connect_driver_stores_driver_mapping(self):
        manager = DriverWSManager()
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")

        assert manager.is_driver_connected("driver-1")
        assert "driver-1" in manager.get_connected_driver_ids()

    @pytest.mark.asyncio
    async def test_connect_driver_sends_confirmation(self):
        manager = DriverWSManager()
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")

        ws.send_json.assert_awaited_once()
        msg = ws.send_json.call_args[0][0]
        assert msg["type"] == "connection"
        assert msg["status"] == "connected"
        assert msg["manager"] == "driver"

    @pytest.mark.asyncio
    async def test_connect_driver_stores_metadata(self):
        manager = DriverWSManager()
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")

        meta = manager.get_client_metadata(ws)
        assert meta is not None
        assert meta["driver_id"] == "driver-1"
        assert meta["tenant_id"] == "tenant-1"
        assert "last_heartbeat" in meta

    @pytest.mark.asyncio
    async def test_connect_multiple_drivers(self):
        manager = DriverWSManager()
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect_driver(ws1, "driver-1", "tenant-1")
        await manager.connect_driver(ws2, "driver-2", "tenant-1")

        assert manager.get_connection_count() == 2
        assert manager.is_driver_connected("driver-1")
        assert manager.is_driver_connected("driver-2")

    @pytest.mark.asyncio
    async def test_connect_driver_updates_presence_online(self):
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")

        es.client.index.assert_called_once()
        call_kwargs = es.client.index.call_args
        assert call_kwargs[1]["index"] == "driver_presence"
        assert call_kwargs[1]["id"] == "driver-1"
        body = call_kwargs[1]["body"]
        assert body["status"] == "online"
        assert body["driver_id"] == "driver-1"


# ---------------------------------------------------------------------------
# Tests: disconnect
# ---------------------------------------------------------------------------


class TestDisconnect:
    """Tests for the disconnect method."""

    @pytest.mark.asyncio
    async def test_disconnect_removes_client(self):
        manager = DriverWSManager()
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")
        assert manager.get_connection_count() == 1

        await manager.disconnect(ws)
        assert manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_disconnect_removes_driver_mapping(self):
        manager = DriverWSManager()
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")
        assert manager.is_driver_connected("driver-1")

        await manager.disconnect(ws)
        assert not manager.is_driver_connected("driver-1")

    @pytest.mark.asyncio
    async def test_disconnect_updates_presence_offline(self):
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")
        es.client.index.reset_mock()

        await manager.disconnect(ws)

        es.client.index.assert_called_once()
        body = es.client.index.call_args[1]["body"]
        assert body["status"] == "offline"

    @pytest.mark.asyncio
    async def test_disconnect_unknown_client_is_noop(self):
        manager = DriverWSManager()
        ws = _make_websocket()

        await manager.disconnect(ws)
        assert manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_disconnect_one_of_many(self):
        manager = DriverWSManager()
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect_driver(ws1, "driver-1", "tenant-1")
        await manager.connect_driver(ws2, "driver-2", "tenant-1")

        await manager.disconnect(ws1)
        assert manager.get_connection_count() == 1
        assert not manager.is_driver_connected("driver-1")
        assert manager.is_driver_connected("driver-2")


# ---------------------------------------------------------------------------
# Tests: send_to_driver
# ---------------------------------------------------------------------------


class TestSendToDriver:
    """Tests for the send_to_driver method. Validates: Req 9.3"""

    @pytest.mark.asyncio
    async def test_send_to_connected_driver(self):
        manager = DriverWSManager()
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")

        event = {"type": "assignment", "data": {"job_id": "JOB-1"}}
        result = await manager.send_to_driver("driver-1", event)

        assert result is True
        # Connection confirmation + the event
        assert ws.send_json.await_count == 2

    @pytest.mark.asyncio
    async def test_send_to_disconnected_driver_returns_false(self):
        manager = DriverWSManager()

        event = {"type": "assignment", "data": {"job_id": "JOB-1"}}
        result = await manager.send_to_driver("driver-999", event)

        assert result is False

    @pytest.mark.asyncio
    async def test_send_adds_timestamp_if_missing(self):
        manager = DriverWSManager()
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")

        event = {"type": "assignment", "data": {"job_id": "JOB-1"}}
        await manager.send_to_driver("driver-1", event)

        sent_msg = ws.send_json.call_args_list[1][0][0]
        assert "timestamp" in sent_msg

    @pytest.mark.asyncio
    async def test_send_to_dead_driver_cleans_up(self):
        manager = DriverWSManager()
        ws = _make_websocket(fail_send=True)

        # Manually register the dead driver (bypass connect which would fail)
        manager._clients[ws] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "tenant-1",
            "pending_count": 0,
            "driver_id": "driver-dead",
            "last_heartbeat": datetime.now(timezone.utc),
        }
        manager._driver_connections["driver-dead"] = ws

        event = {"type": "message", "data": {"body": "hello"}}
        result = await manager.send_to_driver("driver-dead", event)

        assert result is False
        assert not manager.is_driver_connected("driver-dead")
        assert manager.get_connection_count() == 0

    @pytest.mark.asyncio
    async def test_send_respects_backpressure(self):
        manager = DriverWSManager(max_pending_messages=2)
        ws = _make_websocket()

        await manager.connect_driver(ws, "driver-1", "tenant-1")

        # Artificially set pending count to max
        meta = manager.get_client_metadata(ws)
        meta["pending_count"] = 2

        event = {"type": "message", "data": {"body": "hello"}}
        result = await manager.send_to_driver("driver-1", event)

        assert result is False


# ---------------------------------------------------------------------------
# Tests: server-to-driver event helpers
# ---------------------------------------------------------------------------


class TestServerToDriverEvents:
    """Tests for server-to-driver event helper methods. Validates: Req 9.3"""

    @pytest.mark.asyncio
    async def test_send_assignment(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        result = await manager.send_assignment("driver-1", {"job_id": "JOB-1"})

        assert result is True
        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "assignment"
        assert msg["data"]["job_id"] == "JOB-1"

    @pytest.mark.asyncio
    async def test_send_new_route(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        result = await manager.send_new_route("driver-1", {"route_id": "R-1"})

        assert result is True
        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "new_route"

    @pytest.mark.asyncio
    async def test_send_escalation(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        result = await manager.send_escalation("driver-1", {"severity": "critical"})

        assert result is True
        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "escalation"

    @pytest.mark.asyncio
    async def test_send_message(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        result = await manager.send_message("driver-1", {"body": "hello"})

        assert result is True
        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "message"

    @pytest.mark.asyncio
    async def test_send_assignment_revoked(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        result = await manager.send_assignment_revoked(
            "driver-1", {"job_id": "JOB-1", "new_driver_id": "driver-2"}
        )

        assert result is True
        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "assignment_revoked"

    def test_server_event_types_defined(self):
        """Verify all required server-to-driver event types are defined."""
        expected = {"assignment", "new_route", "escalation", "message", "assignment_revoked"}
        assert SERVER_TO_DRIVER_EVENTS == expected

    def test_driver_event_types_defined(self):
        """Verify all required driver-to-server event types are defined."""
        expected = {"ack", "status_update", "exception", "heartbeat", "location_update"}
        assert DRIVER_TO_SERVER_EVENTS == expected


# ---------------------------------------------------------------------------
# Tests: handle_driver_message
# ---------------------------------------------------------------------------


class TestHandleDriverMessage:
    """Tests for the handle_driver_message method. Validates: Req 9.4, 9.5"""

    @pytest.mark.asyncio
    async def test_heartbeat_updates_last_heartbeat(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        before = manager.get_client_metadata(ws)["last_heartbeat"]

        raw = json.dumps({"type": "heartbeat"})
        await manager.handle_driver_message(ws, raw)

        after = manager.get_client_metadata(ws)["last_heartbeat"]
        assert after >= before

    @pytest.mark.asyncio
    async def test_heartbeat_sends_ack(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        raw = json.dumps({"type": "heartbeat"})
        await manager.handle_driver_message(ws, raw)

        # Connection confirmation + heartbeat_ack
        assert ws.send_json.await_count == 2
        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "heartbeat_ack"

    @pytest.mark.asyncio
    async def test_location_update_updates_heartbeat(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        before = manager.get_client_metadata(ws)["last_heartbeat"]

        raw = json.dumps({
            "type": "location_update",
            "data": {"location": {"lat": 1.0, "lon": 2.0}},
        })
        await manager.handle_driver_message(ws, raw)

        after = manager.get_client_metadata(ws)["last_heartbeat"]
        assert after >= before

    @pytest.mark.asyncio
    async def test_ping_sends_pong(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        raw = json.dumps({"type": "ping"})
        await manager.handle_driver_message(ws, raw)

        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "pong"

    @pytest.mark.asyncio
    async def test_invalid_json_sends_error(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        await manager.handle_driver_message(ws, "not-json{{{")

        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "error"
        assert "Invalid JSON" in msg["message"]

    @pytest.mark.asyncio
    async def test_unknown_event_type_sends_error(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        raw = json.dumps({"type": "unknown_event"})
        await manager.handle_driver_message(ws, raw)

        msg = ws.send_json.call_args_list[1][0][0]
        assert msg["type"] == "error"
        assert "Unknown event type" in msg["message"]

    @pytest.mark.asyncio
    async def test_ack_event_handled(self):
        """ack events should be handled without error."""
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        raw = json.dumps({"type": "ack", "data": {"job_id": "JOB-1"}})
        await manager.handle_driver_message(ws, raw)

        # Only the connection confirmation should have been sent (no error)
        assert ws.send_json.await_count == 1

    @pytest.mark.asyncio
    async def test_status_update_event_handled(self):
        """status_update events should be handled without error."""
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        raw = json.dumps({"type": "status_update", "data": {"status": "en_route"}})
        await manager.handle_driver_message(ws, raw)

        assert ws.send_json.await_count == 1

    @pytest.mark.asyncio
    async def test_exception_event_handled(self):
        """exception events should be handled without error."""
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        raw = json.dumps({
            "type": "exception",
            "data": {"exception_type": "road_closure"},
        })
        await manager.handle_driver_message(ws, raw)

        assert ws.send_json.await_count == 1

    @pytest.mark.asyncio
    async def test_unregistered_websocket_ignored(self):
        """Messages from unregistered websockets should be silently ignored."""
        manager = DriverWSManager()
        ws = _make_websocket()

        raw = json.dumps({"type": "heartbeat"})
        # Should not raise
        await manager.handle_driver_message(ws, raw)


# ---------------------------------------------------------------------------
# Tests: update_presence
# ---------------------------------------------------------------------------


class TestUpdatePresence:
    """Tests for the update_presence method. Validates: Req 9.5"""

    @pytest.mark.asyncio
    async def test_update_presence_online(self):
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)

        await manager.update_presence("driver-1", "online", tenant_id="tenant-1")

        es.client.index.assert_called_once()
        call_kwargs = es.client.index.call_args[1]
        assert call_kwargs["index"] == "driver_presence"
        assert call_kwargs["id"] == "driver-1"
        body = call_kwargs["body"]
        assert body["status"] == "online"
        assert body["driver_id"] == "driver-1"
        assert body["tenant_id"] == "tenant-1"
        assert "connected_at" in body

    @pytest.mark.asyncio
    async def test_update_presence_offline(self):
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)

        await manager.update_presence("driver-1", "offline", tenant_id="tenant-1")

        body = es.client.index.call_args[1]["body"]
        assert body["status"] == "offline"
        assert "connected_at" not in body

    @pytest.mark.asyncio
    async def test_update_presence_with_location(self):
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)

        location = {"lat": 40.7128, "lon": -74.0060}
        await manager.update_presence(
            "driver-1", "online", tenant_id="tenant-1", location=location
        )

        body = es.client.index.call_args[1]["body"]
        assert body["last_location"] == location

    @pytest.mark.asyncio
    async def test_update_presence_no_es_is_noop(self):
        manager = DriverWSManager(es_service=None)

        # Should not raise
        await manager.update_presence("driver-1", "online")

    @pytest.mark.asyncio
    async def test_update_presence_es_error_handled(self):
        es = _make_es_service()
        es.client.index.side_effect = Exception("ES down")
        manager = DriverWSManager(es_service=es)

        # Should not raise
        await manager.update_presence("driver-1", "online")


# ---------------------------------------------------------------------------
# Tests: check_heartbeat_timeouts
# ---------------------------------------------------------------------------


class TestCheckHeartbeatTimeouts:
    """Tests for the check_heartbeat_timeouts method. Validates: Req 9.6"""

    @pytest.mark.asyncio
    async def test_no_timeout_when_recent_heartbeat(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        timed_out = await manager.check_heartbeat_timeouts()

        assert timed_out == []
        assert manager.get_connection_count() == 1

    @pytest.mark.asyncio
    async def test_timeout_marks_driver_offline(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        # Set last_heartbeat to well past the timeout
        meta = manager.get_client_metadata(ws)
        meta["last_heartbeat"] = datetime.now(timezone.utc) - timedelta(seconds=200)

        timed_out = await manager.check_heartbeat_timeouts()

        assert "driver-1" in timed_out
        assert manager.get_connection_count() == 0
        assert not manager.is_driver_connected("driver-1")

    @pytest.mark.asyncio
    async def test_timeout_closes_websocket(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        meta = manager.get_client_metadata(ws)
        meta["last_heartbeat"] = datetime.now(timezone.utc) - timedelta(seconds=200)

        await manager.check_heartbeat_timeouts()

        ws.close.assert_awaited_once()
        close_kwargs = ws.close.call_args[1]
        assert close_kwargs["code"] == 4002
        assert "Heartbeat timeout" in close_kwargs["reason"]

    @pytest.mark.asyncio
    async def test_timeout_only_affects_stale_drivers(self):
        manager = DriverWSManager()
        ws_fresh = _make_websocket()
        ws_stale = _make_websocket()

        await manager.connect_driver(ws_fresh, "driver-fresh", "tenant-1")
        await manager.connect_driver(ws_stale, "driver-stale", "tenant-1")

        # Make only one stale
        meta_stale = manager.get_client_metadata(ws_stale)
        meta_stale["last_heartbeat"] = datetime.now(timezone.utc) - timedelta(seconds=200)

        timed_out = await manager.check_heartbeat_timeouts()

        assert "driver-stale" in timed_out
        assert "driver-fresh" not in timed_out
        assert manager.get_connection_count() == 1
        assert manager.is_driver_connected("driver-fresh")

    @pytest.mark.asyncio
    async def test_timeout_handles_close_error(self):
        manager = DriverWSManager()
        ws = _make_websocket()
        ws.close = AsyncMock(side_effect=RuntimeError("already closed"))
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        meta = manager.get_client_metadata(ws)
        meta["last_heartbeat"] = datetime.now(timezone.utc) - timedelta(seconds=200)

        # Should not raise
        timed_out = await manager.check_heartbeat_timeouts()
        assert "driver-1" in timed_out

    @pytest.mark.asyncio
    async def test_empty_manager_returns_empty(self):
        manager = DriverWSManager()

        timed_out = await manager.check_heartbeat_timeouts()
        assert timed_out == []

    def test_heartbeat_timeout_constant(self):
        """Verify the heartbeat timeout is 120 seconds per Req 9.6."""
        assert HEARTBEAT_TIMEOUT_SECONDS == 120


# ---------------------------------------------------------------------------
# Tests: broadcast_to_all_drivers
# ---------------------------------------------------------------------------


class TestBroadcastToAllDrivers:
    """Tests for the broadcast_to_all_drivers method."""

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all(self):
        manager = DriverWSManager()
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect_driver(ws1, "driver-1", "tenant-1")
        await manager.connect_driver(ws2, "driver-2", "tenant-1")

        event = {"type": "escalation", "data": {"severity": "critical"}}
        count = await manager.broadcast_to_all_drivers(event)

        assert count == 2

    @pytest.mark.asyncio
    async def test_broadcast_no_clients_returns_zero(self):
        manager = DriverWSManager()

        count = await manager.broadcast_to_all_drivers(
            {"type": "escalation", "data": {}}
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_broadcast_removes_dead_clients(self):
        manager = DriverWSManager()
        ws_alive = _make_websocket()
        ws_dead = _make_websocket(fail_send=True)

        await manager.connect_driver(ws_alive, "driver-alive", "tenant-1")
        # Manually add dead client
        manager._clients[ws_dead] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "tenant-1",
            "pending_count": 0,
            "driver_id": "driver-dead",
            "last_heartbeat": datetime.now(timezone.utc),
        }
        manager._driver_connections["driver-dead"] = ws_dead

        event = {"type": "escalation", "data": {}}
        count = await manager.broadcast_to_all_drivers(event)

        assert count == 1
        assert ws_dead not in manager._clients


# ---------------------------------------------------------------------------
# Tests: shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    """Tests for the shutdown method."""

    @pytest.mark.asyncio
    async def test_shutdown_closes_all_connections(self):
        manager = DriverWSManager()
        ws1 = _make_websocket()
        ws2 = _make_websocket()

        await manager.connect_driver(ws1, "driver-1", "tenant-1")
        await manager.connect_driver(ws2, "driver-2", "tenant-1")

        await manager.shutdown()

        assert manager.get_connection_count() == 0
        ws1.close.assert_awaited_once()
        ws2.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_empty_manager_is_noop(self):
        manager = DriverWSManager()

        await manager.shutdown()
        assert manager.get_connection_count() == 0


# ---------------------------------------------------------------------------
# Tests: get_driver_ws_manager singleton
# ---------------------------------------------------------------------------


class TestGetDriverWSManager:
    """Tests for the module-level singleton factory."""

    def test_returns_instance(self):
        with patch("driver.ws.driver_ws_manager._driver_ws_manager", None):
            with patch("driver.ws.driver_ws_manager._container", None):
                mgr = get_driver_ws_manager()
                assert isinstance(mgr, DriverWSManager)

    def test_returns_same_instance(self):
        with patch("driver.ws.driver_ws_manager._driver_ws_manager", None):
            with patch("driver.ws.driver_ws_manager._container", None):
                mgr1 = get_driver_ws_manager()
                mgr2 = get_driver_ws_manager()
                assert mgr1 is mgr2
