"""Portal invite link flag and invite email (customer portal PE5).

The invite and the password reset share SuperTokens' reset token and the
``/auth/reset-password`` page. An invite link carries ``invite=1`` so that
page can welcome a new user instead of talking about a reset, and the invite
email uses its own template instead of SuperTokens' reset email.

Delivery goes through the notifications email channel
(:class:`notifications.services.sendgrid_email_dispatcher.SendGridEmailDispatcher`)
when its credentials are already configured in the environment. Nothing here
creates or reads other credentials. Without them :func:`send_invite_email`
returns ``False`` and the caller falls back to SuperTokens' reset email, which
is the behaviour before PE5.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

INVITE_PARAM = "invite"


def with_invite_flag(link: str) -> str:
    """``link`` with ``invite=1`` in its query (idempotent; fragment kept)."""
    parts = urlsplit(link)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != INVITE_PARAM]
    query.append((INVITE_PARAM, "1"))
    return urlunsplit(parts._replace(query=urlencode(query)))


@dataclass(frozen=True)
class InviteEmail:
    subject: str
    text: str


def render_invite_email(*, supplier_name: str, customer_name: str, link: str) -> InviteEmail:
    """The invite email (plain text; no tracking, no internal ids)."""
    supplier = (supplier_name or "").strip() or "Your fuel supplier"
    customer = (customer_name or "").strip()
    for_whom = f" for {customer}" if customer else ""
    subject = f"{supplier} invited you to their customer portal"
    text = (
        f"Hello,\n\n"
        f"{supplier} has set up a customer portal account{for_whom}. In the "
        f"portal you can see your tank levels, request deliveries, follow your "
        f"orders and view your invoices.\n\n"
        f"Choose your password to get started:\n{link}\n\n"
        f"The link works once and expires. If it has expired, ask "
        f"{supplier} to send a new invite, or use \"Forgot password?\" on the "
        f"sign-in page.\n\n"
        f"If you weren't expecting this email, you can ignore it.\n"
    )
    return InviteEmail(subject=subject, text=text)


async def send_invite_email(email: str, content: InviteEmail) -> bool:
    """Send through the configured email channel; ``False`` when there is none
    or the send fails (the caller falls back to the reset email)."""
    if not (os.environ.get("SENDGRID_API_KEY") and os.environ.get("SENDGRID_FROM_EMAIL")):
        return False
    try:
        from notifications.services.sendgrid_email_dispatcher import SendGridEmailDispatcher

        dispatcher = SendGridEmailDispatcher()
        outcome = await dispatcher.dispatch(
            {
                "recipient_reference": email,
                "subject": content.subject,
                "message_body": content.text,
            }
        )
    except Exception as exc:  # noqa: BLE001 — best effort, the reset email follows
        logger.warning("Portal invite email could not be sent: %s", type(exc).__name__)
        return False
    return outcome == "sent"


__all__ = [
    "INVITE_PARAM",
    "InviteEmail",
    "render_invite_email",
    "send_invite_email",
    "with_invite_flag",
]
