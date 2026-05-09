"""
Unit tests for the Fuel Order state machine.

Covers every allowed transition, every rejected transition, the
terminal-status invariant, window-presence guards, and the
is_terminal_status helper.

Validates: Requirements 1.2.3
"""

import pytest

from fuel.order_state_machine import (
    VALID_STATUS_TRANSITIONS,
    TERMINAL_STATUSES,
    STATUSES_REQUIRING_WINDOW,
    assert_transition,
    assert_window_present_for_transition,
    is_terminal_status,
)
from errors.exceptions import AppException


# ─── All statuses in the state machine ────────────────────────────────────────

ALL_STATUSES = list(VALID_STATUS_TRANSITIONS.keys())


# ─── Test: Every allowed transition passes without raising ────────────────────


class TestAllowedTransitions:
    """assert_transition should NOT raise for every (old, new) pair in
    VALID_STATUS_TRANSITIONS."""

    @pytest.mark.parametrize(
        "old_status,new_status",
        [
            (old, new)
            for old, targets in VALID_STATUS_TRANSITIONS.items()
            for new in targets
        ],
    )
    def test_allowed_transition_does_not_raise(self, old_status, new_status):
        # Should complete without raising
        assert_transition(old_status, new_status)


# ─── Test: Every disallowed transition raises AppException with 409 ───────────


class TestRejectedTransitions:
    """assert_transition should raise AppException with error_code
    INVALID_STATUS_TRANSITION and status 409 for every disallowed pair."""

    @pytest.mark.parametrize(
        "old_status,new_status",
        [
            (old, new)
            for old in ALL_STATUSES
            for new in ALL_STATUSES
            if new not in VALID_STATUS_TRANSITIONS[old]
        ],
    )
    def test_disallowed_transition_raises_conflict(self, old_status, new_status):
        with pytest.raises(AppException) as exc_info:
            assert_transition(old_status, new_status)

        exc = exc_info.value
        assert exc.error_code.value == "INVALID_STATUS_TRANSITION"
        assert exc.status_code == 409
        assert exc.details["old_status"] == old_status
        assert exc.details["new_status"] == new_status


# ─── Test: Terminal statuses have empty outgoing transition sets ───────────────


class TestTerminalStatusInvariant:
    """Terminal statuses (delivered, failed, cancelled) must have no
    outgoing transitions in the state machine."""

    @pytest.mark.parametrize("status", list(TERMINAL_STATUSES))
    def test_terminal_status_has_no_outgoing_transitions(self, status):
        assert VALID_STATUS_TRANSITIONS[status] == set()

    def test_terminal_statuses_are_exactly_three(self):
        assert TERMINAL_STATUSES == {"delivered", "failed", "cancelled"}

    @pytest.mark.parametrize("status", list(TERMINAL_STATUSES))
    def test_transition_from_terminal_always_raises(self, status):
        """Any transition attempt from a terminal status should raise."""
        for target in ALL_STATUSES:
            with pytest.raises(AppException) as exc_info:
                assert_transition(status, target)
            assert exc_info.value.status_code == 409


# ─── Test: assert_window_present_for_transition ───────────────────────────────


class TestWindowPresentForTransition:
    """assert_window_present_for_transition rejects transitions to
    scheduled/dispatched/in_transit when the order lacks a window."""

    @pytest.mark.parametrize("target_status", list(STATUSES_REQUIRING_WINDOW))
    def test_rejects_when_order_lacks_window(self, target_status):
        order = {
            "order_id": "ord_test123",
            "delivery_window_start": None,
            "delivery_window_end": None,
        }
        with pytest.raises(AppException) as exc_info:
            assert_window_present_for_transition(order, target_status)

        exc = exc_info.value
        assert exc.error_code.value == "MISSING_DELIVERY_WINDOW"
        assert exc.status_code == 409
        assert exc.details["order_id"] == "ord_test123"
        assert exc.details["target_status"] == target_status

    @pytest.mark.parametrize("target_status", list(STATUSES_REQUIRING_WINDOW))
    def test_rejects_when_only_start_present(self, target_status):
        order = {
            "order_id": "ord_partial",
            "delivery_window_start": "2025-01-15T08:00:00Z",
            "delivery_window_end": None,
        }
        with pytest.raises(AppException) as exc_info:
            assert_window_present_for_transition(order, target_status)

        assert exc_info.value.error_code.value == "MISSING_DELIVERY_WINDOW"

    @pytest.mark.parametrize("target_status", list(STATUSES_REQUIRING_WINDOW))
    def test_rejects_when_only_end_present(self, target_status):
        order = {
            "order_id": "ord_partial2",
            "delivery_window_start": None,
            "delivery_window_end": "2025-01-15T17:00:00Z",
        }
        with pytest.raises(AppException) as exc_info:
            assert_window_present_for_transition(order, target_status)

        assert exc_info.value.error_code.value == "MISSING_DELIVERY_WINDOW"

    @pytest.mark.parametrize("target_status", list(STATUSES_REQUIRING_WINDOW))
    def test_accepts_when_order_has_both_window_fields(self, target_status):
        order = {
            "order_id": "ord_full_window",
            "delivery_window_start": "2025-01-15T08:00:00Z",
            "delivery_window_end": "2025-01-15T17:00:00Z",
        }
        # Should not raise
        assert_window_present_for_transition(order, target_status)

    @pytest.mark.parametrize(
        "target_status",
        [s for s in ALL_STATUSES if s not in STATUSES_REQUIRING_WINDOW],
    )
    def test_does_not_check_window_for_non_requiring_statuses(self, target_status):
        """Transitions to statuses NOT in STATUSES_REQUIRING_WINDOW should
        pass regardless of window presence."""
        order = {
            "order_id": "ord_no_window",
            "delivery_window_start": None,
            "delivery_window_end": None,
        }
        # Should not raise
        assert_window_present_for_transition(order, target_status)


# ─── Test: is_terminal_status ─────────────────────────────────────────────────


class TestIsTerminalStatus:
    """is_terminal_status returns True for terminal statuses and False
    for non-terminal ones."""

    @pytest.mark.parametrize("status", list(TERMINAL_STATUSES))
    def test_returns_true_for_terminal(self, status):
        assert is_terminal_status(status) is True

    @pytest.mark.parametrize(
        "status",
        [s for s in ALL_STATUSES if s not in TERMINAL_STATUSES],
    )
    def test_returns_false_for_non_terminal(self, status):
        assert is_terminal_status(status) is False

    def test_returns_false_for_unknown_status(self):
        assert is_terminal_status("unknown_status") is False
