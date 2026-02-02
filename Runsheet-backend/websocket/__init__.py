"""
WebSocket module for real-time fleet updates.

This module provides WebSocket functionality for pushing real-time
location updates to connected frontend clients.

Validates:
- Requirement 6.7: THE Backend_Service SHALL implement WebSocket connections
  for pushing real-time updates to connected Frontend_Application clients
"""

from .connection_manager import ConnectionManager, get_connection_manager

__all__ = ["ConnectionManager", "get_connection_manager"]
