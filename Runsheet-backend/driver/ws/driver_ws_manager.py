"""
Driver WebSocket Manager.

Manages WebSocket connections for the /ws/driver channel, providing
real-time bidirectional communication between the platform and driver
mobile clients. Supports assignment delivery, heartbeat-based presence
tracking, and driver-to-server event routing.

Extends BaseWSManager for consistent lifecycle metrics and backpressure.

Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import WebSocket

from websocket.base_ws_manager import BaseWSManager

logger = logging.getLogger(__name__)

# Server-to-driver event types (Req 9.3)
SERVER_TO_DRIVER_EVENTS = {
    "assignment",
    "new_route",
    "escalation",
    "message",
    "assignment_revoked",
}

# Driver-to-server event types (Req 9.4)
DRIVER_TO_SERVER_EVENTS = {
    "ack",
    "status_update",
    "exception",
    "heartbeat",
    "location_update",
}

# Heartbeat timeout in seconds (Req 9.6)
HEARTBEAT_TIMEOUT_SECONDS = 120


class DriverWSManager(BaseWSManager):
    """
    Dedicated WebSocket manager for driver mobile clients.

    Extends BaseWSManager with driver-specific features including
    per-driver connection tracking, heartbeat-based presence management,
    and bidirectional event routing.

    Broadcasts server-to-driver events:
    - assignment: new job assignment
    - new_route: updated route information
    - escalation: high/critical severity alerts
    - message: job-thread messages
    - assignment_revoked: job reassigned to another driver

    Handles driver-to-server events:
    - ack: job acknowledgment
    - status_update: driver status change
    - exception: field exception report
    - heartbeat: keep-alive signal
    - location_update: GPS position update

    Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
    """

    def __init__(
        self,
        es_service: Any = None,
        max_pending_messages: int = 50,
    ) -> None:
        super().__init__(
            manager_name="driver",
            max_pending_messages=max_pending_messages,
        )
        self._es = es_service
        # driver_id → WebSocket mapping for targeted sends
        self._driver_connections: Dict[str, WebSocket] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle (Req 9.1, 9.2)
    # ------------------------------------------------------------------

    async def connect_driver(
        self,
        websocket: WebSocket,
        driver_id: str,
        tenant_id: str,
    ) -> None:
        """
        Authenticate and register a driver WebSocket connection.

        Stores the driver_id → WebSocket mapping for targeted sends,
        updates presence to 'online', and sends a connection confirmation.

        Args:
            websocket: The WebSocket connection to register.
            driver_id: Unique identifier for the driver.
            tenant_id: Tenant context for the connection.

        Validates: Requirements 9.1, 9.2
        """
        metadata = {
            "driver_id": driver_id,
            "last_heartbeat": datetime.now(timezone.utc),
        }
        await super().connect(websocket, tenant_id=tenant_id, metadata=metadata)

        self._driver_connections[driver_id] = websocket

        # Update presence to online
        await self.update_presence(driver_id, "online", tenant_id=tenant_id)

        logger.info(
            "Driver %s connected via WebSocket (tenant=%s)",
            driver_id,
            tenant_id,
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """
        Remove a driver WebSocket connection and update presence.

        Cleans up the driver_id → WebSocket mapping and marks the
        driver as offline in the presence index.
        """
        # Find the driver_id for this websocket before removing
        driver_id = None
        tenant_id = ""
        meta = self._clients.get(websocket)
        if meta:
            driver_id = meta.get("driver_id")
            tenant_id = meta.get("tenant_id", "")

        # Remove from driver connections map
        if driver_id and self._driver_connections.get(driver_id) is websocket:
            del self._driver_connections[driver_id]

        await super().disconnect(websocket)

        # Update presence to offline
        if driver_id:
            await self.update_presence(driver_id, "offline", tenant_id=tenant_id)
            logger.info("Driver %s disconnected from WebSocket", driver_id)

    # ------------------------------------------------------------------
    # Targeted sending (Req 9.3)
    # ------------------------------------------------------------------

    async def send_to_driver(self, driver_id: str, event: dict) -> bool:
        """
        Send an event to a specific driver by driver_id.

        Args:
            driver_id: The target driver's identifier.
            event: The event payload to send. Should include a 'type' field.

        Returns:
            True if the message was sent successfully, False otherwise.

        Validates: Requirement 9.3
        """
        ws = self._driver_connections.get(driver_id)
        if ws is None:
            logger.warning(
                "Cannot send to driver %s: not connected", driver_id
            )
            return False

        meta = self._clients.get(ws)
        if meta is None:
            logger.warning(
                "Cannot send to driver %s: no client metadata", driver_id
            )
            return False

        # Backpressure check
        if meta.get("pending_count", 0) >= self.max_pending_messages:
            self._metrics["messages_dropped_total"] += 1
            logger.warning(
                "driver backpressure: dropping message for driver %s (pending=%d)",
                driver_id,
                meta["pending_count"],
            )
            return False

        # Add timestamp if not present
        if "timestamp" not in event:
            event["timestamp"] = datetime.now(timezone.utc).isoformat()

        meta["pending_count"] = meta.get("pending_count", 0) + 1
        ok = await self._send_to_client(ws, event)
        meta["pending_count"] = max(0, meta.get("pending_count", 1) - 1)

        if ok:
            meta["last_send"] = datetime.now(timezone.utc)
            self._metrics["messages_sent_total"] += 1
        else:
            self._metrics["send_failures_total"] += 1
            # Clean up dead connection
            if driver_id in self._driver_connections:
                del self._driver_connections[driver_id]
            async with self._lock:
                self._clients.pop(ws, None)
                self._metrics["disconnections_total"] += 1
            logger.info(
                "Removed dead driver connection for %s during send",
                driver_id,
            )

        return ok

    # ------------------------------------------------------------------
    # Server-to-driver event helpers (Req 9.3)
    # ------------------------------------------------------------------

    async def send_assignment(self, driver_id: str, job_data: dict) -> bool:
        """Send an assignment event to a driver. Validates: Req 9.3"""
        return await self.send_to_driver(driver_id, {
            "type": "assignment",
            "data": job_data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def send_new_route(self, driver_id: str, route_data: dict) -> bool:
        """Send a new_route event to a driver. Validates: Req 9.3"""
        return await self.send_to_driver(driver_id, {
            "type": "new_route",
            "data": route_data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def send_escalation(self, driver_id: str, escalation_data: dict) -> bool:
        """Send an escalation event to a driver. Validates: Req 9.3"""
        return await self.send_to_driver(driver_id, {
            "type": "escalation",
            "data": escalation_data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def send_message(self, driver_id: str, message_data: dict) -> bool:
        """Send a message event to a driver. Validates: Req 9.3"""
        return await self.send_to_driver(driver_id, {
            "type": "message",
            "data": message_data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def send_assignment_revoked(self, driver_id: str, revocation_data: dict) -> bool:
        """Send an assignment_revoked event to a driver. Validates: Req 9.3"""
        return await self.send_to_driver(driver_id, {
            "type": "assignment_revoked",
            "data": revocation_data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ------------------------------------------------------------------
    # Driver-to-server event handling (Req 9.4, 9.5)
    # ------------------------------------------------------------------

    async def handle_driver_message(self, websocket: WebSocket, raw: str) -> None:
        """
        Route driver-to-server events based on message type.

        Supported event types: ack, status_update, exception, heartbeat,
        location_update.

        Args:
            websocket: The WebSocket that sent the message.
            raw: Raw JSON string from the driver client.

        Validates: Requirements 9.4, 9.5
        """
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Malformed JSON from driver WebSocket: %s", raw[:200])
            await self._send_to_client(websocket, {
                "type": "error",
                "message": "Invalid JSON",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return

        msg_type = message.get("type")
        meta = self._clients.get(websocket)

        if meta is None:
            logger.warning("Received message from unregistered driver WebSocket")
            return

        driver_id = meta.get("driver_id", "")
        tenant_id = meta.get("tenant_id", "")

        if msg_type == "heartbeat":
            # Update heartbeat timestamp (Req 9.5)
            meta["last_heartbeat"] = datetime.now(timezone.utc)
            await self.update_presence(driver_id, "online", tenant_id=tenant_id)
            # Send heartbeat acknowledgment
            await self._send_to_client(websocket, {
                "type": "heartbeat_ack",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        elif msg_type == "location_update":
            location = message.get("data", {}).get("location")
            if location:
                await self._update_driver_location(driver_id, location, tenant_id)
            # Also treat as a heartbeat
            meta["last_heartbeat"] = datetime.now(timezone.utc)

        elif msg_type == "ack":
            logger.info("Driver %s sent ack for job %s", driver_id, message.get("data", {}).get("job_id"))

        elif msg_type == "status_update":
            logger.info(
                "Driver %s status update: %s",
                driver_id,
                message.get("data", {}).get("status"),
            )

        elif msg_type == "exception":
            logger.info(
                "Driver %s reported exception: %s",
                driver_id,
                message.get("data", {}).get("exception_type"),
            )

        elif msg_type == "ping":
            await self._send_to_client(websocket, {
                "type": "pong",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        else:
            logger.warning(
                "Unknown driver event type '%s' from driver %s",
                msg_type,
                driver_id,
            )
            await self._send_to_client(websocket, {
                "type": "error",
                "message": f"Unknown event type: {msg_type}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    # ------------------------------------------------------------------
    # Presence management (Req 9.5, 9.6)
    # ------------------------------------------------------------------

    async def update_presence(
        self,
        driver_id: str,
        status: str,
        *,
        tenant_id: str = "",
        location: Optional[dict] = None,
    ) -> None:
        """
        Update driver presence in the driver_presence ES index.

        Args:
            driver_id: The driver's unique identifier.
            status: Presence status ('online' or 'offline').
            tenant_id: Tenant context.
            location: Optional GPS location dict with lat/lng.

        Validates: Requirement 9.5
        """
        if self._es is None:
            logger.debug(
                "ES service not available; skipping presence update for driver %s",
                driver_id,
            )
            return

        from driver.services.driver_es_mappings import DRIVER_PRESENCE_INDEX

        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "driver_id": driver_id,
            "tenant_id": tenant_id,
            "status": status,
            "last_seen": now,
        }

        if status == "online":
            doc["connected_at"] = now

        if location:
            doc["last_location"] = location

        try:
            self._es.client.index(
                index=DRIVER_PRESENCE_INDEX,
                id=driver_id,
                body=doc,
            )
            logger.debug(
                "Updated presence for driver %s: status=%s",
                driver_id,
                status,
            )
        except Exception as exc:
            logger.error(
                "Failed to update presence for driver %s: %s",
                driver_id,
                exc,
            )

    async def check_heartbeat_timeouts(self) -> list:
        """
        Mark drivers as offline if no heartbeat within 120 seconds.

        Iterates over all connected drivers and checks the elapsed time
        since their last heartbeat. Drivers exceeding the timeout are
        marked as offline in the presence index and disconnected.

        Returns:
            List of driver_ids that were marked offline.

        Validates: Requirement 9.6
        """
        now = datetime.now(timezone.utc)
        timed_out_drivers = []
        timed_out_websockets = []

        async with self._lock:
            clients = list(self._clients.items())

        for ws, meta in clients:
            last_hb = meta.get("last_heartbeat")
            if last_hb is None:
                continue

            elapsed = (now - last_hb).total_seconds()
            if elapsed > HEARTBEAT_TIMEOUT_SECONDS:
                driver_id = meta.get("driver_id", "")
                tenant_id = meta.get("tenant_id", "")
                if driver_id:
                    timed_out_drivers.append(driver_id)
                    timed_out_websockets.append((ws, driver_id, tenant_id))
                    logger.info(
                        "Driver %s heartbeat timeout (%.0fs > %ds)",
                        driver_id,
                        elapsed,
                        HEARTBEAT_TIMEOUT_SECONDS,
                    )

        # Disconnect timed-out drivers
        for ws, driver_id, tenant_id in timed_out_websockets:
            await self.update_presence(driver_id, "offline", tenant_id=tenant_id)

            # Remove from driver connections
            if self._driver_connections.get(driver_id) is ws:
                del self._driver_connections[driver_id]

            # Remove from clients
            async with self._lock:
                self._clients.pop(ws, None)
                self._metrics["disconnections_total"] += 1

            # Close the WebSocket
            try:
                await ws.close(code=4002, reason="Heartbeat timeout")
            except Exception:
                pass

        if timed_out_drivers:
            logger.info(
                "Marked %d driver(s) as offline due to heartbeat timeout: %s",
                len(timed_out_drivers),
                timed_out_drivers,
            )

        return timed_out_drivers

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _update_driver_location(
        self,
        driver_id: str,
        location: dict,
        tenant_id: str,
    ) -> None:
        """Update the driver's last known location in the presence index."""
        if self._es is None:
            return

        from driver.services.driver_es_mappings import DRIVER_PRESENCE_INDEX

        try:
            self._es.client.update(
                index=DRIVER_PRESENCE_INDEX,
                id=driver_id,
                body={
                    "doc": {
                        "last_location": location,
                        "last_seen": datetime.now(timezone.utc).isoformat(),
                    },
                    "upsert": {
                        "driver_id": driver_id,
                        "tenant_id": tenant_id,
                        "status": "online",
                        "last_location": location,
                        "last_seen": datetime.now(timezone.utc).isoformat(),
                    },
                },
            )
        except Exception as exc:
            logger.error(
                "Failed to update location for driver %s: %s",
                driver_id,
                exc,
            )

    def get_connected_driver_ids(self) -> list:
        """Return a list of currently connected driver IDs."""
        return list(self._driver_connections.keys())

    def is_driver_connected(self, driver_id: str) -> bool:
        """Check if a specific driver is currently connected."""
        return driver_id in self._driver_connections

    # ------------------------------------------------------------------
    # Broadcasting to all drivers
    # ------------------------------------------------------------------

    async def broadcast_to_all_drivers(self, event: dict) -> int:
        """
        Broadcast an event to all connected drivers.

        Wraps the event in a standard message envelope with a timestamp.

        Returns the number of drivers that successfully received the message.
        """
        if "timestamp" not in event:
            event["timestamp"] = datetime.now(timezone.utc).isoformat()
        return await self.broadcast(event)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_driver_ws_manager: Optional[DriverWSManager] = None

# Compatibility adapter for ServiceContainer
_container: Optional[Any] = None


def bind_container(container: Any) -> None:
    """Called by bootstrap modules to wire the compatibility adapter.

    When bound, ``get_driver_ws_manager()`` delegates to the container's
    ``driver_ws_manager`` attribute instead of the module-level singleton.
    """
    global _container
    _container = container


def get_driver_ws_manager() -> DriverWSManager:
    """Return the module-level DriverWSManager instance.

    If a ServiceContainer has been bound via ``bind_container()``,
    delegates to ``container.driver_ws_manager``.  Otherwise falls back
    to the legacy module-level singleton.
    """
    if _container is not None:
        return _container.driver_ws_manager
    global _driver_ws_manager
    if _driver_ws_manager is None:
        _driver_ws_manager = DriverWSManager()
    return _driver_ws_manager
