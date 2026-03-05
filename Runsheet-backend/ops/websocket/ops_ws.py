"""
WebSocket manager for ops live updates.

Manages client connections with subscription-based filtering for
shipment_update, rider_update, and sla_breach event types.
Sends heartbeat every 30 seconds and disconnects stale clients.

Feature flag integration:
- Reject new connections for disabled tenants with close code 4403
- Disconnect existing clients within 30s when tenant is disabled
- Exclude disabled tenant data from all broadcasts

Validates:
- Requirement 16.1: /ws/ops WebSocket endpoint
- Requirement 16.4: Subscription filters by event type
- Requirement 16.6: Heartbeat every 30s, stale client detection
- Requirement 27.3: Feature flag gating for WebSocket
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from fastapi import WebSocket

if TYPE_CHECKING:
    from ops.services.feature_flags import FeatureFlagService

from ops.services.ops_metrics import ops_ws_active_connections

logger = logging.getLogger(__name__)

VALID_SUBSCRIPTIONS = {"shipment_update", "rider_update", "sla_breach"}
HEARTBEAT_INTERVAL_SECONDS = 30
STALE_TIMEOUT_SECONDS = 30


class _ClientConnection:
    """Tracks a single WebSocket client with its subscriptions, tenant, and liveness."""

    __slots__ = ("websocket", "subscriptions", "tenant_id", "_alive")

    def __init__(self, websocket: WebSocket, subscriptions: Set[str], tenant_id: str = ""):
        self.websocket = websocket
        self.subscriptions = subscriptions
        self.tenant_id = tenant_id
        self._alive = True

    def mark_alive(self) -> None:
        self._alive = True

    def mark_pending(self) -> None:
        self._alive = False

    @property
    def is_alive(self) -> bool:
        return self._alive


class OpsWebSocketManager:
    """
    Manages ops WebSocket connections with subscription filtering,
    heartbeat keep-alive, stale client detection, and feature flag gating.

    Validates: Req 16.1, 16.4, 16.6, 27.3
    """

    def __init__(self) -> None:
        self._clients: Dict[WebSocket, _ClientConnection] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._feature_flag_service: Optional["FeatureFlagService"] = None

    def set_feature_flag_service(self, service: "FeatureFlagService") -> None:
        """Attach the FeatureFlagService for tenant gating."""
        self._feature_flag_service = service

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(
        self,
        websocket: WebSocket,
        subscriptions: Optional[List[str]] = None,
        tenant_id: str = "",
    ) -> None:
        """
        Accept a WebSocket connection and register it with the given subscriptions.

        If *subscriptions* is ``None`` or empty the client receives all event types.
        Invalid subscription names are silently ignored.

        Validates: Req 27.3 — rejects disabled tenants with close code 4403.
        """
        await websocket.accept()

        # Feature flag check: reject disabled tenants immediately after accept
        if tenant_id and self._feature_flag_service:
            try:
                enabled = await self._feature_flag_service.is_enabled(tenant_id)
                if not enabled:
                    logger.info(
                        "Ops WS connection rejected: tenant_id=%s is disabled",
                        tenant_id,
                    )
                    await websocket.close(code=4403, reason="tenant_disabled")
                    return
            except Exception as exc:
                logger.warning(
                    "Feature flag check failed for tenant_id=%s, allowing connection: %s",
                    tenant_id,
                    exc,
                )

        valid_subs: Set[str] = set()
        if subscriptions:
            valid_subs = {s for s in subscriptions if s in VALID_SUBSCRIPTIONS}
        # Empty set means "subscribe to everything"

        client = _ClientConnection(websocket, valid_subs, tenant_id=tenant_id)

        async with self._lock:
            self._clients[websocket] = client

        # Update Prometheus gauge
        ops_ws_active_connections.labels(tenant_id=tenant_id or "unknown").inc()

        logger.info(
            "Ops WebSocket client connected. tenant_id=%s subscriptions=%s total=%d",
            tenant_id or "unknown",
            valid_subs or "all",
            len(self._clients),
        )

        await self._send_to_client(websocket, {
            "type": "connection",
            "status": "connected",
            "subscriptions": sorted(valid_subs) if valid_subs else sorted(VALID_SUBSCRIPTIONS),
            "message": "Connected to ops live updates",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Start the heartbeat loop if not already running
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a client connection."""
        async with self._lock:
            client = self._clients.pop(websocket, None)

        if client:
            ops_ws_active_connections.labels(
                tenant_id=client.tenant_id or "unknown"
            ).dec()

        logger.info(
            "Ops WebSocket client disconnected. total=%d",
            len(self._clients),
        )

    async def disconnect_tenant(self, tenant_id: str) -> int:
        """
        Disconnect all WebSocket clients belonging to a specific tenant.

        Used when a tenant's feature flag is disabled to ensure existing
        connections are terminated within 30s (via heartbeat cycle).

        Returns the number of clients disconnected.
        Validates: Req 27.3
        """
        async with self._lock:
            tenant_clients = [
                (ws, c) for ws, c in self._clients.items()
                if c.tenant_id == tenant_id
            ]
            for ws, _ in tenant_clients:
                self._clients.pop(ws, None)

        disconnected = 0
        for ws, _ in tenant_clients:
            try:
                await ws.close(code=4403, reason="tenant_disabled")
                disconnected += 1
            except Exception:
                disconnected += 1  # Count even if close fails (already gone)

        if disconnected:
            # Update Prometheus gauge
            ops_ws_active_connections.labels(tenant_id=tenant_id).dec(disconnected)
            logger.info(
                "Disconnected %d ops WS clients for disabled tenant_id=%s",
                disconnected,
                tenant_id,
            )
        return disconnected

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast_shipment_update(self, shipment_data: dict) -> int:
        """
        Broadcast a shipment update to clients subscribed to ``shipment_update``.

        Validates: Req 16.2
        """
        return await self._broadcast_event("shipment_update", shipment_data)

    async def broadcast_rider_update(self, rider_data: dict) -> int:
        """
        Broadcast a rider update to clients subscribed to ``rider_update``.

        Validates: Req 16.3
        """
        return await self._broadcast_event("rider_update", rider_data)

    async def broadcast_sla_breach(self, breach_data: dict) -> int:
        """Broadcast an SLA breach event to subscribed clients."""
        return await self._broadcast_event("sla_breach", breach_data)

    async def broadcast_fuel_alert(self, alert_data: dict) -> int:
        """
        Broadcast a fuel stock alert to clients subscribed to ``fuel_alert``.

        Validates: Requirement 4.3 — WebSocket alert within 5 seconds.
        """
        return await self._broadcast_event("fuel_alert", alert_data)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _broadcast_event(self, event_type: str, data: dict) -> int:
        """
        Send *data* to every client whose subscriptions include *event_type*.

        Excludes clients belonging to disabled tenants (Req 27.3).
        """
        # Determine the tenant_id of the data being broadcast
        data_tenant_id = data.get("tenant_id", "")

        # If feature flags are configured and the data's tenant is disabled,
        # skip the entire broadcast for that tenant's data.
        if data_tenant_id and self._feature_flag_service:
            try:
                enabled = await self._feature_flag_service.is_enabled(data_tenant_id)
                if not enabled:
                    logger.debug(
                        "Skipping ops WS broadcast %s: tenant_id=%s is disabled",
                        event_type,
                        data_tenant_id,
                    )
                    return 0
            except Exception as exc:
                logger.warning(
                    "Feature flag check failed during broadcast for tenant_id=%s: %s",
                    data_tenant_id,
                    exc,
                )

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
                "Removed %d disconnected ops WS clients during broadcast",
                len(disconnected),
            )

        logger.debug(
            "Ops WS broadcast %s: %d/%d clients received",
            event_type,
            successful,
            len(targets),
        )
        return successful

    async def _send_to_client(self, websocket: WebSocket, data: dict) -> bool:
        """Send JSON to a single client. Returns True on success."""
        try:
            await websocket.send_json(data)
            return True
        except Exception as exc:
            logger.warning("Failed to send to ops WS client: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Heartbeat & stale detection
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """
        Periodically send heartbeat messages and prune stale clients.

        Also checks feature flags and disconnects clients whose tenants
        have been disabled (within 30s of flag change).

        Cycle:
        1. Disconnect clients for disabled tenants (Req 27.3).
        2. Mark all clients as *pending* (not yet responded).
        3. Send ``{"type": "heartbeat"}`` to every client.
        4. Wait ``HEARTBEAT_INTERVAL_SECONDS``.
        5. Any client still marked *pending* (i.e. send failed or no pong
           received) is considered stale and disconnected.

        Validates: Req 16.6, 27.3
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

                async with self._lock:
                    clients = list(self._clients.values())

                if not clients:
                    # No clients — stop the loop; it will restart on next connect
                    break

                # 0. Disconnect clients whose tenants are now disabled
                if self._feature_flag_service:
                    # Collect unique tenant_ids
                    tenant_ids = {c.tenant_id for c in clients if c.tenant_id}
                    for tid in tenant_ids:
                        try:
                            enabled = await self._feature_flag_service.is_enabled(tid)
                            if not enabled:
                                await self.disconnect_tenant(tid)
                        except Exception as exc:
                            logger.warning(
                                "Feature flag check failed in heartbeat for tenant_id=%s: %s",
                                tid,
                                exc,
                            )

                    # Refresh client list after potential disconnections
                    async with self._lock:
                        clients = list(self._clients.values())
                    if not clients:
                        break

                # 1. Disconnect clients that were already pending from the
                #    previous heartbeat cycle (stale).
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
                        "Disconnected %d stale ops WS clients",
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
                        # Successful send counts as "alive" for this cycle
                        c.mark_alive()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Ops WS heartbeat loop error: %s", exc)

    # ------------------------------------------------------------------
    # Client message handling (called from the endpoint)
    # ------------------------------------------------------------------

    async def handle_client_message(self, websocket: WebSocket, raw: str) -> None:
        """
        Process an incoming text message from a client.

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

_ops_ws_manager: Optional[OpsWebSocketManager] = None


def get_ops_ws_manager() -> OpsWebSocketManager:
    """Return the global OpsWebSocketManager singleton."""
    global _ops_ws_manager
    if _ops_ws_manager is None:
        _ops_ws_manager = OpsWebSocketManager()
    return _ops_ws_manager
