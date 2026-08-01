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

# An order becomes active work when it is dispatched, not when a dispatcher
# merely selects a driver.
_INCREMENT_ACTIVE_TRANSITIONS: set[tuple[str, str]] = {
    ("scheduled", "dispatched"),
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

        # Registry of status-event subscribers. Keyed by event name
        # (e.g. "order.delivered"). Each subscriber is an async callable
        # with signature: async def handler(order: dict) -> None.
        # Subscribers are called after the transition is persisted and
        # broadcast. Failures are logged but never block the main path.
        self._event_subscribers: Dict[str, List[Callable]] = {}

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
    # Event subscription (public subscription helper)
    # ------------------------------------------------------------------

    def subscribe(self, event_name: str, handler: Callable) -> None:
        """Subscribe a handler to a named order status event.

        This is the public subscription helper that downstream modules
        (e.g. commerce invoice generation) use to react to order
        lifecycle events without coupling to the OrderService internals.

        Supported event names follow the pattern ``order.<status>``:
            - ``order.delivered``
            - ``order.cancelled``
            - ``order.failed``
            - etc.

        Handlers are async callables with signature:
            async def handler(order: dict) -> None

        Handlers are called after the transition is persisted and
        broadcast. Failures are logged but MUST NOT block the main path.

        Args:
            event_name: The event to subscribe to (e.g. "order.delivered").
            handler: An async callable that receives the order dict.
        """
        if event_name not in self._event_subscribers:
            self._event_subscribers[event_name] = []
        self._event_subscribers[event_name].append(handler)
        logger.info(
            "OrderService: registered subscriber for %s: %s",
            event_name,
            getattr(handler, "__name__", repr(handler)),
        )

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
        client_event_timestamp: Optional[str] = None,
        event_payload_extra: Optional[Dict[str, Any]] = None,
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
            client_event_timestamp: The caller's own stamp for when the action
                happened, which may predate the server's receipt (an offline
                driver queue drains late). Recorded in ``event_payload``
                alongside the server-stamped ``event_timestamp`` /
                ``ingested_at``, never in place of them (R4.8). ``event_payload``
                is the free-form part of the event document, so this needs no
                ``fuel_order_events`` mapping change.
            event_payload_extra: Additional free-form keys to merge into
                ``event_payload``. The driver path uses it to record the
                Hours-of-Service gate verdict — the acting driver, the reading
                ``recorded_at``, the freshness state, the gate outcome, and the
                override identifier when one applied (R17.25, R17.26). The six
                canonical keys above always win, so no caller can overwrite the
                transition's own record; and like ``client_event_timestamp`` this
                needs no ``fuel_order_events`` mapping change.

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
        event_payload: Dict[str, Any] = dict(event_payload_extra or {})
        event_payload.update(
            {
                "old_status": old_status,
                "new_status": new_status,
                "reason": reason,
                "notes": notes,
                "actor_user_id": actor_user_id,
                "client_event_timestamp": client_event_timestamp,
            }
        )
        event = {
            "event_id": mint_event_id(),
            "order_id": order["order_id"],
            "tenant_id": order["tenant_id"],
            "event_type": event_type,
            "event_payload": event_payload,
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

        # 10. Notify event subscribers (e.g. commerce invoice generation)
        await self._notify_event_subscribers(order, new_status)

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

    async def reconcile_delivery_result(
        self,
        *,
        order: Dict[str, Any],
        delivery_result: Dict[str, Any],
        actor_user_id: Optional[str] = None,
        client_event_timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Attach POD facts to an order that is already delivered.

        A dispatcher may have moved the order to ``delivered`` just before the
        driver's offline POD arrived. Re-applying ``delivered -> delivered`` is
        invalid, but dropping the actual gallons would make invoicing use the
        requested amount. This repair path writes an explicit audit event,
        persists the delivery snapshot, and replays the idempotent delivered
        subscribers so invoice creation can catch up.
        """
        if order.get("status") != "delivered":
            raise ValueError(
                "reconcile_delivery_result requires an already delivered order"
            )
        if not delivery_result:
            raise ValueError("delivery_result is required")
        if order.get("delivery_result") == delivery_result:
            return order

        now = self._clock()
        order["delivery_result"] = delivery_result
        order["updated_at"] = now
        order["last_event_timestamp"] = now
        event = {
            "event_id": mint_event_id(),
            "order_id": order["order_id"],
            "tenant_id": order["tenant_id"],
            "event_type": "order_delivery_result_reconciled",
            "event_payload": {
                "pod_id": delivery_result.get("pod_id"),
                "actual_gallons": delivery_result.get("actual_gallons"),
                "actor_user_id": actor_user_id,
                "client_event_timestamp": client_event_timestamp,
            },
            "event_timestamp": now,
            "ingested_at": now,
            "source_schema_version": order.get("source_schema_version", "1.0"),
            "trace_id": order.get("trace_id", ""),
        }
        await self._order_repo.append_event(order["tenant_id"], event)
        await self._order_repo.upsert_with_last_event_timestamp(
            order["tenant_id"], order
        )
        await self._notify_event_subscribers(order, "delivered")
        return order

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

        if transition in _INCREMENT_ACTIVE_TRANSITIONS:
            delta_active = 1

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

    async def _notify_event_subscribers(
        self,
        order: Dict[str, Any],
        new_status: str,
    ) -> None:
        """Notify registered subscribers for the given status event.

        Constructs the event name as ``order.<new_status>`` and calls
        all registered handlers. Failures are logged but MUST NOT block
        the main path.
        """
        event_name = f"order.{new_status}"
        handlers = self._event_subscribers.get(event_name, [])
        if not handlers:
            return

        for handler in handlers:
            try:
                await handler(order)
            except Exception as exc:
                logger.warning(
                    "OrderService: event subscriber %s failed for "
                    "event=%s, order=%s: %s",
                    getattr(handler, "__name__", repr(handler)),
                    event_name,
                    order.get("order_id"),
                    exc,
                )
