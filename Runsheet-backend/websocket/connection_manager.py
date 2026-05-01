"""
WebSocket connection manager for real-time fleet updates.

This module provides the ConnectionManager class for managing multiple
WebSocket client connections and broadcasting location updates.

Extends BaseWSManager for consistent lifecycle metrics and backpressure.

Validates:
- Requirement 6.5: Tenant-scoped connections and subscription filtering
- Requirement 6.6: Shared BaseWSManager base class
- Requirement 6.7: THE Backend_Service SHALL implement WebSocket connections
  for pushing real-time updates to connected Frontend_Application clients
"""

import logging
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

from websocket.base_ws_manager import BaseWSManager

logger = logging.getLogger(__name__)


class ConnectionManager(BaseWSManager):
    """
    Manager for WebSocket connections supporting real-time fleet updates.

    Extends BaseWSManager with fleet-specific broadcast methods for
    location updates, batch updates, and heartbeat.

    Validates:
    - Requirement 6.6: Extends BaseWSManager
    - Requirement 6.7: Implement WebSocket connections for pushing real-time
      updates to connected Frontend_Application clients
    """

    def __init__(self, max_pending_messages: int = 100):
        """Initialize the ConnectionManager."""
        super().__init__(
            manager_name="fleet",
            max_pending_messages=max_pending_messages,
        )

    # ------------------------------------------------------------------
    # Backward-compatible access
    # ------------------------------------------------------------------

    def get_active_connections_set(self) -> set:
        """Return the set of connected WebSockets for backward compatibility.

        Code that previously iterated over ``manager.active_connections``
        should use this method instead.
        """
        return set(self._clients.keys())

    # ------------------------------------------------------------------
    # Domain-specific broadcast methods
    # ------------------------------------------------------------------

    async def broadcast_location_update(
        self,
        truck_id: str,
        latitude: float,
        longitude: float,
        timestamp: Optional[str] = None,
        speed_kmh: Optional[float] = None,
        heading: Optional[float] = None,
        asset_type: Optional[str] = None,
        asset_subtype: Optional[str] = None,
        **extra_data: Any,
    ) -> int:
        """
        Broadcast a location update to all connected clients.

        Validates:
        - Requirement 6.7: Push real-time updates to connected clients
        - Requirement 3.5: Include asset_type and asset_subtype in broadcast

        Args:
            truck_id: The ID of the asset being updated
            latitude: GPS latitude coordinate
            longitude: GPS longitude coordinate
            timestamp: Optional timestamp of the update (ISO format)
            speed_kmh: Optional speed in km/h
            heading: Optional heading in degrees
            asset_type: Optional asset type classification
            asset_subtype: Optional asset subtype
            **extra_data: Additional data to include in the message

        Returns:
            Number of clients that successfully received the update
        """
        message: Dict[str, Any] = {
            "type": "location_update",
            "data": {
                "truck_id": truck_id,
                "coordinates": {
                    "lat": latitude,
                    "lon": longitude,
                },
                "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            },
        }

        # Add optional fields
        if speed_kmh is not None:
            message["data"]["speed_kmh"] = speed_kmh
        if heading is not None:
            message["data"]["heading"] = heading
        if asset_type is not None:
            message["data"]["asset_type"] = asset_type
        if asset_subtype is not None:
            message["data"]["asset_subtype"] = asset_subtype
        if extra_data:
            message["data"].update(extra_data)

        return await self.broadcast(message)

    async def broadcast_batch_update(self, updates: List[dict]) -> int:
        """
        Broadcast multiple location updates in a single message.

        Args:
            updates: List of location update dictionaries

        Returns:
            Number of clients that successfully received the batch
        """
        message = {
            "type": "batch_location_update",
            "data": {
                "updates": updates,
                "count": len(updates),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast(message)

    async def send_heartbeat(self) -> int:
        """
        Send a heartbeat message to all connected clients.

        Returns:
            Number of clients that successfully received the heartbeat
        """
        message = {
            "type": "heartbeat",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast(message)


# ---------------------------------------------------------------------------
# Module-level singleton & compatibility adapter
# ---------------------------------------------------------------------------

_connection_manager: Optional[ConnectionManager] = None

# Compatibility adapter for ServiceContainer (Req 2.6, 2.7)
_container: Optional[Any] = None


def bind_container(container: Any) -> None:
    """Called by bootstrap modules to wire the compatibility adapter.

    When bound, ``get_connection_manager()`` delegates to the container's
    ``fleet_ws_manager`` attribute instead of the module-level singleton.

    Requirements: 2.6, 2.7
    """
    global _container
    _container = container


def get_connection_manager() -> ConnectionManager:
    """Return the ConnectionManager instance.

    If a ServiceContainer has been bound via ``bind_container()``,
    delegates to ``container.fleet_ws_manager``.  Otherwise falls back
    to the legacy module-level singleton.

    Requirements: 2.6, 2.7
    """
    if _container is not None:
        return _container.fleet_ws_manager
    global _connection_manager
    if _connection_manager is None:
        _connection_manager = ConnectionManager()
    return _connection_manager
