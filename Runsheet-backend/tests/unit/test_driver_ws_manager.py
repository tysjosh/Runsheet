"""
Unit tests for the Driver WebSocket Manager.

Tests the DriverWSManager class including connect_driver, disconnect,
send_to_driver, handle_driver_message, update_presence,
check_heartbeat_timeouts, and the module-level singleton.

Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
"""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

import driver.ws.driver_ws_manager as driver_ws_module
from driver.ws.driver_ws_manager import (
    DriverWSManager,
    get_driver_ws_manager,
    presence_doc_id,
    HEARTBEAT_TIMEOUT_SECONDS,
    SERVER_TO_DRIVER_EVENTS,
    DRIVER_TO_SERVER_EVENTS,
    REST_REDIRECTS,
    WS_OPERATION_NOT_SUPPORTED,
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
    """Create a mock ES service exposing only the async write surface.

    The presence writes go through ``index_document`` / ``update_document``
    (R10.14).  The synchronous ``client`` is deliberately absent: a fake that
    does not offer it fails loudly if a blocking call ever comes back.
    """
    es = MagicMock(spec=["index_document", "update_document"])
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    return es


def _index_call(es, position: int = 0):
    """Return ``(index, doc_id, document)`` for one ``index_document`` call."""
    args = es.index_document.await_args_list[position].args
    return args[0], args[1], args[2]


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

        es.index_document.assert_awaited_once()
        index, doc_id, body = _index_call(es)
        assert index == "driver_presence"
        assert doc_id == "tenant-1:driver-1"
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
        es.index_document.reset_mock()

        await manager.disconnect(ws)

        es.index_document.assert_awaited_once()
        _, doc_id, body = _index_call(es)
        assert doc_id == "tenant-1:driver-1"
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

    def test_driver_event_types_are_the_narrowed_vocabulary(self):
        """The channel accepts only the three non-state-changing types.

        Validates: Requirements 14.10, 14.11
        """
        assert DRIVER_TO_SERVER_EVENTS == {"heartbeat", "location_update", "ping"}

    def test_retired_types_map_to_their_rest_endpoints(self):
        """The three retired types each name the endpoint that performs them.

        Validates: Requirements 14.10, 14.11
        """
        assert REST_REDIRECTS == {
            "ack": "POST /api/driver/orders/{order_id}/status",
            "status_update": "POST /api/driver/orders/{order_id}/status",
            "exception": "POST /api/driver/orders/{order_id}/exceptions",
        }
        # The accepted vocabulary and the retired map are disjoint.
        assert not (DRIVER_TO_SERVER_EVENTS & set(REST_REDIRECTS))


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
        assert msg["error_code"] == WS_OPERATION_NOT_SUPPORTED
        assert "Unknown event type" in msg["message"]

    @pytest.mark.asyncio
    async def test_unregistered_websocket_ignored(self):
        """Messages from unregistered websockets should be silently ignored."""
        manager = DriverWSManager()
        ws = _make_websocket()

        raw = json.dumps({"type": "heartbeat"})
        # Should not raise
        await manager.handle_driver_message(ws, raw)


# ---------------------------------------------------------------------------
# Tests: the narrowed inbound vocabulary and the REST redirect frames
# ---------------------------------------------------------------------------


class TestInboundRejection:
    """Rejection is total, and the retired types name their REST endpoint.

    Validates: Requirements 14.10, 14.11
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "msg_type,payload,rest_endpoint",
        [
            (
                "ack",
                {"job_id": "JOB-1"},
                "POST /api/driver/orders/{order_id}/status",
            ),
            (
                "status_update",
                {"status": "en_route"},
                "POST /api/driver/orders/{order_id}/status",
            ),
            (
                "exception",
                {"exception_type": "road_closure"},
                "POST /api/driver/orders/{order_id}/exceptions",
            ),
        ],
    )
    async def test_retired_type_draws_a_rest_redirect_frame(
        self, msg_type, payload, rest_endpoint
    ):
        """Each retired type is answered with a directive, not a silent drop."""
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")
        es.index_document.reset_mock()

        await manager.handle_driver_message(
            ws, json.dumps({"type": msg_type, "data": payload})
        )

        # Connection confirmation + the rejection frame.
        assert ws.send_json.await_count == 2
        frame = ws.send_json.call_args_list[1][0][0]
        assert frame["type"] == "error"
        assert frame["error_code"] == "WS_OPERATION_NOT_SUPPORTED"
        assert frame["rest_endpoint"] == rest_endpoint
        assert rest_endpoint in frame["message"]
        assert msg_type in frame["message"]
        assert "timestamp" in frame

    @pytest.mark.asyncio
    @pytest.mark.parametrize("msg_type", ["ack", "status_update", "exception"])
    async def test_retired_type_changes_no_state(self, msg_type):
        """No presence write, and no heartbeat credit, on a retired type."""
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")
        es.index_document.reset_mock()
        before = manager.get_client_metadata(ws)["last_heartbeat"]

        await manager.handle_driver_message(
            ws, json.dumps({"type": msg_type, "data": {"status": "en_route"}})
        )

        es.index_document.assert_not_awaited()
        es.update_document.assert_not_awaited()
        assert manager.get_client_metadata(ws)["last_heartbeat"] == before

    @pytest.mark.asyncio
    async def test_unknown_type_changes_no_state(self):
        """Rejection is total: an unrecognised type writes nothing either."""
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")
        es.index_document.reset_mock()

        await manager.handle_driver_message(ws, json.dumps({"type": "wallet_topup"}))

        es.index_document.assert_not_awaited()
        es.update_document.assert_not_awaited()
        frame = ws.send_json.call_args_list[1][0][0]
        assert frame["type"] == "error"
        assert frame["error_code"] == "WS_OPERATION_NOT_SUPPORTED"
        # No REST endpoint performs an operation nobody named.
        assert frame["rest_endpoint"] is None

    @pytest.mark.asyncio
    async def test_heartbeat_still_writes_presence_and_acks(self):
        """The accepted types keep their behaviour, on the async client."""
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")
        es.index_document.reset_mock()

        await manager.handle_driver_message(ws, json.dumps({"type": "heartbeat"}))

        es.index_document.assert_awaited_once()
        _, doc_id, body = _index_call(es)
        assert doc_id == "tenant-1:driver-1"
        assert body["status"] == "online"
        assert ws.send_json.call_args_list[1][0][0]["type"] == "heartbeat_ack"

    @pytest.mark.asyncio
    async def test_location_update_still_merges_and_credits_heartbeat(self):
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")
        before = manager.get_client_metadata(ws)["last_heartbeat"]

        await manager.handle_driver_message(
            ws,
            json.dumps({
                "type": "location_update",
                "data": {"location": {"lat": 1.0, "lon": 2.0}},
            }),
        )

        es.update_document.assert_awaited_once()
        assert manager.get_client_metadata(ws)["last_heartbeat"] >= before

    @pytest.mark.asyncio
    async def test_ping_still_answers_pong_without_writing(self):
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")
        es.index_document.reset_mock()

        await manager.handle_driver_message(ws, json.dumps({"type": "ping"}))

        assert ws.send_json.call_args_list[1][0][0]["type"] == "pong"
        es.index_document.assert_not_awaited()
        es.update_document.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: update_presence
# ---------------------------------------------------------------------------


class TestUpdatePresence:
    """Tests for the update_presence method. Validates: Req 9.5, 10.14, 10.19"""

    @pytest.mark.asyncio
    async def test_update_presence_online(self):
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)

        await manager.update_presence("driver-1", "online", tenant_id="tenant-1")

        es.index_document.assert_awaited_once()
        index, doc_id, body = _index_call(es)
        assert index == "driver_presence"
        assert doc_id == "tenant-1:driver-1"
        assert body["status"] == "online"
        assert body["driver_id"] == "driver-1"
        assert body["tenant_id"] == "tenant-1"
        assert "connected_at" in body

    @pytest.mark.asyncio
    async def test_update_presence_offline(self):
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)

        await manager.update_presence("driver-1", "offline", tenant_id="tenant-1")

        _, _, body = _index_call(es)
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

        _, _, body = _index_call(es)
        assert body["last_location"] == location

    @pytest.mark.asyncio
    async def test_update_presence_no_es_is_noop(self):
        manager = DriverWSManager(es_service=None)

        # Should not raise
        await manager.update_presence("driver-1", "online")

    @pytest.mark.asyncio
    async def test_update_presence_es_error_handled(self):
        es = _make_es_service()
        es.index_document.side_effect = Exception("ES down")
        manager = DriverWSManager(es_service=es)

        # Should not raise
        await manager.update_presence("driver-1", "online")


# ---------------------------------------------------------------------------
# Tests: the presence document id and the async client (R10.14, R10.19)
# ---------------------------------------------------------------------------


class TestPresenceIsAsyncAndTenantScoped:
    """The presence writes are awaited, and keyed per tenant-driver pair.

    Validates: Requirements 10.14, 10.19
    """

    def test_presence_doc_id_is_the_composite_pair(self):
        """R10.19: the id names both halves of the pair, tenant first."""
        assert presence_doc_id("tenant-1", "driver-1") == "tenant-1:driver-1"

    @pytest.mark.asyncio
    async def test_two_tenants_sharing_a_driver_id_get_two_records(self):
        """R10.19: the same ``driver_id`` string in two tenants cannot collide.

        A real store keyed on the document id, so "two records" is observed as
        two surviving documents rather than as two calls.
        """
        store: dict[tuple[str, str], dict] = {}

        class _Store:
            async def index_document(self, index, doc_id, document):
                store[(index, doc_id)] = dict(document)
                return {"result": "created"}

            async def update_document(self, index, doc_id, partial_doc):
                existing = store.get((index, doc_id))
                if existing is None:
                    raise KeyError(doc_id)
                existing.update(dict(partial_doc))
                return {"result": "updated"}

        manager = DriverWSManager(es_service=_Store())

        await manager.update_presence("driver-1", "online", tenant_id="tenant-a")
        await manager.update_presence("driver-1", "offline", tenant_id="tenant-b")

        assert set(store) == {
            ("driver_presence", "tenant-a:driver-1"),
            ("driver_presence", "tenant-b:driver-1"),
        }
        assert store[("driver_presence", "tenant-a:driver-1")]["status"] == "online"
        assert store[("driver_presence", "tenant-b:driver-1")]["status"] == "offline"
        # One record per pair, not one per write: a re-heartbeat replaces.
        await manager.update_presence("driver-1", "online", tenant_id="tenant-b")
        assert len(store) == 2

    @pytest.mark.asyncio
    async def test_location_update_merges_on_the_composite_id(self):
        """R10.14: ``location_update`` awaits a partial merge, not a blocking call."""
        es = _make_es_service()
        manager = DriverWSManager(es_service=es)
        ws = _make_websocket()
        await manager.connect_driver(ws, "driver-1", "tenant-1")

        location = {"lat": 1.5, "lon": 2.5}
        await manager.handle_driver_message(
            ws, json.dumps({"type": "location_update", "data": {"location": location}})
        )

        es.update_document.assert_awaited_once()
        index, doc_id, partial = es.update_document.await_args.args
        assert index == "driver_presence"
        assert doc_id == "tenant-1:driver-1"
        assert partial["last_location"] == location
        assert "last_seen" in partial

    @pytest.mark.asyncio
    async def test_location_update_recreates_a_missing_presence_record(self):
        """A merge against no record falls back to a full write, not a loss."""
        es = _make_es_service()
        es.update_document.side_effect = Exception("document_missing_exception")
        manager = DriverWSManager(es_service=es)

        location = {"lat": 1.5, "lon": 2.5}
        await manager._update_driver_location("driver-1", location, "tenant-1")

        index, doc_id, body = _index_call(es)
        assert (index, doc_id) == ("driver_presence", "tenant-1:driver-1")
        assert body["last_location"] == location
        assert body["driver_id"] == "driver-1"
        assert body["tenant_id"] == "tenant-1"

    @pytest.mark.asyncio
    async def test_location_update_error_handled(self):
        """Both write attempts failing is logged, not raised at the socket."""
        es = _make_es_service()
        es.update_document.side_effect = Exception("ES down")
        es.index_document.side_effect = Exception("ES down")
        manager = DriverWSManager(es_service=es)

        # Should not raise
        await manager._update_driver_location("driver-1", {"lat": 1.0}, "tenant-1")

    def test_no_synchronous_client_call_remains_in_the_presence_paths(self):
        """R10.14: no ``self._es.client.*`` write survives in the module.

        A source scan, because the property is "no blocking call remains
        anywhere on this path" — a statement about the module, not about one
        call. Every presence write must be an awaited async-client call.
        """
        source = Path(driver_ws_module.__file__).read_text(encoding="utf-8")

        assert "self._es.client" not in source
        assert "await self._es.index_document(" in source
        assert "await self._es.update_document(" in source


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
