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


def _tenant_of(data: Any) -> Optional[str]:
    tenant_id = data.get("tenant_id") if isinstance(data, dict) else None
    return str(tenant_id) if tenant_id else None


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
        Broadcast a new notification event to the notification's tenant only.

        Wraps the notification data in a standard message envelope with type
        ``notification_created`` and a timestamp. A notification without a
        ``tenant_id`` is dropped with a WARNING (fail closed, W2): it carries
        a customer message body and used to reach every tenant's sockets.

        Returns the number of clients that successfully received the message.

        Validates: Requirement 11.1
        """
        tenant_id = _tenant_of(notification)
        if not tenant_id:
            logger.warning("notification_created without tenant_id dropped")
            return 0
        message = {
            "type": "notification_created",
            "data": notification,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast_to_tenant(tenant_id, message)

    async def broadcast_status_update(
        self, notification_id: str, status: str, data: dict
    ) -> int:
        """
        Broadcast a delivery status change to the notification's tenant only.

        Wraps the status update in a standard message envelope with type
        ``notification_status_changed`` and a timestamp. ``data`` without a
        ``tenant_id`` is dropped with a WARNING (fail closed, W2).

        Returns the number of clients that successfully received the message.

        Validates: Requirement 11.3
        """
        tenant_id = _tenant_of(data)
        if not tenant_id:
            logger.warning(
                "notification_status_changed %s without tenant_id dropped",
                notification_id,
            )
            return 0
        message = {
            "type": "notification_status_changed",
            "data": {
                "notification_id": notification_id,
                "status": status,
                **data,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self.broadcast_to_tenant(tenant_id, message)
