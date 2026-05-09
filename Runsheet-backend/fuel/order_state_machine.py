"""
Order state machine for the Fuel Order lifecycle.

Defines the valid status transitions, terminal statuses, and guard
functions that enforce the state machine invariants. Used by
``OrderService.apply_status_transition`` and the REST surface to
reject illegal transitions with structured 409 errors.

Validates: Requirements 1.2.3, 1.1.9
"""

from typing import Literal

OrderStatus = Literal[
    "placed", "confirmed", "scheduled", "dispatched",
    "in_transit", "delivered", "failed", "cancelled", "on_hold",
]

# ─── State Machine ────────────────────────────────────────────────────────────

VALID_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "placed":     {"confirmed", "cancelled", "on_hold"},
    "confirmed":  {"scheduled", "cancelled", "on_hold"},
    "scheduled":  {"dispatched", "cancelled", "on_hold"},
    "dispatched": {"in_transit", "failed", "cancelled"},
    "in_transit": {"delivered", "failed"},
    "on_hold":    {"placed", "cancelled"},
    "delivered":  set(),   # terminal
    "failed":     set(),   # terminal
    "cancelled":  set(),   # terminal
}

#: Terminal statuses have no outgoing transitions — once an order
#: reaches one of these it cannot change status again.
TERMINAL_STATUSES: set[str] = {"delivered", "failed", "cancelled"}

#: Statuses that require a non-null delivery_window_start +
#: delivery_window_end on the order document. Enforced inside
#: OrderService.apply_status_transition so will_call / keep_full /
#: auto_fill orders that entered intake without a window cannot
#: leave ``placed`` or ``confirmed`` without one. The forecaster
#: (keep_full, auto_fill) attaches the window; the dispatcher
#: (will_call) attaches it on pickup.
STATUSES_REQUIRING_WINDOW: set[str] = {"scheduled", "dispatched", "in_transit"}


# ─── Guard Functions ──────────────────────────────────────────────────────────


def assert_transition(old: str, new: str) -> None:
    """Validate that *old* → *new* is a legal status transition.

    Raises:
        AppException: HTTP 409 with error_code ``invalid_status_transition``
            when the transition is not in :data:`VALID_STATUS_TRANSITIONS`.
    """
    allowed = VALID_STATUS_TRANSITIONS.get(old, set())
    if new not in allowed:
        from errors.exceptions import conflict

        raise conflict(
            message=f"cannot transition order status {old} → {new}",
            error_code="INVALID_STATUS_TRANSITION",
            details={"old_status": old, "new_status": new},
        )


def assert_window_present_for_transition(order: dict, new_status: str) -> None:
    """Reject forward transitions into a scheduled-or-later state
    unless the order carries a valid delivery window.

    Args:
        order: The order document (dict with at least
            ``delivery_window_start``, ``delivery_window_end``, and
            ``order_id`` keys).
        new_status: The target status being transitioned to.

    Raises:
        AppException: HTTP 409 with error_code ``missing_delivery_window``
            when the target status requires a window and the order lacks one.
    """
    if new_status not in STATUSES_REQUIRING_WINDOW:
        return
    if order.get("delivery_window_start") and order.get("delivery_window_end"):
        return
    from errors.exceptions import conflict

    raise conflict(
        message=(
            f"order {order.get('order_id')} lacks a delivery window "
            f"and cannot transition to {new_status}"
        ),
        error_code="MISSING_DELIVERY_WINDOW",
        details={"order_id": order.get("order_id"), "target_status": new_status},
    )


def is_terminal_status(status: str) -> bool:
    """Return ``True`` if *status* is a terminal (no outgoing transitions)."""
    return status in TERMINAL_STATUSES
