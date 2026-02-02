"""
WebSocket connection manager for real-time fleet updates.

This module provides the ConnectionManager class for managing multiple
WebSocket client connections and broadcasting location updates.

Validates:
- Requirement 6.7: THE Backend_Service SHALL implement WebSocket connections
  for pushing real-time updates to connected Frontend_Application clients
"""

import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Set, Any
from fastapi import WebSocket, WebSocketDisconnect


logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manager for WebSocket connections supporting real-time fleet updates.
    
    This class manages multiple WebSocket client connections, handles
    connection lifecycle (connect, disconnect), and broadcasts location
    updates to all connected clients.
    
    Validates:
    - Requirement 6.7: Implement WebSocket connections for pushing real-time
      updates to connected Frontend_Application clients
    
    Attributes:
        active_connections: Set of currently connected WebSocket clients
        _lock: Asyncio lock for thread-safe connection management
    """
    
    def __init__(self):
        """Initialize the ConnectionManager with empty connection set."""
        self.active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger(__name__)
    
    async def connect(self, websocket: WebSocket) -> None:
        """
        Accept a new WebSocket connection and add it to active connections.
        
        Args:
            websocket: The WebSocket connection to accept
        """
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)
        
        self._logger.info(
            f"WebSocket client connected. Total connections: {len(self.active_connections)}",
            extra={"extra_data": {
                "total_connections": len(self.active_connections),
                "client_host": websocket.client.host if websocket.client else "unknown"
            }}
        )
        
        # Send initial connection confirmation
        await self._send_to_client(websocket, {
            "type": "connection",
            "status": "connected",
            "message": "Connected to fleet live updates",
            "timestamp": datetime.utcnow().isoformat() + "Z"
        })
    
    async def disconnect(self, websocket: WebSocket) -> None:
        """
        Remove a WebSocket connection from active connections.
        
        Args:
            websocket: The WebSocket connection to remove
        """
        async with self._lock:
            self.active_connections.discard(websocket)
        
        self._logger.info(
            f"WebSocket client disconnected. Total connections: {len(self.active_connections)}",
            extra={"extra_data": {
                "total_connections": len(self.active_connections)
            }}
        )
    
    async def _send_to_client(self, websocket: WebSocket, data: dict) -> bool:
        """
        Send data to a specific WebSocket client.
        
        Args:
            websocket: The WebSocket connection to send to
            data: The data dictionary to send as JSON
            
        Returns:
            True if send was successful, False otherwise
        """
        try:
            await websocket.send_json(data)
            return True
        except Exception as e:
            self._logger.warning(
                f"Failed to send to WebSocket client: {e}",
                extra={"extra_data": {"error": str(e)}}
            )
            return False
    
    async def broadcast(self, message: dict) -> int:
        """
        Broadcast a message to all connected WebSocket clients.
        
        This method sends the message to all active connections and
        handles any disconnected clients gracefully.
        
        Args:
            message: The message dictionary to broadcast as JSON
            
        Returns:
            Number of clients that successfully received the message
        """
        if not self.active_connections:
            return 0
        
        # Add timestamp if not present
        if "timestamp" not in message:
            message["timestamp"] = datetime.utcnow().isoformat() + "Z"
        
        # Create a copy of connections to iterate over
        async with self._lock:
            connections = list(self.active_connections)
        
        successful_sends = 0
        disconnected_clients: List[WebSocket] = []
        
        # Send to all clients concurrently
        send_tasks = []
        for websocket in connections:
            send_tasks.append(self._send_to_client(websocket, message))
        
        results = await asyncio.gather(*send_tasks, return_exceptions=True)
        
        # Process results and identify disconnected clients
        for websocket, result in zip(connections, results):
            if result is True:
                successful_sends += 1
            elif isinstance(result, Exception) or result is False:
                disconnected_clients.append(websocket)
        
        # Remove disconnected clients
        if disconnected_clients:
            async with self._lock:
                for websocket in disconnected_clients:
                    self.active_connections.discard(websocket)
            
            self._logger.info(
                f"Removed {len(disconnected_clients)} disconnected clients",
                extra={"extra_data": {
                    "removed_count": len(disconnected_clients),
                    "remaining_connections": len(self.active_connections)
                }}
            )
        
        self._logger.debug(
            f"Broadcast complete: {successful_sends}/{len(connections)} clients received message",
            extra={"extra_data": {
                "successful_sends": successful_sends,
                "total_clients": len(connections)
            }}
        )
        
        return successful_sends
    
    async def broadcast_location_update(
        self,
        truck_id: str,
        latitude: float,
        longitude: float,
        timestamp: Optional[str] = None,
        speed_kmh: Optional[float] = None,
        heading: Optional[float] = None,
        **extra_data: Any
    ) -> int:
        """
        Broadcast a location update to all connected clients.
        
        This is a convenience method for broadcasting location updates
        with a standardized message format.
        
        Validates:
        - Requirement 6.7: Push real-time updates to connected clients
        
        Args:
            truck_id: The ID of the truck being updated
            latitude: GPS latitude coordinate
            longitude: GPS longitude coordinate
            timestamp: Optional timestamp of the update (ISO format)
            speed_kmh: Optional speed in km/h
            heading: Optional heading in degrees
            **extra_data: Additional data to include in the message
            
        Returns:
            Number of clients that successfully received the update
        """
        message = {
            "type": "location_update",
            "data": {
                "truck_id": truck_id,
                "coordinates": {
                    "lat": latitude,
                    "lon": longitude
                },
                "timestamp": timestamp or datetime.utcnow().isoformat() + "Z"
            }
        }
        
        # Add optional fields
        if speed_kmh is not None:
            message["data"]["speed_kmh"] = speed_kmh
        if heading is not None:
            message["data"]["heading"] = heading
        
        # Add any extra data
        if extra_data:
            message["data"].update(extra_data)
        
        return await self.broadcast(message)
    
    async def broadcast_batch_update(
        self,
        updates: List[dict]
    ) -> int:
        """
        Broadcast multiple location updates in a single message.
        
        This is useful for batch updates to reduce WebSocket message overhead.
        
        Args:
            updates: List of location update dictionaries
            
        Returns:
            Number of clients that successfully received the batch
        """
        message = {
            "type": "batch_location_update",
            "data": {
                "updates": updates,
                "count": len(updates)
            },
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        
        return await self.broadcast(message)
    
    def get_connection_count(self) -> int:
        """
        Get the current number of active WebSocket connections.
        
        Returns:
            Number of active connections
        """
        return len(self.active_connections)
    
    async def send_heartbeat(self) -> int:
        """
        Send a heartbeat message to all connected clients.
        
        This can be used to keep connections alive and detect
        disconnected clients.
        
        Returns:
            Number of clients that successfully received the heartbeat
        """
        message = {
            "type": "heartbeat",
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        return await self.broadcast(message)


# Global connection manager instance
_connection_manager: Optional[ConnectionManager] = None


def get_connection_manager() -> ConnectionManager:
    """
    Get the global ConnectionManager instance.
    
    Creates a new instance if one doesn't exist (singleton pattern).
    
    Returns:
        The global ConnectionManager instance
    """
    global _connection_manager
    if _connection_manager is None:
        _connection_manager = ConnectionManager()
    return _connection_manager
