"""
Order Service — status transition orchestration for Fuel Orders.

Implements :class:`OrderService` with:

* ``apply_status_transition`` — the core method used by both the
  ``PATCH /api/orders/{id}/status`` handler and internal callers
  (e.g. POD finalization). Validates the transition, enforces the
  delivery-window guard, stamps timestamps, appends a status-specific
  event, updates driver counters, broadcasts via WebSocket, and
  optionally mirrors to the legacy surface.

* ``place_on_hold`` — convenience helper that transitions an order to
  ``on_hold`` with a mandatory hold reason.

* ``release_hold`` — convenience helper that re-runs registered intake
  hooks (pricing, credit-check) before attempting the ``on_hold → placed``
  transition. If hooks fail, the order remains ``on_hold`` with an
  updated ``hold_reason``.

Validates: Requirements 1.1.9, 1.2.2, 1.2.3, 2.5.7, 3.2.1, 4.1.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from fuel.order_state_machine import (
    assert_transition,
    assert_window_present_for_transition,
)
from fuel.services.order_id_generator import mint_event_id
from fuel.services.order_metrics import orders_state_transition_rejections_total
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

__all__ = ["OrderService"]

# ---------------------------------------------------------------------------
# Status → event_type mapping
# ---------------------------------------------------------------------------

_STATUS_TO_EVENT_TYPE: Dict[str, str] = {
    "placed": "order_placed",
    "confirmed": "order_confirmed",
    "scheduled": "order_scheduled",
    "dispatched": "order_dispatched",
    "in_transit": "order_in_transit",
    "delivered": "order_delivered",
    "failed": "order_failed",
    "cancelled": "order_cancelled",
    "on_hold": "order_on_hold",
}

# ---------------------------------------------------------------------------
# Driver counter transition rules
# ---------------------------------------------------------------------------

#: Transitions that decrement the driver's active_order_count.
#: When an order moves from dispatched/in_transit to a terminal or
#: cancelled state, the driver has one fewer active order.
_DECREMENT_ACTIVE_TRANSITIONS: set[tuple[str, str]] = {
    ("dispatched", "delivered"),
    ("dispatched", "failed"),
    ("dispatched", "cancelled"),
    ("in_transit", "delivered"),
    ("in_transit", "failed"),
}

#: Transitions that increment the driver's completed_today counter.
_INCREMENT_COMPLETED_TRANSITIONS: set[tuple[str, str]] = {
    ("dispatched", "delivered"),
    ("in_transit", "delivered"),
}


# ---------------------------------------------------------------------------
# OrderService
# ---------------------------------------------------------------------------


class OrderService:
    """Orchestrates order status transitions with side-effects.

    Constructor dependencies:
        order_repo: FuelOrderRepository instance for persistence.
        ws_manager: OrdersWSManager (or compatible) for broadcasting.
        driver_counter_service: Optional DriverCounterService for
            incrementing/decrementing driver counters.
        legacy_dual_writer: Optional LegacyDualWriter for mirroring
            to the legacy shipment surface.
        feature_flag_service: FeatureFlagService for checking overlay
            state (controls whether legacy dual-write is active).
        clock: Optional clock override for testing (defaults to utcnow).
    """

    def __init__(
        self,
        *,
        order_repo: Any,
        ws_manager: Any,
        driver_counter_service: Optional[Any] = None,
        legacy_dual_writer: Optional[Any] = None,
        feature_flag_service: Optional[Any] = None,
        clock: Optional[Callable] = None,
    ) -> None:
        self._order_repo = order_repo
        self._ws_manager = ws_manager
        self._driver_counter_service = driver_counter_service
        self._legacy_dual_writer = legacy_dual_writer
        self._feature_flag_service = feature_flag_service
        self._clock = clock or utcnow

        # Registry of intake hooks that run before release_hold
        # transitions. Downstream specs (pricing, credit-check) can
        # register callables here via register_intake_hook().
        self._intake_hooks: List[Callable] = []

    # ------------------------------------------------------------------
    # Hook registration
    # ------------------------------------------------------------------

    def register_intake_hook(self, hook: Callable) -> None:
        """Register an intake hook that runs before release_hold.

        Hooks are async callables with signature:
            async def hook(order: dict) -> Optional[str]

        They return ``None`` on success or a string reason on failure.
        When any hook fails, the order remains on_hold with the
        returned reason as the updated ``hold_reason``.
        """
        self._intake_hooks.append(hook)

    # ------------------------------------------------------------------
    # Core transition method
    # ------------------------------------------------------------------

    async def apply_status_transition(
        self,
        order: Dict[str, Any],
        new_status: str,
        reason: Optional[str] = None,
        notes: Optional[str] = None,
        actor_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Apply a status transition to an order.

        This is the single method used by both the REST handler
        (``PATCH /api/orders/{id}/status``) and internal callers
        (e.g. POD finalization, agent-driven transitions).

        Steps:
            1. Validate the transition via ``assert_transition``.
            2. Enforce delivery-window guard via
               ``assert_window_present_for_transition``.
            3. Stamp ``updated_at`` and ``last_event_timestamp``.
            4. Update the order status.
            5. Append a status-specific event.
            6. Persist the updated order.
            7. Increment/decrement driver counters when applicable.
            8. Broadcast ``order_status_changed`` via WebSocket.
            9. Mirror to legacy surface when enabled.

        Args:
            order: The order document (mutable dict).
            new_status: The target status.
            reason: Optional reason for the transition (e.g. cancellation
                reason, failure reason).
            notes: Optional notes attached to the event.
            actor_user_id: The user initiating the transition.

        Returns:
            The updated order document.

        Raises:
            AppException: HTTP 409 on invalid transition or missing window.
        """
        old_status = order["status"]

        # 1. Validate the state machine transition
        try:
            assert_transition(old_status, new_status)
        except Exception:
            # Increment rejection metric before re-raising
            orders_state_transition_rejections_total.labels(
                tenant_id=order.get("tenant_id", "unknown"),
                old_status=old_status,
                new_status=new_status,
            ).inc()
            raise

        # 2. Enforce delivery-window guard
        assert_window_present_for_transition(order, new_status)

        # 3. Stamp timestamps
        now = self._clock()
        order["updated_at"] = now
        order["last_event_timestamp"] = now

        # 4. Update status
        order["status"] = new_status

        # Clear hold_reason when leaving on_hold
        if old_status == "on_hold" and new_status != "on_hold":
            order["hold_reason"] = None

        # 5. Build and append the status-specific event
        event_type = _STATUS_TO_EVENT_TYPE.get(new_status, f"order_{new_status}")
        event = {
            "event_id": mint_event_id(),
            "order_id": order["order_id"],
            "tenant_id": order["tenant_id"],
            "event_type": event_type,
            "event_payload": {
                "old_status": old_status,
                "new_status": new_status,
                "reason": reason,
                "notes": notes,
                "actor_user_id": actor_user_id,
            },
            "event_timestamp": now,
            "ingested_at": now,
            "source_schema_version": order.get("source_schema_version", "1.0"),
            "trace_id": order.get("trace_id", ""),
        }

        await self._order_repo.append_event(order["tenant_id"], event)

        # 6. Persist the updated order
        await self._order_repo.upsert_with_last_event_timestamp(
            order["tenant_id"], order
        )

        # 7. Driver counter updates
        await self._update_driver_counters(order, old_status, new_status)

        # 8. Broadcast via WebSocket
        await self._broadcast_status_change(order, old_status, new_status)

        # 9. Legacy dual-write (when enabled)
        await self._mirror_legacy_if_enabled(order)

        return order

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    async def place_on_hold(
        self,
        order: Dict[str, Any],
        hold_reason: str,
        actor_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Transition an order to on_hold with a mandatory reason.

        Args:
            order: The order document (mutable dict).
            hold_reason: The reason for placing the order on hold.
            actor_user_id: The user initiating the hold.

        Returns:
            The updated order document.
        """
        # Set hold_reason before the transition so the model validates
        order["hold_reason"] = hold_reason

        return await self.apply_status_transition(
            order=order,
            new_status="on_hold",
            reason=hold_reason,
            actor_user_id=actor_user_id,
        )

    async def release_hold(
        self,
        order: Dict[str, Any],
        actor_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Release an order from on_hold back to placed.

        Re-runs registered intake hooks (pricing, credit-check) before
        attempting the transition. If any hook fails, the order remains
        on_hold with an updated hold_reason.

        Args:
            order: The order document (mutable dict, status must be on_hold).
            actor_user_id: The user initiating the release.

        Returns:
            The updated order document.
        """
        # Run registered intake hooks
        for hook in self._intake_hooks:
            try:
                failure_reason = await hook(order)
            except Exception as exc:
                logger.warning(
                    "Intake hook %s raised for order=%s: %s",
                    getattr(hook, "__name__", repr(hook)),
                    order.get("order_id"),
                    exc,
                )
                failure_reason = str(exc)

            if failure_reason:
                # Hook failed — update hold_reason and persist, but
                # do NOT transition.
                now = self._clock()
                order["hold_reason"] = failure_reason
                order["updated_at"] = now
                order["last_event_timestamp"] = now

                await self._order_repo.upsert_with_last_event_timestamp(
                    order["tenant_id"], order
                )
                logger.info(
                    "release_hold blocked by hook for order=%s: %s",
                    order.get("order_id"),
                    failure_reason,
                )
                return order

        # All hooks passed — transition back to placed
        return await self.apply_status_transition(
            order=order,
            new_status="placed",
            reason="released_from_hold",
            actor_user_id=actor_user_id,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _update_driver_counters(
        self,
        order: Dict[str, Any],
        old_status: str,
        new_status: str,
    ) -> None:
        """Increment/decrement driver counters when the transition
        affects driver workload metrics."""
        if not self._driver_counter_service:
            return

        driver_id = order.get("assigned_driver_id")
        if not driver_id:
            return

        transition = (old_status, new_status)

        delta_active = 0
        delta_completed = 0

        if transition in _DECREMENT_ACTIVE_TRANSITIONS:
            delta_active = -1

        if transition in _INCREMENT_COMPLETED_TRANSITIONS:
            delta_completed = 1

        if delta_active != 0 or delta_completed != 0:
            try:
                await self._driver_counter_service.increment_counters(
                    driver_id=driver_id,
                    tenant_id=order["tenant_id"],
                    delta_active=delta_active,
                    delta_completed=delta_completed,
                )
            except Exception as exc:
                # Driver counter failures MUST NOT block the main path
                logger.warning(
                    "DriverCounterService.increment_counters failed for "
                    "driver=%s, order=%s: %s",
                    driver_id,
                    order.get("order_id"),
                    exc,
                )

    async def _broadcast_status_change(
        self,
        order: Dict[str, Any],
        old_status: str,
        new_status: str,
    ) -> None:
        """Broadcast the status change via the orders WebSocket manager."""
        try:
            await self._ws_manager.broadcast({
                "type": "order_status_changed",
                "data": {
                    "order_id": order["order_id"],
                    "tenant_id": order["tenant_id"],
                    "old_status": old_status,
                    "new_status": new_status,
                    "updated_at": (
                        order["updated_at"].isoformat()
                        if hasattr(order["updated_at"], "isoformat")
                        else str(order["updated_at"])
                    ),
                },
                "tenant_id": order["tenant_id"],
            })
        except Exception as exc:
            # WebSocket broadcast failures MUST NOT block the main path
            logger.warning(
                "OrdersWSManager.broadcast failed for order=%s: %s",
                order.get("order_id"),
                exc,
            )

    async def _mirror_legacy_if_enabled(
        self, order: Dict[str, Any]
    ) -> None:
        """Mirror the order to the legacy shipment surface when the
        overlay flag is in shadow or active_gated state."""
        if not self._legacy_dual_writer:
            return
        if not self._feature_flag_service:
            return

        try:
            overlay_state = await self._feature_flag_service.get_overlay_state(
                "order_intake_pipeline", order["tenant_id"]
            )
        except Exception as exc:
            logger.warning(
                "Failed to read overlay state for tenant=%s: %s — "
                "skipping legacy mirror",
                order.get("tenant_id"),
                exc,
            )
            return

        if overlay_state in ("shadow", "active_gated"):
            try:
                # mirror_order never raises by contract, but we guard
                # defensively so the main path is never blocked.
                await self._legacy_dual_writer.mirror_order(
                    order, tenant_id=order["tenant_id"]
                )
            except Exception as exc:
                logger.warning(
                    "LegacyDualWriter.mirror_order raised unexpectedly "
                    "for order=%s: %s",
                    order.get("order_id"),
                    exc,
                )
