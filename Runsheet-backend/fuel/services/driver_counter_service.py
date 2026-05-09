"""
Driver Counter Service — atomic counter updates for driver workload metrics.

Implements :class:`DriverCounterService` with:

* ``increment_counters`` — atomically adjust ``active_order_count`` and
  ``completed_today`` via the painless script from design §3.

This service is called by :class:`OrderService.apply_status_transition`
whenever an order transitions from ``dispatched | in_transit`` to
``delivered | failed | cancelled``, ensuring driver workload metrics
stay accurate without race conditions.

Validates: Requirements 3.2.1, 3.2.2.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["DriverCounterService"]


# ---------------------------------------------------------------------------
# Painless script for atomic counter increment (design §3)
# ---------------------------------------------------------------------------

DRIVER_COUNTER_SCRIPT = """
if (params.delta_active != 0) {
    ctx._source.active_order_count =
        (ctx._source.active_order_count != null ? ctx._source.active_order_count : 0)
        + params.delta_active;
    if (ctx._source.active_order_count < 0) ctx._source.active_order_count = 0;
}
if (params.delta_completed != 0) {
    ctx._source.completed_today =
        (ctx._source.completed_today != null ? ctx._source.completed_today : 0)
        + params.delta_completed;
}
ctx._source.last_event_timestamp = params.now;
ctx._source.updated_at = params.now;
""".strip()


# ---------------------------------------------------------------------------
# DriverCounterService
# ---------------------------------------------------------------------------


class DriverCounterService:
    """Atomic counter updates for driver workload metrics.

    Wraps :class:`DriverRepository.increment_counters` to provide a
    clean interface for the :class:`OrderService` to call on every
    status transition that affects driver counts.

    The painless script (design §3) ensures:
        - ``active_order_count`` is incremented/decremented atomically.
        - ``active_order_count`` is clamped at 0 (never goes negative).
        - ``completed_today`` is incremented atomically.
        - ``last_event_timestamp`` and ``updated_at`` are stamped.

    Constructor dependencies:
        driver_repo: DriverRepository instance with ``increment_counters``.
    """

    def __init__(self, *, driver_repo: Any) -> None:
        if driver_repo is None:
            raise ValueError("driver_repo must not be None")
        self._driver_repo = driver_repo

    async def increment_counters(
        self,
        driver_id: str,
        tenant_id: str,
        delta_active: int = 0,
        delta_completed: int = 0,
    ) -> bool:
        """Atomically adjust a driver's workload counters.

        Delegates to :meth:`DriverRepository.increment_counters` which
        uses the painless script from design §3 for race-free updates.

        Args:
            driver_id: The driver whose counters to adjust.
            tenant_id: The tenant owning the driver (for ownership
                validation).
            delta_active: Amount to add to ``active_order_count``.
                Positive when a driver is assigned a new order (dispatch),
                negative when an order completes or is cancelled.
            delta_completed: Amount to add to ``completed_today``.
                Typically +1 when an order transitions to ``delivered``.

        Returns:
            ``True`` if the update was applied, ``False`` if the driver
            was not found or the operation was a noop.

        Raises:
            DriverCrossTenantAccessError: If the driver belongs to
                another tenant.
        """
        if not driver_id or not driver_id.strip():
            logger.warning(
                "DriverCounterService.increment_counters: "
                "empty driver_id, skipping"
            )
            return False

        if not tenant_id or not tenant_id.strip():
            logger.warning(
                "DriverCounterService.increment_counters: "
                "empty tenant_id, skipping"
            )
            return False

        if delta_active == 0 and delta_completed == 0:
            # No-op — nothing to update
            return False

        try:
            result = await self._driver_repo.increment_counters(
                tenant_id=tenant_id,
                driver_id=driver_id,
                delta_active=delta_active,
                delta_completed=delta_completed,
            )
            if result:
                logger.debug(
                    "DriverCounterService: updated counters for "
                    "driver=%s (delta_active=%d, delta_completed=%d)",
                    driver_id,
                    delta_active,
                    delta_completed,
                )
            return result
        except Exception as exc:
            logger.error(
                "DriverCounterService.increment_counters failed for "
                "driver=%s, tenant=%s: %s",
                driver_id,
                tenant_id,
                exc,
            )
            raise
