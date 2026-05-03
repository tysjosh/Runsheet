"""
Real SMS dispatcher using the Twilio REST API.

Extends ``ChannelDispatcher`` to deliver SMS messages via Twilio.
Credentials are read from environment variables at init time; a
``ValueError`` is raised if any are missing.  The Twilio SDK is
imported lazily so the module can be imported even when the ``twilio``
package is not installed.

Requirements: 2.1, 2.4, 2.5, 2.6
"""

import logging
import os

from notifications.services.channel_dispatchers import ChannelDispatcher

logger = logging.getLogger(__name__)


class TwilioSmsDispatcher(ChannelDispatcher):
    """SMS dispatcher backed by the Twilio Messages API.

    Validates: Requirements 2.1, 2.4, 2.5, 2.6
    """

    def __init__(self) -> None:
        self._account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        self._auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        self._from_number = os.environ.get("TWILIO_FROM_NUMBER")

        if not all([self._account_sid, self._auth_token, self._from_number]):
            raise ValueError(
                "Missing Twilio SMS credentials in environment. "
                "Required: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER"
            )

        # Lazy import — twilio is an optional runtime dependency
        from twilio.rest import Client  # type: ignore[import-untyped]

        self._client = Client(self._account_sid, self._auth_token)

    @property
    def channel_name(self) -> str:
        return "sms"

    async def dispatch(self, notification: dict) -> str:
        """Send an SMS via Twilio.

        On success the provider message SID is stored in
        ``notification["provider_message_id"]``.  On failure the reason
        is stored in ``notification["failure_reason"]``.

        Returns ``'sent'`` or ``'failed'``.
        """
        from twilio.base.exceptions import TwilioRestException  # type: ignore[import-untyped]

        try:
            message = self._client.messages.create(
                body=notification.get("message_body", ""),
                from_=self._from_number,
                to=notification.get("recipient_reference", ""),
            )
            notification["provider_message_id"] = message.sid
            logger.info(
                "[SMS] Sent to %s — SID %s",
                notification.get("recipient_reference"),
                message.sid,
            )
            return "sent"
        except TwilioRestException as exc:
            if exc.status == 429:
                notification["failure_reason"] = f"Rate limited: {exc.msg}"
                logger.warning(
                    "[SMS] Rate-limited sending to %s: %s",
                    notification.get("recipient_reference"),
                    exc.msg,
                )
            else:
                notification["failure_reason"] = str(exc)
                logger.error(
                    "[SMS] Failed to send to %s: %s",
                    notification.get("recipient_reference"),
                    exc,
                )
            return "failed"
        except Exception as exc:
            notification["failure_reason"] = str(exc)
            logger.error(
                "[SMS] Unexpected error sending to %s: %s",
                notification.get("recipient_reference"),
                exc,
            )
            return "failed"
