"""
Real WhatsApp dispatcher using the Twilio REST API.

Extends ``ChannelDispatcher`` to deliver WhatsApp messages via Twilio.
The WhatsApp channel uses the ``whatsapp:`` prefix on both the sender
and recipient phone numbers.  Credentials are read from environment
variables at init time; a ``ValueError`` is raised if any are missing.
The Twilio SDK is imported lazily so the module can be imported even
when the ``twilio`` package is not installed.

Requirements: 2.2, 2.4, 2.5, 2.6
"""

import logging
import os

from notifications.services.channel_dispatchers import ChannelDispatcher

logger = logging.getLogger(__name__)


class TwilioWhatsAppDispatcher(ChannelDispatcher):
    """WhatsApp dispatcher backed by the Twilio Messages API.

    Validates: Requirements 2.2, 2.4, 2.5, 2.6
    """

    def __init__(self) -> None:
        self._account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        self._auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        self._from_number = os.environ.get("TWILIO_WHATSAPP_FROM_NUMBER")

        if not all([self._account_sid, self._auth_token, self._from_number]):
            raise ValueError(
                "Missing Twilio WhatsApp credentials in environment. "
                "Required: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM_NUMBER"
            )

        # Lazy import — twilio is an optional runtime dependency
        from twilio.rest import Client  # type: ignore[import-untyped]

        self._client = Client(self._account_sid, self._auth_token)

    @property
    def channel_name(self) -> str:
        return "whatsapp"

    async def dispatch(self, notification: dict) -> str:
        """Send a WhatsApp message via Twilio.

        Prefixes both ``from_`` and ``to`` numbers with ``whatsapp:``
        as required by the Twilio WhatsApp API.

        On success the provider message SID is stored in
        ``notification["provider_message_id"]``.  On failure the reason
        is stored in ``notification["failure_reason"]``.

        Returns ``'sent'`` or ``'failed'``.
        """
        from twilio.base.exceptions import TwilioRestException  # type: ignore[import-untyped]

        recipient = notification.get("recipient_reference", "")
        from_number = self._from_number

        # Add whatsapp: prefix if not already present
        if not recipient.startswith("whatsapp:"):
            recipient = f"whatsapp:{recipient}"
        if not from_number.startswith("whatsapp:"):
            from_number = f"whatsapp:{from_number}"

        try:
            message = self._client.messages.create(
                body=notification.get("message_body", ""),
                from_=from_number,
                to=recipient,
            )
            notification["provider_message_id"] = message.sid
            logger.info(
                "[WHATSAPP] Sent to %s — SID %s",
                notification.get("recipient_reference"),
                message.sid,
            )
            return "sent"
        except TwilioRestException as exc:
            if exc.status == 429:
                notification["failure_reason"] = f"Rate limited: {exc.msg}"
                logger.warning(
                    "[WHATSAPP] Rate-limited sending to %s: %s",
                    notification.get("recipient_reference"),
                    exc.msg,
                )
            else:
                notification["failure_reason"] = str(exc)
                logger.error(
                    "[WHATSAPP] Failed to send to %s: %s",
                    notification.get("recipient_reference"),
                    exc,
                )
            return "failed"
        except Exception as exc:
            notification["failure_reason"] = str(exc)
            logger.error(
                "[WHATSAPP] Unexpected error sending to %s: %s",
                notification.get("recipient_reference"),
                exc,
            )
            return "failed"
