"""Order-event subscriber for the margin feed (design "Stage inputs").

Registered on the OrderService's public subscription helper, following
``commerce/hooks/order_delivered_subscriber.py``:

* ``order.dispatched`` -> ``order_estimate`` record (first status with a
  loading plan, so a terminal can be attributed);
* ``order.delivered`` -> ``delivery`` record, including the
  ``reconcile_delivery_result`` replay (a late POD sync);
* ``order.cancelled`` / ``order.failed`` -> void the ``order_estimate``.

The handler hands the order to :class:`MarginHook`, which checks
``commerce_margin_feed_enabled`` per event, deep-copies the order and
schedules the work in the background. Nothing here awaits margin work and
nothing raises, so the order transition path is never slowed or blocked.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from commerce.services.margin_service import ORDER_EVENTS

logger = logging.getLogger(__name__)


class MarginOrderSubscriber:
    """``async def __call__(order)`` for one order event name."""

    def __init__(self, margin_hook: Any, event_name: str) -> None:
        if event_name not in ORDER_EVENTS:
            raise ValueError(f"margin feed does not handle {event_name!r}")
        self._hook = margin_hook
        self.event_name = event_name
        self.__name__ = f"MarginOrderSubscriber[{event_name}]"

    async def __call__(self, order: Dict[str, Any]) -> None:
        try:
            self._hook.order_event(order, self.event_name)
        except Exception as exc:  # noqa: BLE001 - never block the order path
            logger.error(
                "MarginOrderSubscriber: %s failed for order=%s tenant=%s: %s",
                self.event_name,
                order.get("order_id") if hasattr(order, "get") else None,
                order.get("tenant_id") if hasattr(order, "get") else None,
                type(exc).__name__,
            )


def register_margin_order_subscribers(order_service: Any, margin_hook: Any) -> List[MarginOrderSubscriber]:
    """Subscribe one handler per margin order event; returns the handlers."""

    # Constant event names, one call each: the loading-plan subscriber pin
    # (tests/unit/test_loading_plan_executor_side_effects.py) scans for them.
    dispatched = MarginOrderSubscriber(margin_hook, "order.dispatched")
    delivered = MarginOrderSubscriber(margin_hook, "order.delivered")
    cancelled = MarginOrderSubscriber(margin_hook, "order.cancelled")
    failed = MarginOrderSubscriber(margin_hook, "order.failed")
    order_service.subscribe("order.dispatched", dispatched)
    order_service.subscribe("order.delivered", delivered)
    order_service.subscribe("order.cancelled", cancelled)
    order_service.subscribe("order.failed", failed)
    return [dispatched, delivered, cancelled, failed]


__all__ = ["MarginOrderSubscriber", "register_margin_order_subscribers"]
