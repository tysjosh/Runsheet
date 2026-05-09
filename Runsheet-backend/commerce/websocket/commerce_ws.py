"""/ws/commerce/invoices WebSocket route.

Tenant-scoped subscription — every broadcast filters
``invoice.tenant_id == connection.tenant_id``.

Uses the same BaseWSManager helper the intake spec defines for /ws/orders.

Validates:
- Design §6: WS /ws/commerce/invoices
- Tenant isolation: cross-tenant payloads are NEVER delivered
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

from websocket.base_ws_manager import BaseWSManager

logger = logging.getLogger(__name__)


class CommerceInvoiceWSManager(BaseWSManager):
    """WebSocket manager for real-time invoice state updates.

    Extends BaseWSManager with tenant-scoped broadcast filtering.
    Every broadcast envelope carries ``tenant_id`` and the manager
    filters targets so cross-tenant payloads never cross the wire.

    Validates: Design §6 — WS /ws/commerce/invoices
    """

    def __init__(self, max_pending_messages: int = 100) -> None:
        super().__init__(
            manager_name="commerce_invoices",
            max_pending_messages=max_pending_messages,
        )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(
        self,
        websocket: WebSocket,
        *,
        tenant_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Accept and register a WebSocket connection for invoice updates.

        The ``tenant_id`` is derived from the caller's JWT by the endpoint
        handler and stored in client metadata for broadcast filtering.
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

        # Standard handshake confirmation
        await self._send_to_client(websocket, {
            "type": "connection",
            "status": "connected",
            "manager": self.manager_name,
            "channel": "/ws/commerce/invoices",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(
            "Commerce invoices WS client connected. tenant_id=%s total=%d",
            tenant_id or "unknown",
            self.active_connections,
        )

    # ------------------------------------------------------------------
    # Tenant-scoped broadcast
    # ------------------------------------------------------------------

    async def broadcast_invoice_update(self, invoice_doc: Dict[str, Any]) -> int:
        """Broadcast an invoice state update to all connections for that tenant.

        Only clients whose ``meta.tenant_id`` matches
        ``invoice_doc["tenant_id"]`` receive the message. Cross-tenant
        payloads are NEVER delivered.

        Args:
            invoice_doc: The full updated invoice projection dict.
                         Must contain ``tenant_id``.

        Returns:
            Number of clients that successfully received the update.
        """
        payload_tenant_id = invoice_doc.get("tenant_id")
        if not payload_tenant_id:
            raise ValueError(
                "CommerceInvoiceWSManager broadcasts require tenant_id in the invoice doc"
            )

        message: Dict[str, Any] = {
            "type": "invoice_updated",
            "data": invoice_doc,
            "tenant_id": payload_tenant_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Filter targets by tenant_id
        async with self._lock:
            targets = [
                (ws, meta) for ws, meta in self._clients.items()
                if meta.get("tenant_id") == payload_tenant_id
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
                "Removed %d dead commerce invoice WS clients during broadcast",
                len(dead),
            )

        logger.debug(
            "Commerce invoice WS broadcast (tenant=%s): %d/%d clients received",
            payload_tenant_id,
            successful,
            len(targets),
        )
        return successful

    # ------------------------------------------------------------------
    # Client message handling
    # ------------------------------------------------------------------

    async def handle_client_message(self, websocket: WebSocket, raw: str) -> None:
        """Process an incoming text message from a client.

        Supported message types:
        - ``ping``: responds with pong (keep-alive).
        """
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type = message.get("type")

        if msg_type == "ping":
            await self._send_to_client(websocket, {
                "type": "pong",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })


# ---------------------------------------------------------------------------
# Module-level singleton + ServiceContainer compatibility
# ---------------------------------------------------------------------------

_commerce_invoice_ws_manager: Optional[CommerceInvoiceWSManager] = None
_container: Optional[Any] = None


def bind_container(container: Any) -> None:
    """Called by bootstrap modules to wire the compatibility adapter."""
    global _container
    _container = container


def get_commerce_invoice_ws_manager() -> CommerceInvoiceWSManager:
    """Return the global CommerceInvoiceWSManager instance.

    If a ServiceContainer has been bound via ``bind_container()``,
    delegates to ``container.commerce_invoice_ws_manager``. Otherwise
    falls back to the module-level singleton.
    """
    if _container is not None:
        mgr = getattr(_container, "commerce_invoice_ws_manager", None)
        if mgr is not None:
            return mgr
    global _commerce_invoice_ws_manager
    if _commerce_invoice_ws_manager is None:
        _commerce_invoice_ws_manager = CommerceInvoiceWSManager()
    return _commerce_invoice_ws_manager
