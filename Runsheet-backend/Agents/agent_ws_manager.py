"""
Agent Activity WebSocket Manager.

Manages WebSocket connections for the /ws/agent-activity channel,
broadcasting real-time agent activity events, approval queue changes,
and autonomous agent alerts to connected clients.

Extends BaseWSManager for consistent lifecycle metrics and backpressure.

Requirements: 2.7, 6.6, 8.7
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

from websocket.base_ws_manager import BaseWSManager

logger = logging.getLogger(__name__)


class AgentActivityWSManager(BaseWSManager):
    """
    Manages WebSocket connections for agent activity real-time updates.

    Extends BaseWSManager for metrics and backpressure (Req 6.6).

    Broadcasts:
    - Activity log entries (agent actions, monitoring cycles, tool invocations)
    - Approval queue events (created, approved, rejected, expired)
    - Autonomous agent alerts (delay_alert, fuel_alert, sla_breach)

    Validates: Requirements 2.7, 6.6, 8.7
    """

    def __init__(self, max_pending_messages: int = 100) -> None:
        super().__init__(
            manager_name="agent_activity",
            max_pending_messages=max_pending_messages,
        )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, websocket: WebSocket, tenant_id: str = "") -> None:
        """
        Accept a WebSocket connection and register it.

        Sends a connection confirmation message after accepting.
        """
        # Use base class connect which handles accept, registry, and handshake
        await super().connect(websocket, tenant_id=tenant_id)

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        await super().disconnect(websocket)

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast_activity(self, data: dict) -> int:
        """
        Broadcast an activity log event to all connected clients.

        Wraps the data in a standard message envelope with type
        ``agent_activity`` and a timestamp.

        Returns the number of clients that successfully received the message.

        Validates: Requirement 8.7
        """
        message = {
            "type": "agent_activity",
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast(message)

    async def broadcast_approval_event(self, event_type: str, data: dict) -> int:
        """
        Broadcast an approval queue event to all connected clients.

        Wraps the data with the given *event_type* (e.g. ``approval_created``,
        ``approval_approved``, ``approval_rejected``, ``approval_expired``).

        Returns the number of clients that successfully received the message.

        Validates: Requirement 2.7
        """
        message = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast(message)

    async def broadcast_event(self, event_type: str, data: dict) -> int:
        """
        Broadcast a generic event to all connected clients.

        Used by autonomous agents for ``delay_alert``, ``fuel_alert``,
        ``sla_breach``, and other event types.

        Returns the number of clients that successfully received the message.
        """
        message = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast(message)


# Module-level singleton
_agent_ws_manager: Optional[AgentActivityWSManager] = None

# Compatibility adapter for ServiceContainer (Req 2.6, 2.7)
_container: Optional[Any] = None


def bind_container(container: Any) -> None:
    """Called by bootstrap modules to wire the compatibility adapter.

    When bound, ``get_agent_ws_manager()`` delegates to the container's
    ``agent_ws_manager`` attribute instead of the module-level singleton.

    Requirements: 2.6, 2.7
    """
    global _container
    _container = container


def get_agent_ws_manager() -> AgentActivityWSManager:
    """Return the module-level AgentActivityWSManager instance.

    If a ServiceContainer has been bound via ``bind_container()``,
    delegates to ``container.agent_ws_manager``.  Otherwise falls back
    to the legacy module-level singleton.

    Requirements: 2.6, 2.7
    """
    if _container is not None:
        return _container.agent_ws_manager
    global _agent_ws_manager
    if _agent_ws_manager is None:
        _agent_ws_manager = AgentActivityWSManager()
    return _agent_ws_manager
