"""Portal status mapping and projections (design §4.5, §7; ORD-7 / T-ORD-MAP)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import get_args

from fuel.order_models import PORTAL_REVIEW_HOLD_REASON, OrderStatus
from portal.services.projection import (
    ORDER_STATUS_MAP,
    order_status,
    project_forecast,
    tank_label,
)


def test_order_status_map_total():
    """Every OrderStatus literal maps, so a new status can't fall through."""
    statuses = set(get_args(OrderStatus))
    assert statuses == set(ORDER_STATUS_MAP)
    expected = {
        "placed": "confirmed", "confirmed": "confirmed", "scheduled": "confirmed",
        "dispatched": "out_for_delivery", "in_transit": "out_for_delivery",
        "delivered": "delivered", "failed": "not_delivered", "cancelled": "cancelled",
        "on_hold": "on_hold",
    }
    assert {s: order_status(s, None)[0] for s in statuses} == expected
    assert order_status("on_hold", PORTAL_REVIEW_HOLD_REASON) == (
        "awaiting_confirmation", "Awaiting confirmation",
    )
    assert order_status("on_hold", "credit_limit_exceeded") == ("on_hold", "On hold")
    # The portal reason only means "awaiting" while the order is on hold.
    assert order_status("placed", PORTAL_REVIEW_HOLD_REASON) == ("confirmed", "Confirmed")


def test_tank_label():
    assert tank_label("ct_0123456789abcdef", "TANK-7") == "TANK-7"
    assert tank_label("ct_0123456789abcdef", None) == "Tank …abcdef"


def test_forecast_runout_and_days():
    ts = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
    fc = project_forecast({"timestamp": ts.isoformat(), "hours_to_runout_p50": 71.5})
    assert fc.runout_at == ts + timedelta(hours=71.5)
    assert fc.days_to_runout == 2
    assert fc.generated_at == ts
    assert project_forecast(None) is None
    assert project_forecast({"timestamp": ts.isoformat(), "hours_to_runout_p50": None}) is None
    assert project_forecast({"hours_to_runout_p50": 10}) is None
