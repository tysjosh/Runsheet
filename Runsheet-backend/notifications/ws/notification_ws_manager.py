"""
Notification WebSocket Manager.

Manages WebSocket connections for the /ws/notifications channel,
broadcasting real-time notification events and delivery status changes
to connected clients.

Extends BaseWSManager for consistent lifecycle metrics and backpressure.

Requirements: 11.1, 11.2, 11.3
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from websocket.base_ws_manager import BaseWSManager

logger = logging.getLogger(__name__)


class NotificationWSManager(BaseWSManager):
    """
    Manages WebSocket connections for notification real-time updates.

    Extends BaseWSManager for metrics and backpressure (Req 11.2).

    Broadcasts:
    - notification_created: when a new notification is generated
    - notification_status_changed: when a delivery status changes

    Validates: Requirements 11.1, 11.2, 11.3
    """

    def __init__(self, max_pending_messages: int = 100) -> None:
        super().__init__(
            manager_name="notifications",
            max_pending_messages=max_pending_messages,
        )

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast_notification(self, notification: dict) -> int:
        """
        Broadcast a new notification event to all connected clients.

        Wraps the notification data in a standard message envelope with type
        ``notification_created`` and a timestamp.

        Returns the number of clients that successfully received the message.

        Validates: Requirement 11.1
        """
        message = {
            "type": "notification_created",
            "data": notification,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast(message)

    async def broadcast_status_update(
        self, notification_id: str, status: str, data: dict
    ) -> int:
        """
        Broadcast a delivery status change to all connected clients.

        Wraps the status update in a standard message envelope with type
        ``notification_status_changed`` and a timestamp.

        Returns the number of clients that successfully received the message.

        Validates: Requirement 11.3
        """
        message = {
            "type": "notification_status_changed",
            "data": {
                "notification_id": notification_id,
                "status": status,
                **data,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast(message)
