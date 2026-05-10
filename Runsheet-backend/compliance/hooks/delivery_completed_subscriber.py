"""Subscriber for the order.delivered event — fires delivery_completed notification.

Subscribes to the OrderService's ``order.delivered`` event via the public
subscription helper (same pattern as KFactorDeliverySubscriber and the
commerce invoice generation subscriber). When a delivery is confirmed via
POD, sends the ``delivery_completed`` notification to the customer's
delivery contact with delivery details (gallons, price, PO, driver).

The handler is fault-tolerant: notification failures are logged but NEVER
block the POD confirmation / delivery pipeline. The OrderService catches
exceptions from subscribers so even if this handler raises, the delivery
transition completes.

Deduplication: Uses a per-instance set of delivered order_ids to ensure
the notification fires at most once per delivery within the same process
lifecycle. The NotificationService's rule engine provides additional
deduplication at the persistence layer.

Validates: Requirement 12.7
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


class DeliveryCompletedSubscriber:
    """Handles order.delivered events to send delivery_completed notifications.

    This subscriber is registered on the OrderService's public
    subscription helper for the ``order.delivered`` event. On each
    event it:

    1. Checks deduplication (skip if already notified for this order_id).
    2. Extracts delivery details from the order dict.
    3. Calls ``NotificationService.notify_event()`` with event_type
       ``delivery_completed`` and the template placeholders.

    Failures are logged but MUST NOT block the delivery pipeline.

    Args:
        notification_service: The NotificationService instance for
            sending notifications.

    Validates: Requirement 12.7
    """

    def __init__(self, notification_service: Any) -> None:
        """Initialize the subscriber.

        Args:
            notification_service: A NotificationService instance for
                dispatching delivery_completed notifications.
        """
        self._notification_service = notification_service
        # Per-instance deduplication set — ensures the notification fires
        # at most once per delivery within the same process lifecycle.
        self._notified_orders: Set[str] = set()

    async def __call__(self, order: Dict[str, Any]) -> None:
        """Handle an order.delivered event.

        Called by the OrderService's event subscriber mechanism after
        an order transitions to ``delivered`` (POD confirmed).

        Args:
            order: The order document dict (post-transition).
        """
        order_id = order.get("order_id", "")
        tenant_id = order.get("tenant_id", "")
        customer_id = order.get("customer_id", "")

        # Deduplication: skip if already notified for this order
        if order_id in self._notified_orders:
            logger.debug(
                "DeliveryCompletedSubscriber: already notified for "
                "order=%s tenant=%s — skipping duplicate",
                order_id,
                tenant_id,
            )
            return

        # Must have customer_id to send notification
        if not customer_id:
            logger.debug(
                "DeliveryCompletedSubscriber: no customer_id on order=%s "
                "tenant=%s — skipping notification",
                order_id,
                tenant_id,
            )
            return

        # Must have notification service
        if self._notification_service is None:
            logger.debug(
                "DeliveryCompletedSubscriber: no notification_service "
                "available — skipping notification for order=%s",
                order_id,
            )
            return

        # Build event_data with template placeholders for delivery_completed
        event_data = self._build_event_data(order)

        # Fire the notification — non-blocking (errors logged, not raised)
        try:
            await self._notification_service.notify_event(
                event_type="delivery_completed",
                event_data=event_data,
                tenant_id=tenant_id,
            )
            # Mark as notified (deduplication)
            self._notified_orders.add(order_id)
            logger.info(
                "DeliveryCompletedSubscriber: fired delivery_completed "
                "notification for order=%s customer=%s tenant=%s",
                order_id,
                customer_id,
                tenant_id,
            )
        except Exception as exc:
            # Fault-tolerant: log the error but do not re-raise.
            # The delivery pipeline must not be blocked by notification
            # failures.
            logger.error(
                "DeliveryCompletedSubscriber: failed to send "
                "delivery_completed notification for order=%s tenant=%s: %s",
                order_id,
                tenant_id,
                exc,
            )

    @staticmethod
    def _build_event_data(order: Dict[str, Any]) -> Dict[str, Any]:
        """Extract template placeholders from the order document.

        Maps order fields to the delivery_completed template placeholders:
        - customer_name, delivery_date, product_name, gross_gallons,
          net_gallons, unit_price, total_amount, PO_number, driver_name

        Args:
            order: The order document dict.

        Returns:
            Dict of event_data suitable for NotificationService.notify_event().
        """
        # Derive delivery_date from updated_at (the timestamp of the
        # delivered transition) or fall back to current UTC time.
        delivery_date_raw = order.get("updated_at") or order.get("last_event_timestamp")
        if delivery_date_raw:
            # If it's already a string, use it; otherwise format it
            if isinstance(delivery_date_raw, str):
                # Try to parse and format as a human-readable date
                try:
                    dt = datetime.fromisoformat(
                        delivery_date_raw.replace("Z", "+00:00")
                    )
                    delivery_date = dt.strftime("%Y-%m-%d %H:%M UTC")
                except (ValueError, TypeError):
                    delivery_date = delivery_date_raw
            else:
                delivery_date = str(delivery_date_raw)
        else:
            delivery_date = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )

        # Compute unit_price from unit_price_cents if available
        unit_price_cents = order.get("unit_price_cents")
        if unit_price_cents is not None:
            unit_price = f"{int(unit_price_cents) / 100:.2f}"
        else:
            unit_price = "N/A"

        # Compute total_amount from subtotal_cents if available
        subtotal_cents = order.get("subtotal_cents")
        if subtotal_cents is not None:
            total_amount = f"{int(subtotal_cents) / 100:.2f}"
        else:
            total_amount = "N/A"

        # Map product_code to a human-readable product_name
        product_code = order.get("product_code", "")
        product_name = order.get("product_name") or _product_code_to_name(
            product_code
        )

        return {
            "customer_id": order.get("customer_id", ""),
            "customer_name": order.get("customer_name", "Valued Customer"),
            "delivery_date": delivery_date,
            "product_name": product_name,
            "gross_gallons": str(order.get("gallons_requested", "N/A")),
            "net_gallons": str(order.get("net_gallons", order.get("gallons_requested", "N/A"))),
            "unit_price": unit_price,
            "total_amount": total_amount,
            "PO_number": order.get("po_number", "N/A"),
            "driver_name": order.get("driver_name", order.get("assigned_driver_id", "N/A")),
            "order_id": order.get("order_id", ""),
        }


def _product_code_to_name(product_code: str) -> str:
    """Map common fuel product codes to human-readable names.

    Falls back to the raw product_code if no mapping exists.
    """
    _PRODUCT_NAMES = {
        "DIESEL_2": "Diesel #2",
        "DIESEL_1": "Diesel #1",
        "OFF_ROAD_DIESEL": "Off-Road Diesel (Dyed)",
        "HEATING_OIL": "Heating Oil",
        "PROPANE": "Propane",
        "GASOLINE_87": "Gasoline 87",
        "GASOLINE_89": "Gasoline 89",
        "GASOLINE_93": "Gasoline 93",
        "KEROSENE": "Kerosene",
        "DEF": "Diesel Exhaust Fluid (DEF)",
    }
    return _PRODUCT_NAMES.get(product_code, product_code or "Fuel")
