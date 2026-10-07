"""Portal projections: the only code that turns stored orders and tanks into
portal response models (design §2.1 E10, §4.5, §7).

Every field a customer can see is named here. Driver, truck, run, claim,
photos, recipient, geotag, signature, OTP, customer phone and email, special
instructions, ``hold_reason``, ``intake_metadata`` and prices are never read.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

from fuel.order_models import PORTAL_REVIEW_HOLD_REASON
from portal.models import (
    PortalNextDelivery,
    PortalOrder,
    PortalOrderTank,
    PortalTank,
    PortalTankDelivery,
    PortalTankForecast,
)

#: Shown for a portal request waiting for a dispatcher (``on_hold`` with
#: :data:`PORTAL_REVIEW_HOLD_REASON`).
AWAITING_CONFIRMATION: Tuple[str, str] = ("awaiting_confirmation", "Awaiting confirmation")

#: Internal ``OrderStatus`` → portal ``(status_code, status_label)`` (PD11).
#: Covers every ``OrderStatus`` literal (T-ORD-MAP). ``on_hold`` maps to
#: :data:`AWAITING_CONFIRMATION` for the portal hold reason; any other hold
#: shows "On hold" without its reason.
ORDER_STATUS_MAP: Dict[str, Tuple[str, str]] = {
    "on_hold": ("on_hold", "On hold"),
    "placed": ("confirmed", "Confirmed"),
    "confirmed": ("confirmed", "Confirmed"),
    "scheduled": ("confirmed", "Confirmed"),
    "dispatched": ("out_for_delivery", "Out for delivery"),
    "in_transit": ("out_for_delivery", "Out for delivery"),
    "delivered": ("delivered", "Delivered"),
    "failed": ("not_delivered", "Not delivered"),
    "cancelled": ("cancelled", "Cancelled"),
}

#: Statuses whose order is the tank's "next delivery" (PD17).
OPEN_DELIVERY_STATUSES: Tuple[str, ...] = (
    "placed", "confirmed", "scheduled", "dispatched", "in_transit",
)


def now_utc() -> datetime:
    """The projection clock (tests patch it)."""
    return datetime.now(timezone.utc)


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def order_status(status: Optional[str], hold_reason: Optional[str]) -> Tuple[str, str]:
    """Portal ``(status_code, status_label)`` for an internal status."""
    if status == "on_hold" and hold_reason == PORTAL_REVIEW_HOLD_REASON:
        return AWAITING_CONFIRMATION
    return ORDER_STATUS_MAP.get(status or "", ("on_hold", "On hold"))


def tank_label(customer_tank_id: str, external_tank_id: Optional[str]) -> str:
    """``external_tank_id`` when set, else ``"Tank …"`` + the id's last 6 chars."""
    if external_tank_id:
        return external_tank_id
    return f"Tank …{customer_tank_id[-6:]}"


def project_order(order: Any, *, tank_labels: Optional[Mapping[str, str]] = None) -> PortalOrder:
    """Build the :class:`PortalOrder` for a stored order (model or dict)."""
    status = _get(order, "status")
    code, label = order_status(status, _get(order, "hold_reason"))
    tank_id = _get(order, "customer_tank_id")
    tank = None
    if tank_id:
        tank = PortalOrderTank(
            customer_tank_id=tank_id,
            label=(tank_labels or {}).get(tank_id) or tank_label(tank_id, None),
        )
    result = _get(order, "delivery_result")
    return PortalOrder(
        order_id=_get(order, "order_id"),
        status_code=code,
        status_label=label,
        product_code=_get(order, "product_code"),
        gallons_requested=_get(order, "gallons_requested"),
        fill_to_full=bool(_get(order, "fill_to_full")),
        window_start=_get(order, "delivery_window_start"),
        window_end=_get(order, "delivery_window_end"),
        po_number=_get(order, "po_number"),
        tank=tank,
        created_at=_get(order, "created_at"),
        delivered_at=_get(result, "delivered_at") if result else None,
        delivered_gallons=_get(result, "actual_gallons") if result else None,
        ticket_number=_get(result, "ticket_number") if result else None,
        cancellable=(
            (code, label) == AWAITING_CONFIRMATION
            and _get(order, "intake_channel") == "web_portal"
        ),
    )


def project_delivery(order: Any) -> PortalTankDelivery:
    """One row of a tank's delivery history (R7.4)."""
    result = _get(order, "delivery_result")
    return PortalTankDelivery(
        order_id=_get(order, "order_id"),
        delivered_at=_get(result, "delivered_at") if result else None,
        delivered_gallons=_get(result, "actual_gallons") if result else None,
        product_code=_get(order, "product_code"),
        ticket_number=_get(result, "ticket_number") if result else None,
    )


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def project_forecast(doc: Optional[Mapping[str, Any]]) -> Optional[PortalTankForecast]:
    """``runout_at = timestamp + hours_to_runout_p50``; null when unusable (DV2)."""
    if not doc:
        return None
    generated_at = _parse_ts(doc.get("timestamp"))
    hours = doc.get("hours_to_runout_p50")
    if generated_at is None or not isinstance(hours, (int, float)) or isinstance(hours, bool):
        return None
    if not math.isfinite(hours):
        return None
    return PortalTankForecast(
        runout_at=generated_at + timedelta(hours=float(hours)),
        days_to_runout=math.floor(float(hours) / 24),
        generated_at=generated_at,
    )


def project_next_delivery(order: Any) -> Optional[PortalNextDelivery]:
    if order is None:
        return None
    _code, label = order_status(_get(order, "status"), _get(order, "hold_reason"))
    return PortalNextDelivery(
        order_id=_get(order, "order_id"),
        status_label=label,
        window_start=_get(order, "delivery_window_start"),
        window_end=_get(order, "delivery_window_end"),
    )


def project_tank(
    tank: Any,
    *,
    forecast: Optional[Mapping[str, Any]],
    next_delivery: Any,
    now: datetime,
    stale_days: int,
) -> PortalTank:
    """Build the :class:`PortalTank` (R7.1-R7.3). Location, k-factor and the
    source system are never read."""
    capacity = float(_get(tank, "capacity_gallons"))
    level = float(_get(tank, "current_level_gallons"))
    percent = round(min(100.0, max(0.0, level / capacity * 100.0)), 1) if capacity > 0 else 0.0
    last_reading_at = _parse_ts(_get(tank, "last_reading_at"))
    stale = last_reading_at is None or now - last_reading_at > timedelta(days=stale_days)
    tank_id = _get(tank, "customer_tank_id")
    return PortalTank(
        customer_tank_id=tank_id,
        label=tank_label(tank_id, _get(tank, "external_tank_id")),
        product_code=_get(tank, "fuel_product_code"),
        capacity_gallons=capacity,
        current_level_gallons=level,
        percent_full=percent,
        last_reading_at=last_reading_at,
        reading_stale=stale,
        forecast=project_forecast(forecast),
        next_delivery=project_next_delivery(next_delivery),
    )


__all__ = [
    "AWAITING_CONFIRMATION",
    "OPEN_DELIVERY_STATUSES",
    "ORDER_STATUS_MAP",
    "now_utc",
    "order_status",
    "project_delivery",
    "project_forecast",
    "project_next_delivery",
    "project_order",
    "project_tank",
    "tank_label",
]
