"""Notifications for customer-portal delivery requests (portal-fixes B2, C2).

* A new request: dispatchers hear about it through the activity log, the
  feed behind the staff notification bell (``/ws/agent-activity``), and the
  requester gets a "request received" email.
* A dispatcher confirms (release-hold) or declines (cancel) a request that
  was awaiting confirmation: the requester gets a "confirmed" or "declined"
  email.

Every method is best effort and never raises: a notification failure must
not fail the request or the dispatcher's action. Emails go only to the
portal user who made the request (from the ``order_placed`` event), never to
other addresses, and carry no price, cost, margin or internal id. Without
SendGrid configured no email is sent and the activity entry still is.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from portal.services import portal_email

logger = logging.getLogger(__name__)

#: Customer-facing product names (same as ``design/tokens.json`` ``product``).
PRODUCT_NAMES = {
    "GASOLINE_REG": "Regular unleaded",
    "GASOLINE_PREM": "Premium unleaded",
    "DIESEL_2": "Diesel #2 (on-road)",
    "OFF_ROAD_DIESEL": "Off-road dyed diesel",
    "HEATING_OIL": "Heating oil (No. 2)",
    "KEROSENE": "Kerosene (K-1)",
    "ETHANOL_E85": "E85 flex fuel",
    "PROPANE": "Propane",
    "DEF": "Diesel exhaust fluid",
}


def product_name(code: Optional[str]) -> str:
    if not code:
        return "Fuel"
    if code in PRODUCT_NAMES:
        return PRODUCT_NAMES[code]
    text = code.replace("_", " ").replace("-", " ").strip().lower()
    return text[:1].upper() + text[1:] if text else "Fuel"


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _as_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _clock(d: datetime) -> str:
    hour = d.hour % 12 or 12
    return f"{hour}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def _day(d: datetime) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def format_window(start: Any, end: Any, tz_name: str) -> str:
    """The portal's window format in the tenant zone:
    "Fri 9 Oct, 9:00 AM – 1:00 PM CDT"; a whole day is "Fri 9 Oct"."""
    s, e = _as_dt(start), _as_dt(end)
    if s is None:
        return "Not scheduled"
    try:
        zone = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — unknown zone: the Board's default
        zone = ZoneInfo("America/Chicago")
    s = s.astimezone(zone)
    if e is None:
        return f"{_day(s)}, {_clock(s)} {s.tzname()}"
    e = e.astimezone(zone)
    hours = (e - s).total_seconds() / 3600
    if (s.hour, s.minute) == (0, 0) and (e.hour, e.minute) == (0, 0) and 23 <= hours <= 25:
        return _day(s)
    if s.date() == e.date():
        return f"{_day(s)}, {_clock(s)} – {_clock(e)} {e.tzname()}"
    return f"{_day(s)}, {_clock(s)} – {_day(e)}, {_clock(e)} {e.tzname()}"


def format_quantity(gallons: Any, fill_to_full: bool) -> str:
    if fill_to_full or gallons in (None, ""):
        return "Fill to full"
    try:
        return f"{round(float(gallons)):,} gal"
    except (TypeError, ValueError):
        return "Fill to full"


def _portal_link() -> str:
    origin = (os.environ.get("SUPERTOKENS_WEBSITE_DOMAIN") or "").rstrip("/")
    return f"{origin}/portal/orders" if origin else "/portal/orders"


async def _email_for_user(user_id: Optional[str]) -> str:
    if not user_id:
        return ""
    try:
        from auth.password_admin import _email_for_st_user_id

        return (await _email_for_st_user_id(user_id)) or ""
    except Exception:  # noqa: BLE001 — no address, no email
        return ""


class PortalOrderNotifier:
    """Sends the portal request notifications. Collaborators are optional."""

    def __init__(
        self,
        *,
        activity_log: Any = None,
        order_repository: Any = None,
        tank_repository: Any = None,
        email_for_user: Optional[Callable[[Optional[str]], Any]] = None,
        sender: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._activity = activity_log
        self._orders = order_repository
        self._tanks = tank_repository
        self._email_for_user = email_for_user or _email_for_user
        self._send = sender or portal_email.send_portal_email

    # -- data ---------------------------------------------------------------

    async def _tank_name(self, tenant_id: str, tank_id: Optional[str]) -> str:
        from portal.services.projection import tank_label

        if not tank_id:
            return "Your tank"
        tank = None
        if self._tanks is not None:
            try:
                tank = await self._tanks.get(tenant_id, tank_id)
            except Exception:  # noqa: BLE001 — label from the id
                tank = None
        return tank_label(
            tank_id,
            _get(tank, "external_tank_id") if tank else None,
            _get(tank, "display_name") if tank else None,
        )

    async def _template_data(self, tenant_id: str, order: Any) -> dict:
        from portal.services.ordering import tenant_time_zone
        from portal.services.supplier import supplier_name

        po = _get(order, "po_number")
        return {
            "supplier_name": await supplier_name(tenant_id),
            "customer_name": _get(order, "customer_name") or "",
            "tank_name": await self._tank_name(tenant_id, _get(order, "customer_tank_id")),
            "product_name": product_name(_get(order, "product_code")),
            "quantity": format_quantity(
                _get(order, "gallons_requested"), bool(_get(order, "fill_to_full"))
            ),
            "delivery_window": format_window(
                _get(order, "delivery_window_start") or _get(order, "window_start"),
                _get(order, "delivery_window_end") or _get(order, "window_end"),
                tenant_time_zone(tenant_id),
            ),
            "po_line": f"PO: {po}\n" if po else "",
            "portal_link": _portal_link(),
        }

    async def _requester(self, tenant_id: str, order_id: str) -> Optional[str]:
        if self._orders is None:
            return None
        try:
            events = await self._orders.get_events_for_order(tenant_id, order_id)
        except Exception:  # noqa: BLE001
            return None
        for ev in events or []:
            if _get(ev, "event_type") == "order_placed":
                payload = _get(ev, "event_payload") or {}
                return _get(payload, "actor_user_id")
        return None

    async def _email(self, tenant_id: str, event_type: str, order: Any, user_id: Optional[str]) -> bool:
        if not portal_email.email_channel_configured():
            return False
        recipient = await self._email_for_user(user_id)
        if not recipient:
            return False
        mail = await portal_email.render_portal_email(
            event_type, await self._template_data(tenant_id, order), tenant_id=tenant_id
        )
        return bool(await self._send(recipient, mail))

    # -- events -------------------------------------------------------------

    async def request_received(self, tenant_id: str, order: Any, *, requester_user_id: str) -> None:
        """A new request: tell dispatchers, email the requester."""
        try:
            await self._log_for_dispatchers(tenant_id, order)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Portal request: dispatcher notice failed: %s", type(exc).__name__)
        try:
            await self._email(tenant_id, portal_email.REQUEST_RECEIVED, order, requester_user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Portal request: received email failed: %s", type(exc).__name__)

    async def request_resolved(self, tenant_id: str, order: Any, *, confirmed: bool) -> None:
        """A dispatcher confirmed or declined a request: email the requester."""
        event_type = portal_email.REQUEST_CONFIRMED if confirmed else portal_email.REQUEST_DECLINED
        try:
            user_id = await self._requester(tenant_id, _get(order, "order_id"))
            await self._email(tenant_id, event_type, order, user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Portal request: %s email failed: %s", event_type, type(exc).__name__)

    async def _log_for_dispatchers(self, tenant_id: str, order: Any) -> None:
        if self._activity is None:
            return
        data = await self._template_data(tenant_id, order)
        # The bell shows "{action_type}: {tool_name}", so both are readable.
        await self._activity.log(
            {
                "agent_id": "system",
                "action_type": "New delivery request",
                "tool_name": (
                    f"{data['customer_name'] or 'A customer'} · {data['tank_name']} · "
                    f"{data['quantity']} · {data['delivery_window']}"
                ),
                "tenant_id": tenant_id,
                "outcome": "pending_approval",
                "risk_level": "low",
                "details": {
                    "order_id": _get(order, "order_id"),
                    "intake_channel": "web_portal",
                    "awaiting": "dispatcher_confirmation",
                },
            }
        )


_notifier: Optional[PortalOrderNotifier] = None


def configure_portal_order_notifier(notifier: Optional[PortalOrderNotifier]) -> None:
    global _notifier
    _notifier = notifier


def get_portal_order_notifier() -> Optional[PortalOrderNotifier]:
    return _notifier


async def notify_staff_resolution(tenant_id: str, before: Any, after: Any, *, confirmed: bool) -> None:
    """Called by the staff release-hold / cancel routes. Only a portal request
    that was awaiting confirmation (``before``) counts; never raises."""
    from fuel.order_models import PORTAL_REVIEW_HOLD_REASON

    try:
        if _get(before, "intake_channel") != "web_portal":
            return
        if _get(before, "status") != "on_hold" or _get(before, "hold_reason") != PORTAL_REVIEW_HOLD_REASON:
            return
        notifier = _notifier
        if notifier is None:
            return
        await notifier.request_resolved(tenant_id, after or before, confirmed=confirmed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Portal request resolution notice failed: %s", type(exc).__name__)


__all__ = [
    "PRODUCT_NAMES",
    "PortalOrderNotifier",
    "configure_portal_order_notifier",
    "format_quantity",
    "format_window",
    "get_portal_order_notifier",
    "notify_staff_resolution",
    "product_name",
]
