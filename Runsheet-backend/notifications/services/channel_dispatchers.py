"""
Channel dispatcher abstract base class and stub implementations.

Defines the ``ChannelDispatcher`` ABC that every delivery channel must
implement, plus log-only stub dispatchers for SMS, email, and WhatsApp
used during MVP development.

Requirements: 2.1, 2.4, 2.5, 2.6
"""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class ChannelDispatcher(ABC):
    """Abstract base class for notification channel dispatchers.

    Each concrete dispatcher is responsible for delivering a rendered
    notification through a specific channel (SMS, email, WhatsApp, etc.)
    and reporting back a delivery status string.

    Validates: Requirement 2.6
    """

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Return the channel identifier (e.g., 'sms', 'email', 'whatsapp')."""

    @abstractmethod
    async def dispatch(self, notification: dict) -> str:
        """Dispatch a notification.

        Parameters
        ----------
        notification:
            A dict containing at least ``recipient_reference`` and
            ``message_body`` keys, plus any channel-specific fields.

        Returns
        -------
        str
            Delivery status — ``'sent'`` on success, ``'failed'`` on error.
        """


class StubSmsDispatcher(ChannelDispatcher):
    """Log-only SMS dispatcher for MVP development.

    Logs the notification details and returns ``'sent'`` without
    actually sending an SMS message.

    Validates: Requirement 2.1, 2.4
    """

    @property
    def channel_name(self) -> str:
        return "sms"

    async def dispatch(self, notification: dict) -> str:
        logger.info(
            "[SMS STUB] To: %s | %s",
            notification.get("recipient_reference", "unknown"),
            notification.get("message_body", ""),
        )
        return "sent"


class StubEmailDispatcher(ChannelDispatcher):
    """Log-only email dispatcher for MVP development.

    Logs the notification details and returns ``'sent'`` without
    actually sending an email.

    Validates: Requirement 2.1, 2.4
    """

    @property
    def channel_name(self) -> str:
        return "email"

    async def dispatch(self, notification: dict) -> str:
        logger.info(
            "[EMAIL STUB] To: %s | Subject: %s | %s",
            notification.get("recipient_reference", "unknown"),
            notification.get("subject", "(no subject)"),
            notification.get("message_body", ""),
        )
        return "sent"


class StubWhatsAppDispatcher(ChannelDispatcher):
    """Log-only WhatsApp dispatcher for MVP development.

    Logs the notification details and returns ``'sent'`` without
    actually sending a WhatsApp message.

    Validates: Requirement 2.1, 2.4
    """

    @property
    def channel_name(self) -> str:
        return "whatsapp"

    async def dispatch(self, notification: dict) -> str:
        logger.info(
            "[WHATSAPP STUB] To: %s | %s",
            notification.get("recipient_reference", "unknown"),
            notification.get("message_body", ""),
        )
        return "sent"
