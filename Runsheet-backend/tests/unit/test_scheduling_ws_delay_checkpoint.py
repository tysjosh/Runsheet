"""
Checkpoint 10: Verify WebSocket and delay detection for Logistics Scheduling.

Definition of Done:
  - WebSocket client at /ws/scheduling receives job_created event within 5s of POST /scheduling/jobs
  - WebSocket client receives status_changed event within 5s of PATCH /scheduling/jobs/{id}/status
  - WebSocket client receives delay_alert when a job's estimated_arrival passes
  - WebSocket heartbeat received within 30s of connection
  - Delay detection marks overdue jobs as delayed within one check interval

Validates: Requirements 7.3, 7.4, 9.1-9.6
"""

import asyncio
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from scheduling.websocket.scheduling_ws import (
    SchedulingWebSocketManager,
    HEARTBEAT_INTERVAL_SECONDS,
    VALID_SUBSCRIPTIONS,
)
from scheduling.services.delay_detection_service import DelayDetectionService


# ---------------------------------------------------------------------------
# Helpers: Fake WebSocket
# ---------------------------------------------------------------------------

class FakeWebSocket:
    """Minimal WebSocket stub that records sent messages."""

    def __init__(self):
        self.messages: list[dict] = []
        self.accepted = False
        self.closed = False
        self.query_params = {}

    async def accept(self):
        self.accepted = True

    async def send_json(self, data: dict):
        self.messages.append(data)

    async def close(self, code=1000, reason=""):
        self.closed = True

    async def receive_text(self):
        # Block forever (simulates idle client)
        await asyncio.sleep(3600)
        return ""


def _make_es_mock():
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    es.update_document = AsyncMock(return_value={"result": "updated"})
    return es


def _overdue_job_hit(job_id="JOB_1", minutes_overdue=30):
    eta = datetime.now(timezone.utc) - timedelta(minutes=minutes_overdue)
    return {
        "_source": {
            "job_id": job_id,
            "job_type": "cargo_transport",
            "status": "in_progress",
            "tenant_id": "tenant_a",
            "asset_assigned": "TRUCK_001",
            "origin": "Port Harcourt",
            "destination": "Lagos",
            "estimated_arrival": eta.isoformat(),
            "delayed": False,
            "delay_duration_minutes": 0,
            "scheduled_time": (eta - timedelta(hours=4)).isoformat(),
        }
    }


# ---------------------------------------------------------------------------
# 1. WebSocket receives job_created event after broadcast
# ---------------------------------------------------------------------------

class TestWebSocketReceivesJobCreated:
    """Verify that a connected WS client receives job_created events."""

    @pytest.mark.asyncio
    async def test_client_receives_job_created_broadcast(self):
        """A subscribed client should receive a job_created message when
        broadcast_job_created is called.

        Validates: Req 9.1, 9.2
        """
        mgr = SchedulingWebSocketManager()
        ws = FakeWebSocket()

        await mgr.connect(ws, subscriptions=["job_created"])

        # The connect sends a "connection" message first
        assert len(ws.messages) == 1
        assert ws.messages[0]["type"] == "connection"

        job_data = {
            "job_id": "JOB_100",
            "job_type": "cargo_transport",
            "status": "scheduled",
            "tenant_id": "tenant_a",
        }
        sent = await mgr.broadcast_job_created(job_data)

        assert sent == 1
        # Second message should be the job_created broadcast
        assert len(ws.messages) == 2
        msg = ws.messages[1]
        assert msg["type"] == "job_created"
        assert msg["data"]["job_id"] == "JOB_100"
        assert "timestamp" in msg

    @pytest.mark.asyncio
    async def test_unsubscribed_client_does_not_receive_job_created(self):
        """A client subscribed only to delay_alert should NOT receive
        job_created events.

        Validates: Req 9.3
        """
        mgr = SchedulingWebSocketManager()
        ws = FakeWebSocket()

        await mgr.connect(ws, subscriptions=["delay_alert"])

        sent = await mgr.broadcast_job_created({"job_id": "JOB_101"})

        assert sent == 0
        # Only the connection message, no broadcast
        assert len(ws.messages) == 1

    @pytest.mark.asyncio
    async def test_client_with_no_filter_receives_all_events(self):
        """A client with no subscription filter should receive all event types.

        Validates: Req 9.3
        """
        mgr = SchedulingWebSocketManager()
        ws = FakeWebSocket()

        await mgr.connect(ws, subscriptions=None)

        await mgr.broadcast_job_created({"job_id": "JOB_102"})
        await mgr.broadcast("status_changed", {"job_id": "JOB_102"})
        await mgr.broadcast("delay_alert", {"job_id": "JOB_102"})

        # connection + 3 broadcasts
        assert len(ws.messages) == 4


# ---------------------------------------------------------------------------
# 2. WebSocket receives status_changed event after status transition
# ---------------------------------------------------------------------------

class TestWebSocketReceivesStatusChanged:
    """Verify that a connected WS client receives status_changed events."""

    @pytest.mark.asyncio
    async def test_client_receives_status_changed_broadcast(self):
        """A subscribed client should receive a status_changed message
        with old_status and new_status fields.

        Validates: Req 9.2
        """
        mgr = SchedulingWebSocketManager()
        ws = FakeWebSocket()

        await mgr.connect(ws, subscriptions=["status_changed"])

        sent = await mgr.broadcast_status_changed(
            job_data={"job_id": "JOB_200", "status": "in_progress"},
            old_status="assigned",
            new_status="in_progress",
        )

        assert sent == 1
        msg = ws.messages[1]  # index 0 is connection
        assert msg["type"] == "status_changed"
        assert msg["data"]["job_id"] == "JOB_200"
        assert msg["data"]["old_status"] == "assigned"
        assert msg["data"]["new_status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_multiple_clients_receive_status_changed(self):
        """Multiple subscribed clients should all receive the broadcast.

        Validates: Req 9.2
        """
        mgr = SchedulingWebSocketManager()
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()

        await mgr.connect(ws1, subscriptions=["status_changed"])
        await mgr.connect(ws2, subscriptions=["status_changed"])

        sent = await mgr.broadcast_status_changed(
            job_data={"job_id": "JOB_201"},
            old_status="scheduled",
            new_status="assigned",
        )

        assert sent == 2
        assert ws1.messages[1]["type"] == "status_changed"
        assert ws2.messages[1]["type"] == "status_changed"


# ---------------------------------------------------------------------------
# 3. WebSocket receives delay_alert when estimated_arrival passes
# ---------------------------------------------------------------------------

class TestWebSocketReceivesDelayAlert:
    """Verify that delay_alert is broadcast when a job becomes delayed."""

    @pytest.mark.asyncio
    async def test_delay_detection_broadcasts_delay_alert(self):
        """check_delays should broadcast delay_alert via WebSocket for
        each newly delayed job.

        Validates: Req 7.4, 9.4
        """
        es = _make_es_mock()
        ws_mock = AsyncMock()
        ws_mock.broadcast = AsyncMock()

        hit = _overdue_job_hit(minutes_overdue=45)
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [hit], "total": {"value": 1}}}
        )

        svc = DelayDetectionService(es_service=es, ws_manager=ws_mock)
        result = await svc.check_delays()

        assert len(result) == 1
        ws_mock.broadcast.assert_awaited_once()
        call_args = ws_mock.broadcast.await_args
        assert call_args.args[0] == "delay_alert"
        payload = call_args.args[1]
        assert payload["job_id"] == "JOB_1"
        assert payload["delay_duration_minutes"] >= 45

    @pytest.mark.asyncio
    async def test_ws_client_receives_delay_alert_broadcast(self):
        """A WebSocket client subscribed to delay_alert should receive
        the delay_alert message from the manager.

        Validates: Req 9.4
        """
        mgr = SchedulingWebSocketManager()
        ws = FakeWebSocket()

        await mgr.connect(ws, subscriptions=["delay_alert"])

        sent = await mgr.broadcast_delay_alert(
            job_data={
                "job_id": "JOB_300",
                "job_type": "cargo_transport",
                "asset_assigned": "TRUCK_001",
                "origin": "Port Harcourt",
                "destination": "Lagos",
                "estimated_arrival": "2026-03-06T10:00:00+00:00",
                "tenant_id": "tenant_a",
            },
            delay_minutes=60,
        )

        assert sent == 1
        msg = ws.messages[1]
        assert msg["type"] == "delay_alert"
        assert msg["data"]["job_id"] == "JOB_300"
        assert msg["data"]["delay_duration_minutes"] == 60
        assert msg["data"]["asset_assigned"] == "TRUCK_001"

    @pytest.mark.asyncio
    async def test_delay_detection_without_ws_manager_does_not_crash(self):
        """check_delays should work gracefully when ws_manager is None.

        Validates: Req 7.3
        """
        es = _make_es_mock()
        hit = _overdue_job_hit(minutes_overdue=20)
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [hit], "total": {"value": 1}}}
        )

        svc = DelayDetectionService(es_service=es, ws_manager=None)
        result = await svc.check_delays()

        assert len(result) == 1
        assert result[0]["delayed"] is True


# ---------------------------------------------------------------------------
# 4. WebSocket heartbeat received within 30s of connection
# ---------------------------------------------------------------------------

class TestWebSocketHeartbeat:
    """Verify heartbeat messages are sent at the configured interval."""

    def test_heartbeat_interval_is_30_seconds(self):
        """The heartbeat interval constant should be 30 seconds.

        Validates: Req 9.6
        """
        assert HEARTBEAT_INTERVAL_SECONDS == 30

    @pytest.mark.asyncio
    async def test_heartbeat_sent_after_interval(self):
        """After the heartbeat interval, connected clients should receive
        a heartbeat message.

        Validates: Req 9.6
        """
        mgr = SchedulingWebSocketManager()
        ws = FakeWebSocket()

        await mgr.connect(ws, subscriptions=None)

        # Manually trigger the heartbeat by patching the sleep interval
        # to be very short so the test doesn't wait 30s
        original_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            if seconds == HEARTBEAT_INTERVAL_SECONDS:
                await original_sleep(0.01)
            else:
                await original_sleep(seconds)

        with patch("scheduling.websocket.scheduling_ws.asyncio.sleep", side_effect=fast_sleep):
            # Start heartbeat loop as a task
            task = asyncio.create_task(mgr._heartbeat_loop())
            # Give it time to send one heartbeat
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should have connection message + at least one heartbeat
        heartbeat_msgs = [m for m in ws.messages if m.get("type") == "heartbeat"]
        assert len(heartbeat_msgs) >= 1
        assert "timestamp" in heartbeat_msgs[0]

    @pytest.mark.asyncio
    async def test_stale_client_disconnected_after_missed_heartbeat(self):
        """A client that doesn't respond to heartbeat should be disconnected.

        Validates: Req 9.6
        """
        mgr = SchedulingWebSocketManager()
        ws = FakeWebSocket()

        await mgr.connect(ws, subscriptions=None)
        assert mgr.get_connection_count() == 1

        # Manually mark the client as pending (simulating missed heartbeat)
        async with mgr._lock:
            client = mgr._clients[ws]
            client.mark_pending()

        original_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            if seconds == HEARTBEAT_INTERVAL_SECONDS:
                await original_sleep(0.01)
            else:
                await original_sleep(seconds)

        with patch("scheduling.websocket.scheduling_ws.asyncio.sleep", side_effect=fast_sleep):
            task = asyncio.create_task(mgr._heartbeat_loop())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Stale client should have been removed
        assert mgr.get_connection_count() == 0


# ---------------------------------------------------------------------------
# 5. Delay detection marks overdue jobs as delayed within one check interval
# ---------------------------------------------------------------------------

class TestDelayDetectionMarksOverdueJobs:
    """Verify that check_delays correctly identifies and marks overdue jobs."""

    @pytest.mark.asyncio
    async def test_marks_single_overdue_job_as_delayed(self):
        """check_delays should set delayed=true and calculate delay_duration_minutes.

        Validates: Req 7.3
        """
        es = _make_es_mock()
        ws = AsyncMock()
        ws.broadcast = AsyncMock()

        hit = _overdue_job_hit(minutes_overdue=60)
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [hit], "total": {"value": 1}}}
        )

        svc = DelayDetectionService(es_service=es, ws_manager=ws)
        result = await svc.check_delays()

        assert len(result) == 1
        assert result[0]["delayed"] is True
        assert result[0]["delay_duration_minutes"] >= 60

        # Verify ES update was called
        es.update_document.assert_awaited_once()
        update_args = es.update_document.await_args.args
        assert update_args[0] == "jobs_current"
        assert update_args[1] == "JOB_1"
        assert update_args[2]["delayed"] is True

    @pytest.mark.asyncio
    async def test_marks_multiple_overdue_jobs(self):
        """check_delays should handle multiple overdue jobs in one pass.

        Validates: Req 7.3
        """
        es = _make_es_mock()
        ws = AsyncMock()
        ws.broadcast = AsyncMock()

        hits = [
            _overdue_job_hit(job_id="JOB_1", minutes_overdue=30),
            _overdue_job_hit(job_id="JOB_2", minutes_overdue=90),
        ]
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": hits, "total": {"value": 2}}}
        )

        svc = DelayDetectionService(es_service=es, ws_manager=ws)
        result = await svc.check_delays()

        assert len(result) == 2
        assert all(j["delayed"] is True for j in result)
        assert es.update_document.await_count == 2
        assert ws.broadcast.await_count == 2

    @pytest.mark.asyncio
    async def test_no_overdue_jobs_returns_empty(self):
        """check_delays should return empty list when no jobs are overdue.

        Validates: Req 7.3
        """
        es = _make_es_mock()
        ws = AsyncMock()
        ws.broadcast = AsyncMock()

        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [], "total": {"value": 0}}}
        )

        svc = DelayDetectionService(es_service=es, ws_manager=ws)
        result = await svc.check_delays()

        assert result == []
        es.update_document.assert_not_awaited()
        ws.broadcast.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_query_filters_only_in_progress_not_delayed(self):
        """check_delays should query for status=in_progress AND delayed=false
        AND estimated_arrival < now.

        Validates: Req 7.3
        """
        es = _make_es_mock()
        ws = AsyncMock()
        ws.broadcast = AsyncMock()

        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [], "total": {"value": 0}}}
        )

        svc = DelayDetectionService(es_service=es, ws_manager=ws)
        await svc.check_delays()

        query_body = es.search_documents.await_args.args[1]
        must_clauses = query_body["query"]["bool"]["must"]

        # Verify the three required filter clauses
        status_clause = next(
            (c for c in must_clauses if "term" in c and "status" in c.get("term", {})),
            None,
        )
        assert status_clause is not None
        assert status_clause["term"]["status"] == "in_progress"

        delayed_clause = next(
            (c for c in must_clauses if "term" in c and "delayed" in c.get("term", {})),
            None,
        )
        assert delayed_clause is not None
        assert delayed_clause["term"]["delayed"] is False

        eta_clause = next(
            (c for c in must_clauses if "range" in c and "estimated_arrival" in c.get("range", {})),
            None,
        )
        assert eta_clause is not None
        assert "lt" in eta_clause["range"]["estimated_arrival"]


# ---------------------------------------------------------------------------
# 6. WebSocket subscription filtering
# ---------------------------------------------------------------------------

class TestWebSocketSubscriptionFiltering:
    """Verify subscription-based message filtering works correctly."""

    @pytest.mark.asyncio
    async def test_valid_subscriptions_constant(self):
        """VALID_SUBSCRIPTIONS should contain the expected event types.

        Validates: Req 9.3
        """
        expected = {"job_created", "status_changed", "delay_alert", "cargo_update", "cargo_complete"}
        assert VALID_SUBSCRIPTIONS == expected

    @pytest.mark.asyncio
    async def test_cargo_update_only_sent_to_cargo_subscribers(self):
        """cargo_update should only be sent to clients subscribed to it.

        Validates: Req 9.3
        """
        mgr = SchedulingWebSocketManager()
        ws_cargo = FakeWebSocket()
        ws_status = FakeWebSocket()

        await mgr.connect(ws_cargo, subscriptions=["cargo_update"])
        await mgr.connect(ws_status, subscriptions=["status_changed"])

        sent = await mgr.broadcast_cargo_update("JOB_400", "ITEM_1", "delivered")

        assert sent == 1
        # ws_cargo got connection + cargo_update
        assert len(ws_cargo.messages) == 2
        assert ws_cargo.messages[1]["type"] == "cargo_update"
        # ws_status only got connection
        assert len(ws_status.messages) == 1

    @pytest.mark.asyncio
    async def test_cargo_complete_broadcast(self):
        """cargo_complete should be broadcast to subscribed clients.

        Validates: Req 6.6
        """
        mgr = SchedulingWebSocketManager()
        ws = FakeWebSocket()

        await mgr.connect(ws, subscriptions=["cargo_complete"])

        sent = await mgr.broadcast_cargo_complete(
            "JOB_500",
            {"job_type": "cargo_transport", "origin": "A", "destination": "B", "asset_assigned": "T1"},
        )

        assert sent == 1
        msg = ws.messages[1]
        assert msg["type"] == "cargo_complete"
        assert msg["data"]["job_id"] == "JOB_500"


# ---------------------------------------------------------------------------
# 7. WebSocket client message handling (pong / subscribe)
# ---------------------------------------------------------------------------

class TestWebSocketClientMessageHandling:
    """Verify that the manager handles incoming client messages."""

    @pytest.mark.asyncio
    async def test_pong_marks_client_alive(self):
        """A pong message should mark the client as alive.

        Validates: Req 9.6
        """
        mgr = SchedulingWebSocketManager()
        ws = FakeWebSocket()

        await mgr.connect(ws, subscriptions=None)

        # Mark pending (simulating heartbeat cycle)
        async with mgr._lock:
            client = mgr._clients[ws]
            client.mark_pending()
            assert not client.is_alive

        await mgr.handle_client_message(ws, json.dumps({"type": "pong"}))

        async with mgr._lock:
            client = mgr._clients[ws]
            assert client.is_alive

    @pytest.mark.asyncio
    async def test_subscribe_updates_subscriptions(self):
        """A subscribe message should update the client's subscription set.

        Validates: Req 9.3
        """
        mgr = SchedulingWebSocketManager()
        ws = FakeWebSocket()

        await mgr.connect(ws, subscriptions=["job_created"])

        await mgr.handle_client_message(
            ws,
            json.dumps({"type": "subscribe", "subscriptions": ["delay_alert", "status_changed"]}),
        )

        async with mgr._lock:
            client = mgr._clients[ws]
            assert client.subscriptions == {"delay_alert", "status_changed"}

        # Should have received a "subscribed" confirmation
        sub_msgs = [m for m in ws.messages if m.get("type") == "subscribed"]
        assert len(sub_msgs) == 1


# ---------------------------------------------------------------------------
# 8. Integration: JobService broadcasts via WebSocket manager
# ---------------------------------------------------------------------------

class TestJobServiceWebSocketIntegration:
    """Verify that JobService._broadcast_job_update calls the WS manager."""

    @pytest.mark.asyncio
    async def test_broadcast_calls_ws_manager(self):
        """_broadcast_job_update should call ws_manager.broadcast.

        Validates: Req 9.2
        """
        from scheduling.services.job_service import JobService

        es = _make_es_mock()
        svc = JobService(es_service=es, redis_url=None)

        ws_mgr = AsyncMock()
        ws_mgr.broadcast = AsyncMock()
        svc._ws_manager = ws_mgr

        await svc._broadcast_job_update("job_created", {"job_id": "JOB_600"})

        ws_mgr.broadcast.assert_awaited_once_with("job_created", {"job_id": "JOB_600"})

    @pytest.mark.asyncio
    async def test_broadcast_without_ws_manager_does_not_crash(self):
        """_broadcast_job_update should be a no-op when ws_manager is None.

        Validates: Req 9.2
        """
        from scheduling.services.job_service import JobService

        es = _make_es_mock()
        svc = JobService(es_service=es, redis_url=None)
        svc._ws_manager = None

        # Should not raise
        await svc._broadcast_job_update("job_created", {"job_id": "JOB_601"})

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_propagate(self):
        """If ws_manager.broadcast raises, the error should be caught.

        Validates: Req 9.2
        """
        from scheduling.services.job_service import JobService

        es = _make_es_mock()
        svc = JobService(es_service=es, redis_url=None)

        ws_mgr = AsyncMock()
        ws_mgr.broadcast = AsyncMock(side_effect=Exception("WS down"))
        svc._ws_manager = ws_mgr

        # Should not raise
        await svc._broadcast_job_update("job_created", {"job_id": "JOB_602"})
