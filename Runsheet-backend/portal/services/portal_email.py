"""Customer-portal emails: templates, rendering and SendGrid delivery
(portal-fixes C2).

Four emails, each an ``email`` template in the notifications templates
mechanism (``notifications.services.template_renderer.DEFAULT_TEMPLATES``,
seeded per tenant and editable through the notification templates API):

* ``portal_invite`` — the invite with the set-password link (PE5 wording);
* ``portal_request_received`` — the customer's delivery request arrived;
* ``portal_request_confirmed`` — a dispatcher confirmed it;
* ``portal_request_declined`` — a dispatcher declined it.

Every email is sent as plain text plus an HTML alternative built from the
same text (escaped, paragraphs, the link as a labelled link), so the two
never disagree. Nothing here carries prices, costs, margins or internal ids.

Delivery uses :class:`notifications.services.sendgrid_email_dispatcher.
SendGridEmailDispatcher`, the dispatcher ``bootstrap/notifications.py``
registers, and only when ``SENDGRID_API_KEY`` and ``SENDGRID_FROM_EMAIL``
are set. Without them every send returns ``False`` and nothing is logged
about the recipient beyond what callers already log. The key is never read
here, only its presence.
"""
from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

INVITE = "portal_invite"
REQUEST_RECEIVED = "portal_request_received"
REQUEST_CONFIRMED = "portal_request_confirmed"
REQUEST_DECLINED = "portal_request_declined"

_REQUEST_PLACEHOLDERS = [
    "supplier_name",
    "customer_name",
    "tank_name",
    "product_name",
    "quantity",
    "delivery_window",
    "po_line",
    "portal_link",
]

#: The four portal email templates (``channel: email``). Same shape as the
#: other ``DEFAULT_TEMPLATES`` entries; appended to that list so a tenant's
#: seed creates them and staff can edit the wording per tenant.
PORTAL_EMAIL_TEMPLATES: List[Dict[str, Any]] = [
    {
        "event_type": INVITE,
        "channel": "email",
        "subject_template": "{supplier_name} invited you to their customer portal",
        "body_template": (
            "Hello,\n\n"
            "{supplier_name} has set up a customer portal account{for_customer}. In the "
            "portal you can see your tank levels, request deliveries, follow your "
            "orders and view your invoices.\n\n"
            "Choose your password to get started:\n{link}\n\n"
            "The link works once and expires. If it has expired, ask "
            "{supplier_name} to send a new invite, or use \"Forgot password?\" on the "
            "sign-in page.\n\n"
            "If you weren't expecting this email, you can ignore it.\n"
        ),
        "placeholders": ["supplier_name", "for_customer", "link"],
    },
    {
        "event_type": REQUEST_RECEIVED,
        "channel": "email",
        "subject_template": "We received your delivery request",
        "body_template": (
            "Hello,\n\n"
            "{supplier_name} received your delivery request. A dispatcher will "
            "confirm it soon, and we'll email you when they do.\n\n"
            "Tank: {tank_name}\n"
            "Product: {product_name}\n"
            "Quantity: {quantity}\n"
            "Delivery window: {delivery_window}\n"
            "{po_line}\n"
            "You can follow the request, or cancel it while it's waiting for "
            "confirmation, in the portal:\n{portal_link}\n"
        ),
        "placeholders": _REQUEST_PLACEHOLDERS,
    },
    {
        "event_type": REQUEST_CONFIRMED,
        "channel": "email",
        "subject_template": "Your delivery request is confirmed",
        "body_template": (
            "Hello,\n\n"
            "{supplier_name} confirmed your delivery request.\n\n"
            "Tank: {tank_name}\n"
            "Product: {product_name}\n"
            "Quantity: {quantity}\n"
            "Delivery window: {delivery_window}\n"
            "{po_line}\n"
            "You can follow the delivery in the portal:\n{portal_link}\n"
        ),
        "placeholders": _REQUEST_PLACEHOLDERS,
    },
    {
        "event_type": REQUEST_DECLINED,
        "channel": "email",
        "subject_template": "Your delivery request was declined",
        "body_template": (
            "Hello,\n\n"
            "{supplier_name} couldn't accept your delivery request, so it has "
            "been declined.\n\n"
            "Tank: {tank_name}\n"
            "Product: {product_name}\n"
            "Quantity: {quantity}\n"
            "Delivery window: {delivery_window}\n"
            "{po_line}\n"
            "Please contact {supplier_name} if you still need this delivery, or "
            "send a new request from the portal:\n{portal_link}\n"
        ),
        "placeholders": _REQUEST_PLACEHOLDERS,
    },
]

_DEFAULTS: Dict[str, Dict[str, Any]] = {t["event_type"]: t for t in PORTAL_EMAIL_TEMPLATES}

#: Optional tenant-override source (the notifications ``TemplateRenderer``),
#: registered from bootstrap. Unset: the defaults above are used.
_template_renderer: Any = None


def configure_portal_email_templates(renderer: Any) -> None:
    global _template_renderer
    _template_renderer = renderer


@dataclass(frozen=True)
class PortalEmail:
    subject: str
    text: str
    html: str = ""


def email_channel_configured() -> bool:
    """True when the SendGrid email channel has its two settings."""
    return bool(os.environ.get("SENDGRID_API_KEY") and os.environ.get("SENDGRID_FROM_EMAIL"))


def _render(template: str, data: Mapping[str, Any]) -> str:
    from notifications.services.template_renderer import render_template

    return render_template(template, dict(data))


_URL_RE = re.compile(r"https?://[^\s<>\"']+")

_LINK_LABELS = {
    INVITE: "Choose your password",
}


def text_to_html(subject: str, text: str, *, link_label: str = "Open the customer portal") -> str:
    """An accessible HTML alternative of ``text``: one ``<p>`` per paragraph,
    line breaks kept, every URL a labelled link, the language set, no images,
    no tracking, 16 px text, colours with AA contrast."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    rendered: List[str] = []
    for para in paragraphs:
        lines = []
        for line in para.split("\n"):
            pieces: List[str] = []
            last = 0
            for m in _URL_RE.finditer(line):
                pieces.append(html.escape(line[last:m.start()]))
                url = html.escape(m.group(0), quote=True)
                pieces.append(
                    f'<a href="{url}" style="color:#1d4ed8;font-weight:600;">'
                    f"{html.escape(link_label)}</a>"
                    f'<br><span style="color:#475569;font-size:14px;word-break:break-all;">{url}</span>'
                )
                last = m.end()
            pieces.append(html.escape(line[last:]))
            lines.append("".join(pieces))
        rendered.append(
            '<p style="margin:0 0 16px 0;">' + "<br>".join(lines) + "</p>"
        )
    body = "\n".join(rendered)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(subject)}</title>\n</head>\n"
        '<body style="margin:0;padding:24px;background:#ffffff;color:#0f172a;'
        'font-family:Arial,Helvetica,sans-serif;font-size:16px;line-height:24px;">\n'
        f'<div role="article" aria-label="{html.escape(subject, quote=True)}" style="max-width:560px;">\n'
        f"{body}\n</div>\n</body>\n</html>\n"
    )


def render_default(event_type: str, data: Mapping[str, Any]) -> PortalEmail:
    """Render the built-in template for ``event_type`` (sync; no lookups)."""
    template = _DEFAULTS[event_type]
    return _finish(event_type, template["subject_template"], template["body_template"], data)


def _finish(event_type: str, subject_t: str, body_t: str, data: Mapping[str, Any]) -> PortalEmail:
    subject = _render(subject_t, data).strip()
    text = _render(body_t, data)
    # A blank optional line (e.g. no PO) leaves an empty line; collapse runs.
    text = re.sub(r"\n{3,}", "\n\n", text)
    label = _LINK_LABELS.get(event_type, "Open the customer portal")
    return PortalEmail(subject=subject, text=text, html=text_to_html(subject, text, link_label=label))


async def render_portal_email(
    event_type: str, data: Mapping[str, Any], *, tenant_id: Optional[str] = None
) -> PortalEmail:
    """The tenant's edited template when one exists, else the default."""
    renderer = _template_renderer
    if renderer is not None and tenant_id:
        try:
            found = await renderer.list_templates(tenant_id, event_type=event_type, channel="email")
        except Exception as exc:  # noqa: BLE001 — the default still renders
            logger.warning("Portal email: template lookup failed: %s", type(exc).__name__)
            found = []
        if found:
            t = found[0]
            return _finish(
                event_type,
                t.get("subject_template") or _DEFAULTS[event_type]["subject_template"],
                t.get("body_template") or _DEFAULTS[event_type]["body_template"],
                data,
            )
    return render_default(event_type, data)


async def send_portal_email(recipient: str, email: PortalEmail) -> bool:
    """Send through SendGrid; ``False`` when it isn't configured or fails.

    The recipient is not logged here (it's PII); the dispatcher logs its own
    outcome line.
    """
    if not recipient or not email_channel_configured():
        return False
    try:
        from notifications.services.sendgrid_email_dispatcher import SendGridEmailDispatcher

        dispatcher = SendGridEmailDispatcher()
        outcome = await dispatcher.dispatch(
            {
                "recipient_reference": recipient,
                "subject": email.subject,
                "message_body": email.text,
                "html_body": email.html or None,
            }
        )
    except Exception as exc:  # noqa: BLE001 — best effort
        logger.warning("Portal email could not be sent: %s", type(exc).__name__)
        return False
    return outcome == "sent"


__all__ = [
    "INVITE",
    "PORTAL_EMAIL_TEMPLATES",
    "PortalEmail",
    "REQUEST_CONFIRMED",
    "REQUEST_DECLINED",
    "REQUEST_RECEIVED",
    "configure_portal_email_templates",
    "email_channel_configured",
    "render_default",
    "render_portal_email",
    "send_portal_email",
    "text_to_html",
]
