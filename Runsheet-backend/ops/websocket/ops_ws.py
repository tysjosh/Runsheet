"""
WebSocket manager for ops live updates.

Manages client connections with subscription-based filtering for
shipment_update, rider_update, and sla_breach event types.
Sends heartbeat every 30 seconds and disconnects stale clients.

Extends BaseWSManager for consistent lifecycle metrics and backpressure.

Feature flag integration:
- Reject new connections for disabled tenants with close code 4403
- Disconnect existing clients within 30s when tenant is disabled
- Exclude disabled tenant data from all broadcasts

Validates:
- Requirement 16.1: /ws/ops WebSocket endpoint
- Requirement 16.4: Subscription filters by event type
- Requirement 16.6: Heartbeat every 30s, stale client detection
- Requirement 27.3: Feature flag gating for WebSocket
- Requirement 6.6: Extends BaseWSManager
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING, Dict, List, Optional, Set

from fastapi import WebSocket

if TYPE_CHECKING:
    from ops.services.feature_flags import FeatureFlagService

from ops.services.ops_metrics import ops_ws_active_connections
from websocket.base_ws_manager import BaseWSManager

logger = logging.getLogger(__name__)

VALID_SUBSCRIPTIONS = {"shipment_update", "rider_update", "sla_breach"}
HEARTBEAT_INTERVAL_SECONDS = 30
STALE_TIMEOUT_SECONDS = 30


class OpsWebSocketManager(BaseWSManager):
    """
    Manages ops WebSocket connections with subscription filtering,
    heartbeat keep-alive, stale client detection, and feature flag gating.

    Extends BaseWSManager for metrics and backpressure (Req 6.6).

    Validates: Req 16.1, 16.4, 16.6, 27.3
    """

    def __init__(self, max_pending_messages: int = 100) -> None:
        super().__init__(
            manager_name="ops",
            max_pending_messages=max_pending_messages,
        )
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

        # Build extra metadata for the base registry
        extra_meta: Dict[str, Any] = {"subscriptions": valid_subs}

        # Register via base class (without calling accept again)
        client_meta: Dict[str, Any] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": tenant_id,
            "pending_count": 0,
            "subscriptions": valid_subs,
            # Heartbeat liveness tracking
            "_alive": True,
        }

        async with self._lock:
            self._clients[websocket] = client_meta
            self._metrics["connections_total"] += 1

        # Update Prometheus gauge
        ops_ws_active_connections.labels(tenant_id=tenant_id or "unknown").inc()

        logger.info(
            "Ops WebSocket client connected. tenant_id=%s subscriptions=%s total=%d",
            tenant_id or "unknown",
            valid_subs or "all",
            len(self._clients),
        )

        # Send handshake confirmation
        await self._send_to_client(websocket, {
            "type": "connection",
            "status": "connected",
            "manager": self.manager_name,
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
            if client is not None:
                self._metrics["disconnections_total"] += 1

        if client:
            ops_ws_active_connections.labels(
                tenant_id=client.get("tenant_id") or "unknown"
            ).dec()

        logger.info(
            "Ops WebSocket client disconnected. total=%d",
            len(self._clients),
        )

    async def disconnect_tenant(self, tenant_id: str) -> int:
        """
        Disconnect all WebSocket clients belonging to a specific tenant.

        Returns the number of clients disconnected.
        Validates: Req 27.3
        """
        async with self._lock:
            tenant_clients = [
                (ws, meta) for ws, meta in self._clients.items()
                if meta.get("tenant_id") == tenant_id
            ]
            for ws, _ in tenant_clients:
                self._clients.pop(ws, None)
                self._metrics["disconnections_total"] += 1

        disconnected = 0
        for ws, _ in tenant_clients:
            try:
                await ws.close(code=4403, reason="tenant_disabled")
                disconnected += 1
            except Exception:
                disconnected += 1

        if disconnected:
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
        """Broadcast a shipment update to subscribed clients. Validates: Req 16.2"""
        return await self._broadcast_event("shipment_update", shipment_data)

    async def broadcast_rider_update(self, rider_data: dict) -> int:
        """Broadcast a rider update to subscribed clients. Validates: Req 16.3"""
        return await self._broadcast_event("rider_update", rider_data)

    async def broadcast_sla_breach(self, breach_data: dict) -> int:
        """Broadcast an SLA breach event to subscribed clients."""
        return await self._broadcast_event("sla_breach", breach_data)

    async def broadcast_fuel_alert(self, alert_data: dict) -> int:
        """Broadcast a fuel stock alert. Validates: Requirement 4.3."""
        return await self._broadcast_event("fuel_alert", alert_data)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _broadcast_event(self, event_type: str, data: dict) -> int:
        """
        Send *data* to every client whose subscriptions include *event_type*.

        Excludes clients belonging to disabled tenants (Req 27.3).
        Applies backpressure from BaseWSManager.
        """
        data_tenant_id = data.get("tenant_id", "")

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

    # ------------------------------------------------------------------
    # Heartbeat & stale detection
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """
        Periodically send heartbeat messages and prune stale clients.

        Also checks feature flags and disconnects clients whose tenants
        have been disabled.

        Validates: Req 16.6, 27.3
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

                async with self._lock:
                    clients = list(self._clients.items())

                if not clients:
                    break

                # 0. Disconnect clients whose tenants are now disabled
                if self._feature_flag_service:
                    tenant_ids = {meta.get("tenant_id") for _, meta in clients if meta.get("tenant_id")}
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
                        "Disconnected %d stale ops WS clients",
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
            logger.error("Ops WS heartbeat loop error: %s", exc)

    # ------------------------------------------------------------------
    # Client message handling
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

_ops_ws_manager: Optional[OpsWebSocketManager] = None

# Compatibility adapter for ServiceContainer (Req 2.6, 2.7)
_container: Optional[Any] = None


def bind_container(container: Any) -> None:
    """Called by bootstrap modules to wire the compatibility adapter.

    When bound, ``get_ops_ws_manager()`` delegates to the container's
    ``ops_ws_manager`` attribute instead of the module-level singleton.

    Requirements: 2.6, 2.7
    """
    global _container
    _container = container


def get_ops_ws_manager() -> OpsWebSocketManager:
    """Return the global OpsWebSocketManager instance.

    If a ServiceContainer has been bound via ``bind_container()``,
    delegates to ``container.ops_ws_manager``.  Otherwise falls back
    to the legacy module-level singleton.

    Requirements: 2.6, 2.7
    """
    if _container is not None:
        return _container.ops_ws_manager
    global _ops_ws_manager
    if _ops_ws_manager is None:
        _ops_ws_manager = OpsWebSocketManager()
    return _ops_ws_manager
