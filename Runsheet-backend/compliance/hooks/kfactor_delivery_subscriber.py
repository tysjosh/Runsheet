"""Subscriber for the order.delivered event — triggers K-factor variance computation.

Subscribes to the OrderService's ``order.delivered`` event via the public
subscription helper (same pattern as invoice generation). When a delivery
is completed for an auto-fill or keep-full customer, computes the variance
between predicted and actual gallons using the KFactorCalibrationService.

The handler is fault-tolerant: errors are logged but never block the
delivery pipeline. The OrderService catches exceptions from subscribers
so even if this handler raises, the delivery transition completes.

Validates: Requirement 9.1
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Call types that use HDD-based K-factor predictions (auto-fill customers)
AUTOFILL_CALL_TYPES = {"auto_fill", "keep_full"}


class KFactorDeliverySubscriber:
    """Handles order.delivered events to trigger K-factor variance computation.

    This subscriber is registered on the OrderService's public
    subscription helper for the ``order.delivered`` event. On each
    event it:

    1. Checks if the delivery is for an auto-fill or keep-full customer
       (skips will-call deliveries).
    2. Calls ``KFactorCalibrationService.compute_variance(delivery_id, tenant_id)``.
    3. Logs the result (flagged or not).
    4. If flagged, optionally sends a notification to the operator.

    Failures are logged but MUST NOT block the delivery pipeline.

    Args:
        kfactor_service: The KFactorCalibrationService instance.
        notification_service: Optional notification service for alerting
            operators when variance exceeds threshold.

    Validates: Requirement 9.1
    """

    def __init__(
        self,
        kfactor_service: Any,
        notification_service: Optional[Any] = None,
    ) -> None:
        self._kfactor_service = kfactor_service
        self._notification_service = notification_service

    async def __call__(self, order: Dict[str, Any]) -> None:
        """Handle an order.delivered event.

        Called by the OrderService's event subscriber mechanism after
        an order transitions to ``delivered``.

        Args:
            order: The order document dict (post-transition).
        """
        order_id = order.get("order_id", "")
        tenant_id = order.get("tenant_id", "")
        call_type = order.get("call_type", "")

        # Only process auto-fill and keep-full deliveries (skip will-call)
        if call_type not in AUTOFILL_CALL_TYPES:
            logger.debug(
                "KFactorDeliverySubscriber: skipping order=%s tenant=%s — "
                "call_type '%s' is not auto-fill/keep-full",
                order_id,
                tenant_id,
                call_type,
            )
            return

        # Compute variance — fault-tolerant (errors logged, not raised)
        try:
            variance = await self._kfactor_service.compute_variance(
                delivery_id=order_id,
                tenant_id=tenant_id,
            )

            if variance.flagged:
                logger.warning(
                    "KFactorDeliverySubscriber: FLAGGED variance for "
                    "order=%s tank=%s tenant=%s: predicted=%.2f "
                    "actual=%.2f variance=%.2f%% suggested_kfactor=%.4f",
                    order_id,
                    variance.tank_id,
                    tenant_id,
                    variance.predicted_gallons,
                    variance.actual_gallons,
                    variance.variance_percent,
                    variance.suggested_kfactor or 0.0,
                )
                # Notify operator if notification service is available
                await self._notify_operator_if_flagged(variance, tenant_id)
            else:
                logger.info(
                    "KFactorDeliverySubscriber: variance within threshold "
                    "for order=%s tank=%s tenant=%s: variance=%.2f%%",
                    order_id,
                    variance.tank_id,
                    tenant_id,
                    variance.variance_percent,
                )

        except Exception as exc:
            # Fault-tolerant: log the error but do not re-raise.
            # The delivery pipeline must not be blocked by K-factor
            # computation failures (e.g., missing weather data, no
            # previous delivery, tank not found).
            logger.error(
                "KFactorDeliverySubscriber: failed to compute variance "
                "for order=%s tenant=%s: %s",
                order_id,
                tenant_id,
                exc,
            )

    async def _notify_operator_if_flagged(
        self,
        variance: Any,
        tenant_id: str,
    ) -> None:
        """Send a notification to the operator when variance is flagged.

        This is a best-effort notification — failures are logged but
        do not propagate.
        """
        if self._notification_service is None:
            return

        try:
            await self._notification_service.send(
                template_key="kfactor_variance_alert",
                tenant_id=tenant_id,
                context={
                    "tank_id": variance.tank_id,
                    "delivery_id": variance.delivery_id,
                    "predicted_gallons": variance.predicted_gallons,
                    "actual_gallons": variance.actual_gallons,
                    "variance_percent": variance.variance_percent,
                    "suggested_kfactor": variance.suggested_kfactor,
                },
            )
        except Exception as exc:
            logger.warning(
                "KFactorDeliverySubscriber: failed to send operator "
                "notification for tank=%s tenant=%s: %s",
                variance.tank_id,
                tenant_id,
                exc,
            )
