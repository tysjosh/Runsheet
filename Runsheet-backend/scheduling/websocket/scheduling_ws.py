"""
WebSocket manager for scheduling live updates.

Manages client connections with subscription-based filtering for
job_created, status_changed, delay_alert, and cargo_update event types.
Sends heartbeat every 30 seconds and disconnects stale clients.

Validates:
- Requirement 9.1: /ws/scheduling WebSocket endpoint
- Requirement 9.3: Subscription filters by event type
- Requirement 9.6: Heartbeat every 30s, stale client detection
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)

VALID_SUBSCRIPTIONS = {
    "job_created",
    "status_changed",
    "delay_alert",
    "cargo_update",
    "cargo_complete",
}
HEARTBEAT_INTERVAL_SECONDS = 30


class _ClientConnection:
    """Tracks a single WebSocket client with its subscriptions and liveness."""

    __slots__ = ("websocket", "subscriptions", "_alive")

    def __init__(self, websocket: WebSocket, subscriptions: Set[str]):
        self.websocket = websocket
        self.subscriptions = subscriptions
        self._alive = True

    def mark_alive(self) -> None:
        self._alive = True

    def mark_pending(self) -> None:
        self._alive = False

    @property
    def is_alive(self) -> bool:
        return self._alive


class SchedulingWebSocketManager:
    """
    Manages scheduling WebSocket connections with subscription filtering,
    heartbeat keep-alive, and stale client detection.

    Validates: Req 9.1, 9.3, 9.6
    """

    def __init__(self) -> None:
        self._clients: Dict[WebSocket, _ClientConnection] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(
        self,
        websocket: WebSocket,
        subscriptions: Optional[List[str]] = None,
    ) -> None:
        """Accept a WebSocket connection and register it with subscriptions.

        If *subscriptions* is None or empty the client receives all event types.
        Invalid subscription names are silently ignored.

        Validates: Req 9.1, 9.3
        """
        await websocket.accept()

        valid_subs: Set[str] = set()
        if subscriptions:
            valid_subs = {s for s in subscriptions if s in VALID_SUBSCRIPTIONS}
        # Empty set means "subscribe to everything"

        client = _ClientConnection(websocket, valid_subs)

        async with self._lock:
            self._clients[websocket] = client

        logger.info(
            "Scheduling WebSocket client connected. subscriptions=%s total=%d",
            valid_subs or "all",
            len(self._clients),
        )

        await self._send_to_client(websocket, {
            "type": "connection",
            "status": "connected",
            "subscriptions": sorted(valid_subs) if valid_subs else sorted(VALID_SUBSCRIPTIONS),
            "message": "Connected to scheduling live updates",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Start the heartbeat loop if not already running
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a client connection."""
        async with self._lock:
            self._clients.pop(websocket, None)

        logger.info(
            "Scheduling WebSocket client disconnected. total=%d",
            len(self._clients),
        )

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast(self, event_type: str, data: dict) -> int:
        """Send data to every client whose subscriptions include event_type.

        This is the generic broadcast method called by services via
        ``_broadcast_job_update`` and ``_broadcast_cargo_complete`` stubs.

        Validates: Req 9.2, 9.3

        Returns:
            Number of clients that successfully received the message.
        """
        message = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        async with self._lock:
            targets = [
                c for c in self._clients.values()
                if not c.subscriptions or event_type in c.subscriptions
            ]

        if not targets:
            return 0

        successful = 0
        disconnected: List[WebSocket] = []

        send_tasks = [self._send_to_client(c.websocket, message) for c in targets]
        results = await asyncio.gather(*send_tasks, return_exceptions=True)

        for client, result in zip(targets, results):
            if result is True:
                successful += 1
            else:
                disconnected.append(client.websocket)

        if disconnected:
            async with self._lock:
                for ws in disconnected:
                    self._clients.pop(ws, None)
            logger.info(
                "Removed %d disconnected scheduling WS clients during broadcast",
                len(disconnected),
            )

        logger.debug(
            "Scheduling WS broadcast %s: %d/%d clients received",
            event_type,
            successful,
            len(targets),
        )
        return successful

    async def broadcast_job_created(self, job_data: dict) -> int:
        """Broadcast a job_created event. Validates: Req 9.2"""
        return await self.broadcast("job_created", job_data)

    async def broadcast_status_changed(
        self, job_data: dict, old_status: str, new_status: str
    ) -> int:
        """Broadcast a status_changed event. Validates: Req 9.2"""
        payload = {**job_data, "old_status": old_status, "new_status": new_status}
        return await self.broadcast("status_changed", payload)

    async def broadcast_delay_alert(
        self, job_data: dict, delay_minutes: int
    ) -> int:
        """Broadcast a delay_alert event. Validates: Req 9.4"""
        payload = {
            "job_id": job_data.get("job_id"),
            "job_type": job_data.get("job_type"),
            "asset_assigned": job_data.get("asset_assigned"),
            "origin": job_data.get("origin"),
            "destination": job_data.get("destination"),
            "estimated_arrival": job_data.get("estimated_arrival"),
            "delay_duration_minutes": delay_minutes,
            "tenant_id": job_data.get("tenant_id"),
        }
        return await self.broadcast("delay_alert", payload)

    async def broadcast_cargo_update(
        self, job_id: str, item_id: str, new_status: str
    ) -> int:
        """Broadcast a cargo_update event. Validates: Req 9.3"""
        return await self.broadcast("cargo_update", {
            "job_id": job_id,
            "item_id": item_id,
            "item_status": new_status,
        })

    async def broadcast_cargo_complete(
        self, job_id: str, job_data: dict
    ) -> int:
        """Broadcast a cargo_complete event. Validates: Req 6.6"""
        return await self.broadcast("cargo_complete", {
            "job_id": job_id,
            "job_type": job_data.get("job_type"),
            "origin": job_data.get("origin"),
            "destination": job_data.get("destination"),
            "asset_assigned": job_data.get("asset_assigned"),
        })

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _send_to_client(self, websocket: WebSocket, data: dict) -> bool:
        """Send JSON to a single client. Returns True on success."""
        try:
            await websocket.send_json(data)
            return True
        except Exception as exc:
            logger.warning("Failed to send to scheduling WS client: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Heartbeat & stale detection  (Req 9.6)
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Periodically send heartbeat messages and prune stale clients.

        Cycle:
        1. Mark all clients as *pending* (not yet responded).
        2. Send ``{"type": "heartbeat"}`` to every client.
        3. Wait ``HEARTBEAT_INTERVAL_SECONDS``.
        4. Any client still marked *pending* is considered stale and disconnected.

        Validates: Req 9.6
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

                async with self._lock:
                    clients = list(self._clients.values())

                if not clients:
                    break

                # 1. Disconnect stale clients from previous cycle
                stale: List[WebSocket] = []
                for c in clients:
                    if not c.is_alive:
                        stale.append(c.websocket)
                    else:
                        c.mark_pending()

                if stale:
                    async with self._lock:
                        for ws in stale:
                            self._clients.pop(ws, None)
                    for ws in stale:
                        try:
                            await ws.close(code=1000, reason="stale")
                        except Exception:
                            pass
                    logger.info(
                        "Disconnected %d stale scheduling WS clients",
                        len(stale),
                    )

                # 2. Send heartbeat to remaining clients
                heartbeat_msg = {
                    "type": "heartbeat",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

                async with self._lock:
                    remaining = list(self._clients.values())

                for c in remaining:
                    ok = await self._send_to_client(c.websocket, heartbeat_msg)
                    if ok:
                        c.mark_alive()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Scheduling WS heartbeat loop error: %s", exc)

    # ------------------------------------------------------------------
    # Client message handling
    # ------------------------------------------------------------------

    async def handle_client_message(self, websocket: WebSocket, raw: str) -> None:
        """Process an incoming text message from a client.

        Supported message types:
        - ``pong``: marks the client as alive (heartbeat response).
        - ``subscribe``: updates the client's subscription set.
        """
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type = message.get("type")

        if msg_type == "pong":
            async with self._lock:
                client = self._clients.get(websocket)
            if client:
                client.mark_alive()

        elif msg_type == "subscribe":
            new_subs = message.get("subscriptions", [])
            valid = {s for s in new_subs if s in VALID_SUBSCRIPTIONS}
            async with self._lock:
                client = self._clients.get(websocket)
            if client:
                client.subscriptions = valid
                await self._send_to_client(websocket, {
                    "type": "subscribed",
                    "subscriptions": sorted(valid) if valid else sorted(VALID_SUBSCRIPTIONS),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_connection_count(self) -> int:
        """Return the number of active connections."""
        return len(self._clients)

    async def shutdown(self) -> None:
        """Cancel heartbeat and close all connections."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            for ws in list(self._clients.keys()):
                try:
                    await ws.close(code=1000, reason="shutdown")
                except Exception:
                    pass
            self._clients.clear()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_scheduling_ws_manager: Optional[SchedulingWebSocketManager] = None


def get_scheduling_ws_manager() -> SchedulingWebSocketManager:
    """Return the global SchedulingWebSocketManager singleton."""
    global _scheduling_ws_manager
    if _scheduling_ws_manager is None:
        _scheduling_ws_manager = SchedulingWebSocketManager()
    return _scheduling_ws_manager
