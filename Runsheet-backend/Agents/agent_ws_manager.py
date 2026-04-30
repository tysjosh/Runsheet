"""
Agent Activity WebSocket Manager.

Manages WebSocket connections for the /ws/agent-activity channel,
broadcasting real-time agent activity events, approval queue changes,
and autonomous agent alerts to connected clients.

Requirements: 2.7, 8.7
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class AgentActivityWSManager:
    """
    Manages WebSocket connections for agent activity real-time updates.

    Broadcasts:
    - Activity log entries (agent actions, monitoring cycles, tool invocations)
    - Approval queue events (created, approved, rejected, expired)
    - Autonomous agent alerts (delay_alert, fuel_alert, sla_breach)

    Handles dead client cleanup on broadcast failures.

    Validates: Requirements 2.7, 8.7
    """

    def __init__(self) -> None:
        self._clients: Dict[WebSocket, datetime] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, websocket: WebSocket) -> None:
        """
        Accept a WebSocket connection and register it.

        Sends a connection confirmation message after accepting.
        """
        await websocket.accept()

        async with self._lock:
            self._clients[websocket] = datetime.now(timezone.utc)

        logger.info(
            "Agent activity WS client connected. total=%d",
            len(self._clients),
        )

        await self._send_to_client(websocket, {
            "type": "connection",
            "status": "connected",
            "message": "Connected to agent activity updates",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        async with self._lock:
            self._clients.pop(websocket, None)

        logger.info(
            "Agent activity WS client disconnected. total=%d",
            len(self._clients),
        )

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast_activity(self, data: dict) -> int:
        """
        Broadcast an activity log event to all connected clients.

        Wraps the data in a standard message envelope with type
        ``agent_activity`` and a timestamp.

        Returns the number of clients that successfully received the message.
        Removes dead clients on send failure.

        Validates: Requirement 8.7
        """
        message = {
            "type": "agent_activity",
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self._broadcast(message)

    async def broadcast_approval_event(self, event_type: str, data: dict) -> int:
        """
        Broadcast an approval queue event to all connected clients.

        Wraps the data with the given *event_type* (e.g. ``approval_created``,
        ``approval_approved``, ``approval_rejected``, ``approval_expired``).

        Returns the number of clients that successfully received the message.
        Removes dead clients on send failure.

        Validates: Requirement 2.7
        """
        message = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self._broadcast(message)

    async def broadcast_event(self, event_type: str, data: dict) -> int:
        """
        Broadcast a generic event to all connected clients.

        Used by autonomous agents for ``delay_alert``, ``fuel_alert``,
        ``sla_breach``, and other event types.

        Returns the number of clients that successfully received the message.
        Removes dead clients on send failure.
        """
        message = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self._broadcast(message)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _broadcast(self, message: dict) -> int:
        """
        Send *message* to every connected client.

        Dead clients (those that fail to receive the message) are removed
        from the connection pool.

        Returns the number of clients that successfully received the message.
        """
        async with self._lock:
            clients = list(self._clients.keys())

        if not clients:
            return 0

        successful = 0
        dead: List[WebSocket] = []

        send_tasks = [self._send_to_client(ws, message) for ws in clients]
        results = await asyncio.gather(*send_tasks, return_exceptions=True)

        for ws, result in zip(clients, results):
            if result is True:
                successful += 1
            else:
                dead.append(ws)

        # Clean up dead clients
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.pop(ws, None)
            logger.info(
                "Removed %d dead agent activity WS clients during broadcast",
                len(dead),
            )

        logger.debug(
            "Agent activity WS broadcast: %d/%d clients received",
            successful,
            len(clients),
        )
        return successful

    async def _send_to_client(self, websocket: WebSocket, data: dict) -> bool:
        """Send JSON to a single client. Returns True on success."""
        try:
            await websocket.send_json(data)
            return True
        except Exception as exc:
            logger.warning("Failed to send to agent activity WS client: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_connection_count(self) -> int:
        """Return the number of active connections."""
        return len(self._clients)

    async def shutdown(self) -> None:
        """Close all connections and clear the client pool."""
        async with self._lock:
            for ws in list(self._clients.keys()):
                try:
                    await ws.close(code=1000, reason="shutdown")
                except Exception:
                    pass
            self._clients.clear()

        logger.info("Agent activity WS manager shut down")


# Module-level singleton
_agent_ws_manager: Optional[AgentActivityWSManager] = None


def get_agent_ws_manager() -> AgentActivityWSManager:
    """Return the module-level AgentActivityWSManager singleton."""
    global _agent_ws_manager
    if _agent_ws_manager is None:
        _agent_ws_manager = AgentActivityWSManager()
    return _agent_ws_manager
