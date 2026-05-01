"""
WebSocket manager for scheduling live updates.

Manages client connections with subscription-based filtering for
job_created, status_changed, delay_alert, and cargo_update event types.
Sends heartbeat every 30 seconds and disconnects stale clients.

Extends BaseWSManager for consistent lifecycle metrics and backpressure.

Validates:
- Requirement 9.1: /ws/scheduling WebSocket endpoint
- Requirement 9.3: Subscription filters by event type
- Requirement 9.6: Heartbeat every 30s, stale client detection
- Requirement 6.6: Extends BaseWSManager
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

from websocket.base_ws_manager import BaseWSManager

logger = logging.getLogger(__name__)

VALID_SUBSCRIPTIONS = {
    "job_created",
    "status_changed",
    "delay_alert",
    "cargo_update",
    "cargo_complete",
}
HEARTBEAT_INTERVAL_SECONDS = 30


class SchedulingWebSocketManager(BaseWSManager):
    """
    Manages scheduling WebSocket connections with subscription filtering,
    heartbeat keep-alive, and stale client detection.

    Extends BaseWSManager for metrics and backpressure (Req 6.6).

    Validates: Req 9.1, 9.3, 9.6
    """

    def __init__(self, max_pending_messages: int = 100) -> None:
        super().__init__(
            manager_name="scheduling",
            max_pending_messages=max_pending_messages,
        )
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

        client_meta: Dict[str, Any] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": "",
            "pending_count": 0,
            "subscriptions": valid_subs,
            "_alive": True,
        }

        async with self._lock:
            self._clients[websocket] = client_meta
            self._metrics["connections_total"] += 1

        logger.info(
            "Scheduling WebSocket client connected. subscriptions=%s total=%d",
            valid_subs or "all",
            len(self._clients),
        )

        await self._send_to_client(websocket, {
            "type": "connection",
            "status": "connected",
            "manager": self.manager_name,
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
            removed = self._clients.pop(websocket, None)
            if removed is not None:
                self._metrics["disconnections_total"] += 1

        logger.info(
            "Scheduling WebSocket client disconnected. total=%d",
            len(self._clients),
        )

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast(self, event_type: str, data: dict) -> int:
        """Send data to every client whose subscriptions include event_type.

        Applies backpressure from BaseWSManager.

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
                (ws, meta) for ws, meta in self._clients.items()
                if not meta.get("subscriptions") or event_type in meta.get("subscriptions", set())
            ]

        if not targets:
            return 0

        successful = 0
        disconnected: List[WebSocket] = []

        for ws, meta in targets:
            # Backpressure check
            if meta.get("pending_count", 0) >= self.max_pending_messages:
                self._metrics["messages_dropped_total"] += 1
                logger.warning(
                    "%s backpressure: dropping message for client (pending=%d)",
                    self.manager_name, meta["pending_count"],
                )
                continue

            meta["pending_count"] = meta.get("pending_count", 0) + 1
            ok = await self._send_to_client(ws, message)
            meta["pending_count"] = max(0, meta.get("pending_count", 1) - 1)

            if ok:
                successful += 1
                meta["last_send"] = datetime.now(timezone.utc)
                self._metrics["messages_sent_total"] += 1
            else:
                disconnected.append(ws)
                self._metrics["send_failures_total"] += 1

        if disconnected:
            async with self._lock:
                for ws in disconnected:
                    self._clients.pop(ws, None)
                    self._metrics["disconnections_total"] += 1
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
    # Heartbeat & stale detection  (Req 9.6)
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Periodically send heartbeat messages and prune stale clients.

        Validates: Req 9.6
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

                async with self._lock:
                    clients = list(self._clients.items())

                if not clients:
                    break

                # 1. Disconnect stale clients from previous cycle
                stale: List[WebSocket] = []
                for ws, meta in clients:
                    if not meta.get("_alive", True):
                        stale.append(ws)
                    else:
                        meta["_alive"] = False

                if stale:
                    async with self._lock:
                        for ws in stale:
                            self._clients.pop(ws, None)
                            self._metrics["disconnections_total"] += 1
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
                    remaining = list(self._clients.items())

                for ws, meta in remaining:
                    ok = await self._send_to_client(ws, heartbeat_msg)
                    if ok:
                        meta["_alive"] = True
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
                meta = self._clients.get(websocket)
            if meta is not None:
                meta["_alive"] = True

        elif msg_type == "subscribe":
            new_subs = message.get("subscriptions", [])
            valid = {s for s in new_subs if s in VALID_SUBSCRIPTIONS}
            async with self._lock:
                meta = self._clients.get(websocket)
            if meta is not None:
                meta["subscriptions"] = valid
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

        logger.info("%s WS manager shut down", self.manager_name)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_scheduling_ws_manager: Optional[SchedulingWebSocketManager] = None

# Compatibility adapter for ServiceContainer (Req 2.6, 2.7)
_container: Optional[Any] = None


def bind_container(container: Any) -> None:
    """Called by bootstrap modules to wire the compatibility adapter.

    When bound, ``get_scheduling_ws_manager()`` delegates to the container's
    ``scheduling_ws_manager`` attribute instead of the module-level singleton.

    Requirements: 2.6, 2.7
    """
    global _container
    _container = container


def get_scheduling_ws_manager() -> SchedulingWebSocketManager:
    """Return the global SchedulingWebSocketManager instance.

    If a ServiceContainer has been bound via ``bind_container()``,
    delegates to ``container.scheduling_ws_manager``.  Otherwise falls back
    to the legacy module-level singleton.

    Requirements: 2.6, 2.7
    """
    if _container is not None:
        return _container.scheduling_ws_manager
    global _scheduling_ws_manager
    if _scheduling_ws_manager is None:
        _scheduling_ws_manager = SchedulingWebSocketManager()
    return _scheduling_ws_manager
