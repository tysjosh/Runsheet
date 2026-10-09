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
    #: The accessible HTML alternative of ``text`` (portal-fixes C2).
    html: str = ""


def _invite_data(supplier_name: str, customer_name: str, link: str) -> dict:
    supplier = (supplier_name or "").strip() or "Your fuel supplier"
    customer = (customer_name or "").strip()
    return {
        "supplier_name": supplier,
        "for_customer": f" for {customer}" if customer else "",
        "link": link,
    }


def render_invite_email(*, supplier_name: str, customer_name: str, link: str) -> InviteEmail:
    """The invite email from the built-in ``portal_invite`` template: plain
    text and HTML, no tracking, no internal ids."""
    from portal.services.portal_email import INVITE, render_default

    mail = render_default(INVITE, _invite_data(supplier_name, customer_name, link))
    return InviteEmail(subject=mail.subject, text=mail.text, html=mail.html)


async def render_invite_email_for_tenant(
    tenant_id: str, *, supplier_name: str, customer_name: str, link: str
) -> InviteEmail:
    """Like :func:`render_invite_email`, using the tenant's edited
    ``portal_invite`` template when there is one."""
    from portal.services.portal_email import INVITE, render_portal_email

    mail = await render_portal_email(
        INVITE, _invite_data(supplier_name, customer_name, link), tenant_id=tenant_id
    )
    return InviteEmail(subject=mail.subject, text=mail.text, html=mail.html)


async def send_invite_email(email: str, content: InviteEmail) -> bool:
    """Send through the SendGrid email channel; ``False`` when it isn't
    configured or the send fails (the caller falls back to the reset email)."""
    if not (os.environ.get("SENDGRID_API_KEY") and os.environ.get("SENDGRID_FROM_EMAIL")):
        return False
    from portal.services.portal_email import PortalEmail, send_portal_email

    try:
        return await send_portal_email(
            email, PortalEmail(subject=content.subject, text=content.text, html=content.html)
        )
    except Exception as exc:  # noqa: BLE001 — best effort, the reset email follows
        logger.warning("Portal invite email could not be sent: %s", type(exc).__name__)
        return False


__all__ = [
    "INVITE_PARAM",
    "InviteEmail",
    "render_invite_email",
    "render_invite_email_for_tenant",
    "send_invite_email",
    "with_invite_flag",
]
