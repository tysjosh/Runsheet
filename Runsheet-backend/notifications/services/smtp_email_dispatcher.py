"""
Provider-neutral email dispatcher over SMTP (stdlib ``smtplib``).

Extends ``ChannelDispatcher`` to deliver email through any SMTP relay
(staging uses Mailtrap: ``live.smtp.mailtrap.io:587``, username ``api``,
password = the Mailtrap API token). Settings come from the same ``SMTP_*``
environment variables SuperTokens' auth email reads
(``auth/supertokens_init.py::_build_email_delivery``), so one secret feeds
both paths. A ``ValueError`` is raised at init when a required one is
missing.

Same non-blocking, bounded shape as ``SendGridEmailDispatcher``: the
synchronous ``smtplib`` session runs in a dedicated executor under
``asyncio.wait_for(SEND_TIMEOUT_SECONDS)``, and every socket operation has the
same timeout, so a slow or hung relay never stalls the event loop or the
request handlers that await this. Failures are logged by exception type and
SMTP code only; the password is never logged.

Transport: ``SMTP_SECURE=true`` uses implicit TLS (port 465). Otherwise
STARTTLS is required before login (port 587); a relay that doesn't offer it
fails the send rather than receiving the password in clear.
"""

import asyncio
import logging
import os
import smtplib
import ssl
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from notifications.services.channel_dispatchers import ChannelDispatcher

logger = logging.getLogger(__name__)

#: Upper bound for one SMTP send (socket timeout and the awaited wait).
#: Module-level so tests can shorten it.
SEND_TIMEOUT_SECONDS: float = 10.0

#: Dedicated threads for the synchronous SMTP session, so a stalled send
#: never occupies the loop's default executor and loop shutdown never waits
#: on it.
_SEND_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="smtp-send")

_REQUIRED_ENV = ("SMTP_HOST", "SMTP_FROM_EMAIL", "SMTP_PASSWORD")


def smtp_email_configured() -> bool:
    """True when ``SMTP_HOST``, ``SMTP_FROM_EMAIL`` and ``SMTP_PASSWORD`` are
    all set (presence only; the password is never read here)."""
    return all((os.environ.get(name) or "").strip() for name in _REQUIRED_ENV)


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


class SmtpEmailDispatcher(ChannelDispatcher):
    """Email dispatcher backed by an SMTP relay (STARTTLS or implicit TLS)."""

    def __init__(self) -> None:
        missing = [name for name in _REQUIRED_ENV if not (os.environ.get(name) or "").strip()]
        if missing:
            raise ValueError(
                "Missing SMTP settings in environment. Required: "
                + ", ".join(_REQUIRED_ENV)
                + f" (missing: {', '.join(missing)})"
            )
        self._host = os.environ["SMTP_HOST"].strip()
        self._from_email = os.environ["SMTP_FROM_EMAIL"].strip()
        self._password = os.environ["SMTP_PASSWORD"]
        self._from_name = (os.environ.get("SMTP_FROM_NAME") or "").strip()
        self._username = (os.environ.get("SMTP_USERNAME") or "").strip() or self._from_email
        self._secure = _truthy(os.environ.get("SMTP_SECURE") or "")
        port_raw = (os.environ.get("SMTP_PORT") or "").strip() or ("465" if self._secure else "587")
        try:
            self._port = int(port_raw)
        except ValueError:
            raise ValueError("SMTP_PORT must be an integer") from None

    @property
    def channel_name(self) -> str:
        return "email"

    def _build_message(self, recipient: str, subject: str, body: str, html_body) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = formataddr((self._from_name, self._from_email)) if self._from_name else self._from_email
        msg["To"] = recipient
        msg["Subject"] = subject
        domain = self._from_email.rsplit("@", 1)[-1] or None
        msg["Message-ID"] = make_msgid(domain=domain)
        msg.set_content(body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        return msg

    def _send_sync(self, msg: EmailMessage, timeout: float) -> None:
        """One SMTP session: connect, TLS, login, send, quit. Runs in
        ``_SEND_EXECUTOR``; every socket operation is bounded by ``timeout``."""
        context = ssl.create_default_context()
        if self._secure:
            server = smtplib.SMTP_SSL(self._host, self._port, timeout=timeout, context=context)
        else:
            server = smtplib.SMTP(self._host, self._port, timeout=timeout)
        try:
            if not self._secure:
                server.ehlo()
                # Raises SMTPNotSupportedError when the relay has no STARTTLS,
                # so the password is never sent over a plain connection.
                server.starttls(context=context)
                server.ehlo()
            server.login(self._username, self._password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 — the send outcome is already decided
                server.close()

    async def dispatch(self, notification: dict) -> str:
        """Send an email over SMTP.

        On success the generated ``Message-ID`` is stored in
        ``notification["provider_message_id"]``. On failure the reason
        (exception type and SMTP code, never credentials) is stored in
        ``notification["failure_reason"]``.

        Returns ``'sent'`` or ``'failed'``.
        """
        recipient = notification.get("recipient_reference", "")
        subject = notification.get("subject", "Notification")
        body = notification.get("message_body", "")
        # Optional HTML alternative (customer portal emails); the plain-text
        # part is always sent as well.
        html_body = notification.get("html_body")

        timeout = SEND_TIMEOUT_SECONDS
        try:
            msg = self._build_message(recipient, subject, body, html_body)
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(_SEND_EXECUTOR, self._send_sync, msg, timeout),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            reason = f"SMTP send timed out after {timeout:g}s"
            notification["failure_reason"] = reason
            logger.warning("[EMAIL] Failed to send to %s: %s", recipient, reason)
            return "failed"
        except Exception as exc:  # noqa: BLE001 — every failure is reported, never raised
            reason = _describe_failure(exc)
            notification["failure_reason"] = reason
            logger.warning("[EMAIL] Failed to send to %s: %s", recipient, reason)
            return "failed"

        message_id = msg["Message-ID"]
        notification["provider_message_id"] = message_id
        logger.info("[EMAIL] Sent to %s via SMTP %s — Message-ID %s", recipient, self._host, message_id)
        return "sent"


def _describe_failure(exc: BaseException) -> str:
    """A credential-free failure reason: the exception type, plus the SMTP
    reply code when there is one. Server reply text is left out on purpose
    (it can echo what the client sent)."""
    name = type(exc).__name__
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        if code == 535 or isinstance(exc, smtplib.SMTPAuthenticationError):
            return f"SMTP authentication failed ({name} {code})"
        return f"SMTP error {code} ({name})"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "SMTP recipient refused (SMTPRecipientsRefused)"
    return f"SMTP send failed ({name})"
