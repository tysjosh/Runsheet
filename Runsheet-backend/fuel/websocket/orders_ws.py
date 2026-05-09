"""
WebSocket manager for order and driver real-time updates.

Manages client connections with subscription-based filtering for
order_placed, order_status_changed, order_assigned, driver_update,
and sla_breach event types. Every broadcast envelope carries tenant_id
and the manager filters targets by both subscription AND tenant_id so
cross-tenant payloads never cross the wire.

Extends BaseWSManager for consistent lifecycle metrics and backpressure.

Validates:
- Requirement 4.1: /ws/orders WebSocket endpoint
- Requirement 9.1.4: Tenant-scoped broadcast filtering
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

from websocket.base_ws_manager import BaseWSManager

logger = logging.getLogger(__name__)

VALID_SUBSCRIPTIONS: Set[str] = {
    "order_placed",
    "order_status_changed",
    "order_assigned",
    "driver_update",
    "sla_breach",
}

HEARTBEAT_INTERVAL_SECONDS = 30


class OrdersWSManager(BaseWSManager):
    """
    Manages order WebSocket connections with subscription filtering
    and tenant-scoped broadcast isolation.

    Extends BaseWSManager for metrics and backpressure.

    Every broadcast envelope includes ``tenant_id`` and the manager
    filters targets by BOTH subscription type AND
    ``meta.tenant_id == payload.tenant_id``. Cross-tenant payloads
    are NEVER delivered.

    Validates: Requirement 4.1, 9.1.4
    """

    valid_subscriptions = VALID_SUBSCRIPTIONS

    def __init__(self, max_pending_messages: int = 100) -> None:
        super().__init__(
            manager_name="orders",
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
        tenant_id: str = "",
    ) -> None:
        """
        Accept a WebSocket connection and register it with the given subscriptions.

        If *subscriptions* is ``None`` or empty the client receives all event types.
        Invalid subscription names are silently ignored.

        The ``tenant_id`` is derived from the caller's JWT by the endpoint handler
        and stored in client metadata for broadcast filtering.
        """
        await websocket.accept()

        valid_subs: Set[str] = set()
        if subscriptions:
            valid_subs = {s for s in subscriptions if s in VALID_SUBSCRIPTIONS}

        client_meta: Dict[str, Any] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": tenant_id,
            "pending_count": 0,
            "subscriptions": valid_subs,
            "_alive": True,
        }

        async with self._lock:
            self._clients[websocket] = client_meta
            self._metrics["connections_total"] += 1

        logger.info(
            "Orders WebSocket client connected. tenant_id=%s subscriptions=%s total=%d",
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
            "message": "Connected to orders live updates",
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

        logger.info(
            "Orders WebSocket client disconnected. total=%d",
            len(self._clients),
        )

    async def disconnect_tenant(self, tenant_id: str) -> int:
        """
        Disconnect all WebSocket clients belonging to a specific tenant.

        Returns the number of clients disconnected.
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
            logger.info(
                "Disconnected %d orders WS clients for tenant_id=%s",
                disconnected,
                tenant_id,
            )
        return disconnected

    # ------------------------------------------------------------------
    # Broadcasting — tenant-scoped + subscription-filtered
    # ------------------------------------------------------------------

    async def broadcast_order_placed(self, order_data: dict) -> int:
        """Broadcast an order_placed event to subscribed clients.

        Validates: Requirement 4.1.1
        """
        return await self._broadcast_event("order_placed", order_data)

    async def broadcast_order_status_changed(self, order_data: dict) -> int:
        """Broadcast an order_status_changed event to subscribed clients.

        Validates: Requirement 4.1.1
        """
        return await self._broadcast_event("order_status_changed", order_data)

    async def broadcast_order_assigned(self, order_data: dict) -> int:
        """Broadcast an order_assigned event to subscribed clients.

        Validates: Requirement 4.1.1
        """
        return await self._broadcast_event("order_assigned", order_data)

    async def broadcast_driver_update(self, driver_data: dict) -> int:
        """Broadcast a driver_update event to subscribed clients.

        Validates: Requirement 4.1.1
        """
        return await self._broadcast_event("driver_update", driver_data)

    async def broadcast_sla_breach(self, breach_data: dict) -> int:
        """Broadcast an sla_breach event to subscribed clients.

        Validates: Requirement 4.1.1
        """
        return await self._broadcast_event("sla_breach", breach_data)

    # ------------------------------------------------------------------
    # Core broadcast with tenant isolation
    # ------------------------------------------------------------------

    async def _broadcast_event(self, event_type: str, data: dict) -> int:
        """
        Send *data* to every client whose subscriptions include *event_type*
        AND whose ``meta.tenant_id`` matches ``data["tenant_id"]``.

        Cross-tenant payloads are NEVER delivered (Req 4.1.2, 9.1.4).
        Applies backpressure from BaseWSManager.

        Raises ValueError if ``tenant_id`` is missing from the payload.
        """
        payload_tenant_id = data.get("tenant_id")
        if not payload_tenant_id:
            raise ValueError(
                "OrdersWSManager broadcasts require tenant_id in the payload"
            )

        message = {
            "type": event_type,
            "data": data,
            "tenant_id": payload_tenant_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Filter targets by BOTH tenant_id AND subscription
        async with self._lock:
            targets = [
                (ws, meta) for ws, meta in self._clients.items()
                if meta.get("tenant_id") == payload_tenant_id
                and (
                    not meta.get("subscriptions")
                    or event_type in meta.get("subscriptions", set())
                )
            ]

        if not targets:
            return 0

        successful = 0
        dead: List[WebSocket] = []

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
                dead.append(ws)
                self._metrics["send_failures_total"] += 1

        # Clean up dead clients
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.pop(ws, None)
                    self._metrics["disconnections_total"] += 1
            logger.info(
                "Removed %d disconnected orders WS clients during broadcast",
                len(dead),
            )

        logger.debug(
            "Orders WS broadcast %s (tenant=%s): %d/%d clients received",
            event_type,
            payload_tenant_id,
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

        Validates: Requirement 4.1 (keep-alive for connected clients)
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

                async with self._lock:
                    clients = list(self._clients.items())

                if not clients:
                    break

                # Disconnect stale clients from previous cycle
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
                        except Exception as close_exc:
                            logger.debug(
                                "Stale orders WS close failed (already gone?): %s",
                                close_exc,
                            )
                    logger.info(
                        "Disconnected %d stale orders WS clients",
                        len(stale),
                    )

                # Send heartbeat to remaining clients
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
            logger.error("Orders WS heartbeat loop error: %s", exc)

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
                except Exception as close_exc:
                    logger.debug(
                        "Orders WS shutdown close failed for client: %s",
                        close_exc,
                    )
            self._clients.clear()

        logger.info("%s WS manager shut down", self.manager_name)


# ---------------------------------------------------------------------------
# Module-level singleton + ServiceContainer compatibility
# ---------------------------------------------------------------------------

_orders_ws_manager: Optional[OrdersWSManager] = None
_container: Optional[Any] = None


def bind_container(container: Any) -> None:
    """Called by bootstrap modules to wire the compatibility adapter.

    When bound, ``get_orders_ws_manager()`` delegates to the container's
    ``orders_ws_manager`` attribute instead of the module-level singleton.
    """
    global _container
    _container = container


def get_orders_ws_manager() -> OrdersWSManager:
    """Return the global OrdersWSManager instance.

    If a ServiceContainer has been bound via ``bind_container()``,
    delegates to ``container.orders_ws_manager``. Otherwise falls back
    to the module-level singleton.
    """
    if _container is not None:
        return _container.orders_ws_manager
    global _orders_ws_manager
    if _orders_ws_manager is None:
        _orders_ws_manager = OrdersWSManager()
    return _orders_ws_manager
