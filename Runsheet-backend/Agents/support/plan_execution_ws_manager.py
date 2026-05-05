"""
Plan Execution WebSocket Manager.

Manages WebSocket connections for the /ws/plan-execution channel,
broadcasting real-time execution updates (driver check-ins, stop completions)
to connected clients.

Extends BaseWSManager for consistent lifecycle metrics and backpressure.

Requirements: 3.6, 3.9
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import WebSocket

from websocket.base_ws_manager import BaseWSManager

logger = logging.getLogger(__name__)


class PlanExecutionWSManager(BaseWSManager):
    """
    Manages WebSocket connections for plan execution real-time updates.

    Extends BaseWSManager for metrics and backpressure.

    Broadcasts:
    - Execution updates (driver check-ins, stop completions, progress changes)

    Channel: /ws/plan-execution
    Clients subscribe with tenant_id.

    Validates: Requirements 3.6, 3.9
    """

    def __init__(self, max_pending_messages: int = 100) -> None:
        super().__init__(
            manager_name="plan_execution",
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
        await super().connect(websocket, tenant_id=tenant_id)

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        await super().disconnect(websocket)

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast_execution_update(
        self,
        plan_id: str,
        route_id: str,
        stop_data: Dict[str, Any],
        completed_stops: int,
        total_stops: int,
    ) -> int:
        """
        Broadcast an execution update event to all connected clients.

        Wraps the data in a standard message envelope with type
        ``execution_update`` and a timestamp.

        Args:
            plan_id: The plan being executed.
            route_id: The route within the plan.
            stop_data: Dict with stop details (station_id, sequence, status).
            completed_stops: Number of stops completed so far.
            total_stops: Total number of stops in the execution.

        Returns the number of clients that successfully received the message.

        Validates: Requirement 3.6
        """
        message = {
            "type": "execution_update",
            "data": {
                "plan_id": plan_id,
                "route_id": route_id,
                "stop": stop_data,
                "completed_stops": completed_stops,
                "total_stops": total_stops,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast(message)


# Module-level singleton
_plan_execution_ws_manager: Optional[PlanExecutionWSManager] = None


def get_plan_execution_ws_manager() -> PlanExecutionWSManager:
    """Return the module-level PlanExecutionWSManager singleton instance.

    Creates the instance on first access (lazy initialization).

    Requirements: 3.6, 3.9
    """
    global _plan_execution_ws_manager
    if _plan_execution_ws_manager is None:
        _plan_execution_ws_manager = PlanExecutionWSManager()
    return _plan_execution_ws_manager
