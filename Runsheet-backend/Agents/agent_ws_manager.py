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

#: Top-level activity-log fields pushed live. Everything else (``parameters``,
#: which holds the orchestrator's ``{"message": <user prompt>}``, plus
#: ``user_id`` and ``session_id``) stays in the persisted log only (L2).
_ACTIVITY_BROADCAST_KEYS = frozenset(
    {
        "log_id",
        "agent_id",
        "action_type",
        "tool_name",
        "risk_level",
        "outcome",
        "duration_ms",
        "tenant_id",
        "timestamp",
    }
)

#: ``details`` fields pushed live: counts, ids and codes, never free text such
#: as the plan ``goal`` (the user's prompt), ``step_results`` or ``result``.
_ACTIVITY_DETAIL_KEYS = frozenset(
    {
        "event",
        "plan_id",
        "step_count",
        "target_domains",
        "targets",
        "is_complex",
        "detection_count",
        "action_count",
        "confirmation_method",
        "response_length",
        "failed_targets",
        "error_codes",
        "dropped_targets",
    }
)


def _activity_projection(data: Dict[str, Any]) -> Dict[str, Any]:
    """The allowlisted subset of an activity entry that is safe to push live."""
    projected = {k: v for k, v in data.items() if k in _ACTIVITY_BROADCAST_KEYS}
    details = data.get("details")
    if isinstance(details, dict):
        projected["details"] = {
            k: v for k, v in details.items() if k in _ACTIVITY_DETAIL_KEYS
        }
    return projected


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
        Broadcast an activity log event to the entry's tenant only.

        Wraps a redacted projection of the entry (:func:`_activity_projection`)
        in a standard message envelope with type ``agent_activity`` and a
        timestamp.

        Returns the number of clients that successfully received the message.

        An entry without a ``tenant_id`` is dropped with a WARNING (fail
        closed, L2). It used to go to every connected client in every tenant,
        and the planner's ``plan_created`` entry carried no tenant and the
        user's chat prompt verbatim. The persisted activity log keeps the full
        entry; only the live push is narrowed.

        Validates: Requirement 8.7
        """
        tenant_id = (data or {}).get("tenant_id") if isinstance(data, dict) else None
        if not tenant_id:
            logger.warning(
                "agent_activity %s without tenant_id dropped",
                (data or {}).get("action_type") if isinstance(data, dict) else None,
            )
            return 0
        message = {
            "type": "agent_activity",
            "data": _activity_projection(data),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast_to_tenant(tenant_id, message)

    async def broadcast_approval_event(self, event_type: str, data: dict) -> int:
        """
        Broadcast an approval queue event to the entry's tenant only.

        Wraps the data with the given *event_type* (e.g. ``approval_created``,
        ``approval_approved``, ``approval_rejected``, ``approval_expired``,
        ``approval_execution_updated``). Data without a ``tenant_id`` is
        dropped with a WARNING (fail closed, loading-plan-executor K9).

        Returns the number of clients that successfully received the message.

        Validates: Requirement 2.7
        """
        tenant_id = (data or {}).get("tenant_id") if isinstance(data, dict) else None
        if not tenant_id:
            logger.warning("approval event %s without tenant_id dropped", event_type)
            return 0
        message = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast_to_tenant(tenant_id, message)

    async def broadcast_event(self, event_type: str, data: dict) -> int:
        """
        Broadcast a generic event to the payload's tenant only.

        Used by autonomous agents for ``delay_alert``, ``fuel_alert``,
        ``sla_breach``, and other event types. Alerts are tenant-scoped like
        approval events (K9 extended to alerts, OI-01): data without a
        ``tenant_id`` is dropped with a WARNING (fail closed).

        Returns the number of clients that successfully received the message.
        """
        tenant_id = (data or {}).get("tenant_id") if isinstance(data, dict) else None
        if not tenant_id:
            logger.warning("%s without tenant_id dropped", event_type)
            return 0
        message = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast_to_tenant(tenant_id, message)


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
