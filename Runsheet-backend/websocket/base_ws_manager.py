"""
Base WebSocket Manager with lifecycle metrics and backpressure.

All four WS managers (fleet, ops, scheduling, agent activity) extend
this base class to get consistent connection tracking, metric emission,
backpressure enforcement, and stale client detection.

Requirements: 6.1–6.9
"""
import asyncio
import logging
from abc import ABC
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class BaseWSManager(ABC):
    """Abstract base for all WebSocket connection managers.

    Provides:
    - Connection registry with metadata (connected_at, last_send, tenant_id, pending_count)
    - Prometheus-compatible metric counters
    - Configurable backpressure (max pending messages per client)
    - Stale client detection (time since last successful send)
    - Standard lifecycle: connect, disconnect, broadcast, shutdown

    Args:
        manager_name: Label for metrics (e.g., "fleet", "ops").
        max_pending_messages: Backpressure threshold per client (default 100).
    """

    def __init__(
        self,
        manager_name: str,
        max_pending_messages: int = 100,
    ) -> None:
        self.manager_name = manager_name
        self.max_pending_messages = max_pending_messages
        self._lock = asyncio.Lock()

        # Connection registry: ws → metadata dict
        self._clients: Dict[WebSocket, Dict[str, Any]] = {}

        # Metrics counters (Req 6.1)
        self._metrics = {
            "connections_total": 0,
            "disconnections_total": 0,
            "messages_sent_total": 0,
            "send_failures_total": 0,
            "messages_dropped_total": 0,
        }

    # ------------------------------------------------------------------
    # Metrics (Req 6.1)
    # ------------------------------------------------------------------

    @property
    def active_connections(self) -> int:
        """Gauge: number of currently active connections."""
        return len(self._clients)

    def get_metrics(self) -> Dict[str, Any]:
        """Return a snapshot of all metrics for this manager."""
        return {
            "manager": self.manager_name,
            "active_connections": self.active_connections,
            **self._metrics,
        }

    # ------------------------------------------------------------------
    # Connection lifecycle (Req 6.1, 6.9)
    # ------------------------------------------------------------------

    async def connect(
        self,
        websocket: WebSocket,
        *,
        tenant_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Accept and register a WebSocket connection.

        Sends a standard connection confirmation message (Req 6.9):
        ``{"type": "connection", "status": "connected", "manager": "<name>", "timestamp": "<iso>"}``
        """
        await websocket.accept()

        client_meta: Dict[str, Any] = {
            "connected_at": datetime.now(timezone.utc),
            "last_send": None,
            "tenant_id": tenant_id,
            "pending_count": 0,
        }
        if metadata:
            client_meta.update(metadata)

        async with self._lock:
            self._clients[websocket] = client_meta
            self._metrics["connections_total"] += 1

        # Standard handshake confirmation (Req 6.9)
        await self._send_to_client(websocket, {
            "type": "connection",
            "status": "connected",
            "manager": self.manager_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(
            "%s WS client connected. total=%d tenant=%s",
            self.manager_name, self.active_connections, tenant_id,
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection and update metrics."""
        async with self._lock:
            removed = self._clients.pop(websocket, None)
            if removed is not None:
                self._metrics["disconnections_total"] += 1

        logger.info(
            "%s WS client disconnected. total=%d",
            self.manager_name, self.active_connections,
        )

    # ------------------------------------------------------------------
    # Broadcasting with backpressure (Req 6.2, 6.3, 6.7)
    # ------------------------------------------------------------------

    async def broadcast(self, message: dict) -> int:
        """Send *message* to all connected clients.

        Applies backpressure (Req 6.2): clients whose pending count exceeds
        ``max_pending_messages`` have the message dropped.

        Dead clients are cleaned up within 5 seconds (Req 6.7).

        Returns the number of clients that received the message.
        """
        async with self._lock:
            clients = list(self._clients.items())

        if not clients:
            return 0

        successful = 0
        dead: List[WebSocket] = []

        for ws, meta in clients:
            # Backpressure check (Req 6.2, 6.3)
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

        # Clean up dead clients (Req 6.7 — within 5 seconds)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.pop(ws, None)
                    self._metrics["disconnections_total"] += 1
            logger.info(
                "%s: removed %d dead clients during broadcast",
                self.manager_name, len(dead),
            )

        return successful

    # ------------------------------------------------------------------
    # Client communication
    # ------------------------------------------------------------------

    async def _send_to_client(self, websocket: WebSocket, data: dict) -> bool:
        """Send JSON to a single client. Returns True on success."""
        try:
            await websocket.send_json(data)
            return True
        except Exception as exc:
            logger.warning(
                "%s: send failed: %s", self.manager_name, exc,
            )
            return False

    # ------------------------------------------------------------------
    # Stale client detection (Req 6.4)
    # ------------------------------------------------------------------

    def get_stale_clients(self, stale_seconds: float = 120.0) -> List[WebSocket]:
        """Return clients that haven't received a message in *stale_seconds*.

        A client is considered stale if ``last_send`` is set and the elapsed
        time exceeds *stale_seconds*.  Clients that have never received a
        message (``last_send is None``) are not considered stale.
        """
        now = datetime.now(timezone.utc)
        stale: List[WebSocket] = []
        for ws, meta in self._clients.items():
            last = meta.get("last_send")
            if last is not None and (now - last).total_seconds() > stale_seconds:
                stale.append(ws)
        return stale

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_connection_count(self) -> int:
        """Return the number of active connections."""
        return len(self._clients)

    def get_client_metadata(self, websocket: WebSocket) -> Optional[Dict[str, Any]]:
        """Return metadata for a specific client, or None if not connected."""
        return self._clients.get(websocket)

    async def shutdown(self) -> None:
        """Close all connections and clear the client pool."""
        async with self._lock:
            for ws in list(self._clients.keys()):
                try:
                    await ws.close(code=1000, reason="shutdown")
                except Exception:
                    pass
            self._clients.clear()

        logger.info("%s WS manager shut down", self.manager_name)
