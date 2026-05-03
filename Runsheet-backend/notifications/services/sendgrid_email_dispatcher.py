"""
Real email dispatcher using the SendGrid v3 API.

Extends ``ChannelDispatcher`` to deliver email messages via SendGrid.
Credentials are read from environment variables at init time; a
``ValueError`` is raised if any are missing.  The SendGrid SDK is
imported lazily so the module can be imported even when the
``sendgrid`` package is not installed.

Requirements: 2.3, 2.4, 2.5, 2.6
"""

import logging
import os

from notifications.services.channel_dispatchers import ChannelDispatcher

logger = logging.getLogger(__name__)


class SendGridEmailDispatcher(ChannelDispatcher):
    """Email dispatcher backed by the SendGrid v3 Mail Send API.

    Validates: Requirements 2.3, 2.4, 2.5, 2.6
    """

    def __init__(self) -> None:
        self._api_key = os.environ.get("SENDGRID_API_KEY")
        self._from_email = os.environ.get("SENDGRID_FROM_EMAIL")

        if not all([self._api_key, self._from_email]):
            raise ValueError(
                "Missing SendGrid credentials in environment. "
                "Required: SENDGRID_API_KEY, SENDGRID_FROM_EMAIL"
            )

        # Lazy import — sendgrid is an optional runtime dependency
        from sendgrid import SendGridAPIClient  # type: ignore[import-untyped]

        self._client = SendGridAPIClient(self._api_key)

    @property
    def channel_name(self) -> str:
        return "email"

    async def dispatch(self, notification: dict) -> str:
        """Send an email via SendGrid.

        On success the provider message ID (from the
        ``X-Message-Id`` response header) is stored in
        ``notification["provider_message_id"]``.  On failure the reason
        is stored in ``notification["failure_reason"]``.

        Returns ``'sent'`` or ``'failed'``.
        """
        from sendgrid.helpers.mail import Mail  # type: ignore[import-untyped]

        recipient = notification.get("recipient_reference", "")
        subject = notification.get("subject", "Notification")
        body = notification.get("message_body", "")

        mail = Mail(
            from_email=self._from_email,
            to_emails=recipient,
            subject=subject,
            plain_text_content=body,
        )

        try:
            response = self._client.send(mail)

            if response.status_code in (200, 201, 202):
                # SendGrid returns the message ID in the X-Message-Id header
                message_id = None
                if response.headers:
                    message_id = response.headers.get("X-Message-Id")
                if message_id:
                    notification["provider_message_id"] = message_id
                logger.info(
                    "[EMAIL] Sent to %s — Message-Id %s (status %s)",
                    recipient,
                    message_id,
                    response.status_code,
                )
                return "sent"
            else:
                reason = f"SendGrid returned status {response.status_code}"
                if response.status_code == 429:
                    reason = f"Rate limited: SendGrid returned {response.status_code}"
                notification["failure_reason"] = reason
                logger.warning(
                    "[EMAIL] Failed to send to %s: %s",
                    recipient,
                    reason,
                )
                return "failed"
        except Exception as exc:
            notification["failure_reason"] = str(exc)
            logger.error(
                "[EMAIL] Unexpected error sending to %s: %s",
                recipient,
                exc,
            )
            return "failed"
