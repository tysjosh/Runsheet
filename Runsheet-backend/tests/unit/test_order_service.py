"""
Unit tests for fuel.services.order_service.OrderService.

Validates: Requirements 1.1.9, 1.2.2, 1.2.3, 2.5.7, 3.2.1, 4.1.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fuel.services.order_service import OrderService


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _FIXED_NOW


def _make_order(
    *,
    status: str = "placed",
    tenant_id: str = "tenant_1",
    order_id: str = "ord_abc123",
    assigned_driver_id: Optional[str] = None,
    delivery_window_start: Optional[str] = "2026-05-11T08:00:00+00:00",
    delivery_window_end: Optional[str] = "2026-05-11T12:00:00+00:00",
    hold_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "status": status,
        "assigned_driver_id": assigned_driver_id,
        "delivery_window_start": delivery_window_start,
        "delivery_window_end": delivery_window_end,
        "hold_reason": hold_reason,
        "source_schema_version": "1.0",
        "trace_id": "trace_001",
        "updated_at": datetime(2026, 5, 9, 10, 0, 0, tzinfo=timezone.utc),
        "last_event_timestamp": datetime(2026, 5, 9, 10, 0, 0, tzinfo=timezone.utc),
    }


def _build_service(
    *,
    driver_counter_service: Optional[Any] = None,
    overlay_state: str = "disabled",
) -> tuple:
    """Build an OrderService with mocked dependencies."""
    order_repo = AsyncMock()
    order_repo.append_event = AsyncMock()
    order_repo.upsert_with_last_event_timestamp = AsyncMock(return_value=True)

    ws_manager = AsyncMock()
    ws_manager.broadcast = AsyncMock(return_value=1)

    feature_flag_service = AsyncMock()
    feature_flag_service.get_overlay_state = AsyncMock(return_value=overlay_state)

    service = OrderService(
        order_repo=order_repo,
        ws_manager=ws_manager,
        driver_counter_service=driver_counter_service,
        feature_flag_service=feature_flag_service,
        clock=_fixed_clock,
    )

    return service, order_repo, ws_manager, feature_flag_service


# ---------------------------------------------------------------------------
# Tests: apply_status_transition
# ---------------------------------------------------------------------------


class TestApplyStatusTransition:
    """Tests for OrderService.apply_status_transition."""

    @pytest.mark.asyncio
    async def test_valid_transition_updates_status(self):
        """A valid transition updates the order status."""
        service, repo, ws, _ = _build_service()
        order = _make_order(status="placed")

        result = await service.apply_status_transition(
            order, "confirmed", reason="customer confirmed", actor_user_id="user_1"
        )

        assert result["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_valid_transition_stamps_timestamps(self):
        """Timestamps are stamped via the clock."""
        service, repo, ws, _ = _build_service()
        order = _make_order(status="placed")

        result = await service.apply_status_transition(order, "confirmed")

        assert result["updated_at"] == _FIXED_NOW
        assert result["last_event_timestamp"] == _FIXED_NOW

    @pytest.mark.asyncio
    async def test_valid_transition_appends_event(self):
        """A status-specific event is appended."""
        service, repo, ws, _ = _build_service()
        order = _make_order(status="placed")

        await service.apply_status_transition(
            order, "confirmed", reason="ok", notes="test", actor_user_id="u1"
        )

        repo.append_event.assert_called_once()
        call_args = repo.append_event.call_args
        event = call_args[0][1]  # second positional arg
        assert event["event_type"] == "order_confirmed"
        assert event["event_payload"]["old_status"] == "placed"
        assert event["event_payload"]["new_status"] == "confirmed"
        assert event["event_payload"]["reason"] == "ok"
        assert event["event_payload"]["notes"] == "test"
        assert event["event_payload"]["actor_user_id"] == "u1"
        assert event["order_id"] == "ord_abc123"
        assert event["tenant_id"] == "tenant_1"

    @pytest.mark.asyncio
    async def test_valid_transition_persists_order(self):
        """The updated order is persisted via upsert."""
        service, repo, ws, _ = _build_service()
        order = _make_order(status="placed")

        await service.apply_status_transition(order, "confirmed")

        repo.upsert_with_last_event_timestamp.assert_called_once_with(
            "tenant_1", order
        )

    @pytest.mark.asyncio
    async def test_valid_transition_broadcasts(self):
        """A WebSocket broadcast is emitted."""
        service, repo, ws, _ = _build_service()
        order = _make_order(status="placed")

        await service.apply_status_transition(order, "confirmed")

        ws.broadcast.assert_called_once()
        msg = ws.broadcast.call_args[0][0]
        assert msg["type"] == "order_status_changed"
        assert msg["data"]["old_status"] == "placed"
        assert msg["data"]["new_status"] == "confirmed"
        assert msg["data"]["order_id"] == "ord_abc123"
        assert msg["tenant_id"] == "tenant_1"

    @pytest.mark.asyncio
    async def test_invalid_transition_raises_409(self):
        """An invalid transition raises a conflict error."""
        service, repo, ws, _ = _build_service()
        order = _make_order(status="placed")

        with pytest.raises(Exception) as exc_info:
            await service.apply_status_transition(order, "in_transit")

        # The error should be from the state machine
        exc = exc_info.value
        assert hasattr(exc, "error_code")
        assert exc.error_code == "INVALID_STATUS_TRANSITION"
        assert exc.status_code == 409

    @pytest.mark.asyncio
    async def test_missing_window_rejects_scheduled_transition(self):
        """Transitioning to scheduled without a window raises 409."""
        service, repo, ws, _ = _build_service()
        order = _make_order(
            status="confirmed",
            delivery_window_start=None,
            delivery_window_end=None,
        )

        with pytest.raises(Exception) as exc_info:
            await service.apply_status_transition(order, "scheduled")

        exc = exc_info.value
        assert hasattr(exc, "error_code")
        assert exc.error_code == "MISSING_DELIVERY_WINDOW"
        assert exc.status_code == 409

    @pytest.mark.asyncio
    async def test_missing_window_rejects_dispatched_transition(self):
        """Transitioning to dispatched without a window raises 409."""
        service, repo, ws, _ = _build_service()
        order = _make_order(
            status="scheduled",
            delivery_window_start=None,
            delivery_window_end=None,
        )

        with pytest.raises(Exception) as exc_info:
            await service.apply_status_transition(order, "dispatched")

        exc = exc_info.value
        assert hasattr(exc, "error_code")
        assert exc.error_code == "MISSING_DELIVERY_WINDOW"
        assert exc.status_code == 409

    @pytest.mark.asyncio
    async def test_window_present_allows_scheduled_transition(self):
        """Transitioning to scheduled with a window succeeds."""
        service, repo, ws, _ = _build_service()
        order = _make_order(status="confirmed")

        result = await service.apply_status_transition(order, "scheduled")

        assert result["status"] == "scheduled"

    @pytest.mark.asyncio
    async def test_clears_hold_reason_on_leaving_on_hold(self):
        """hold_reason is cleared when transitioning out of on_hold."""
        service, repo, ws, _ = _build_service()
        order = _make_order(status="on_hold", hold_reason="credit_check_failed")

        result = await service.apply_status_transition(order, "placed")

        assert result["hold_reason"] is None


# ---------------------------------------------------------------------------
# Tests: Driver counter updates
# ---------------------------------------------------------------------------


class TestDriverCounterUpdates:
    """Tests for driver counter increment/decrement logic."""

    @pytest.mark.asyncio
    async def test_scheduled_to_dispatched_increments_active(self):
        """Active work begins at dispatch, not at driver selection."""
        counter_svc = AsyncMock()
        counter_svc.increment_counters = AsyncMock()
        service, _, _, _ = _build_service(
            driver_counter_service=counter_svc
        )
        order = _make_order(status="scheduled", assigned_driver_id="drv_1")

        await service.apply_status_transition(order, "dispatched")

        counter_svc.increment_counters.assert_called_once_with(
            driver_id="drv_1",
            tenant_id="tenant_1",
            delta_active=1,
            delta_completed=0,
        )

    @pytest.mark.asyncio
    async def test_dispatched_to_delivered_decrements_active_increments_completed(self):
        """dispatched → delivered decrements active and increments completed."""
        counter_svc = AsyncMock()
        counter_svc.increment_counters = AsyncMock()
        service, repo, ws, _ = _build_service(driver_counter_service=counter_svc)
        order = _make_order(status="dispatched", assigned_driver_id="drv_1")

        await service.apply_status_transition(order, "in_transit")
        # in_transit doesn't trigger counters
        counter_svc.increment_counters.assert_not_called()

        # Reset and test dispatched → delivered (need fresh order)
        counter_svc.reset_mock()
        order2 = _make_order(status="in_transit", assigned_driver_id="drv_1")
        await service.apply_status_transition(order2, "delivered")

        counter_svc.increment_counters.assert_called_once_with(
            driver_id="drv_1",
            tenant_id="tenant_1",
            delta_active=-1,
            delta_completed=1,
        )

    @pytest.mark.asyncio
    async def test_dispatched_to_failed_decrements_active_only(self):
        """dispatched → failed decrements active but does not increment completed."""
        counter_svc = AsyncMock()
        counter_svc.increment_counters = AsyncMock()
        service, repo, ws, _ = _build_service(driver_counter_service=counter_svc)
        order = _make_order(status="dispatched", assigned_driver_id="drv_1")

        await service.apply_status_transition(order, "failed")

        counter_svc.increment_counters.assert_called_once_with(
            driver_id="drv_1",
            tenant_id="tenant_1",
            delta_active=-1,
            delta_completed=0,
        )

    @pytest.mark.asyncio
    async def test_dispatched_to_cancelled_decrements_active_only(self):
        """dispatched → cancelled decrements active."""
        counter_svc = AsyncMock()
        counter_svc.increment_counters = AsyncMock()
        service, repo, ws, _ = _build_service(driver_counter_service=counter_svc)
        order = _make_order(status="dispatched", assigned_driver_id="drv_1")

        await service.apply_status_transition(order, "cancelled")

        counter_svc.increment_counters.assert_called_once_with(
            driver_id="drv_1",
            tenant_id="tenant_1",
            delta_active=-1,
            delta_completed=0,
        )

    @pytest.mark.asyncio
    async def test_no_counter_update_without_driver(self):
        """No counter update when order has no assigned driver."""
        counter_svc = AsyncMock()
        counter_svc.increment_counters = AsyncMock()
        service, repo, ws, _ = _build_service(driver_counter_service=counter_svc)
        order = _make_order(status="dispatched", assigned_driver_id=None)

        await service.apply_status_transition(order, "failed")

        counter_svc.increment_counters.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_counter_update_without_counter_service(self):
        """No error when driver_counter_service is None."""
        service, repo, ws, _ = _build_service(driver_counter_service=None)
        order = _make_order(status="dispatched", assigned_driver_id="drv_1")

        # Should not raise
        result = await service.apply_status_transition(order, "failed")
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_counter_failure_does_not_block_main_path(self):
        """Counter service failure does not block the transition."""
        counter_svc = AsyncMock()
        counter_svc.increment_counters = AsyncMock(
            side_effect=RuntimeError("ES down")
        )
        service, repo, ws, _ = _build_service(driver_counter_service=counter_svc)
        order = _make_order(status="dispatched", assigned_driver_id="drv_1")

        result = await service.apply_status_transition(order, "failed")

        assert result["status"] == "failed"
        # Broadcast still happened
        ws.broadcast.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Legacy dual-write (removed)
# ---------------------------------------------------------------------------
# ``TestLegacyDualWrite`` asserted that a transition mirrored into the
# legacy shipment surface in ``shadow`` / ``active_gated``, stayed silent in
# ``active_auto`` / ``disabled``, and never blocked the transition when the
# mirror failed. ``_mirror_legacy_if_enabled`` and the LegacyDualWriter it
# called are both gone, so the class went with them.


# ---------------------------------------------------------------------------
# Tests: place_on_hold
# ---------------------------------------------------------------------------


class TestPlaceOnHold:
    """Tests for OrderService.place_on_hold."""

    @pytest.mark.asyncio
    async def test_transitions_to_on_hold_with_reason(self):
        """place_on_hold sets status to on_hold with the given reason."""
        service, repo, ws, _ = _build_service()
        order = _make_order(status="placed")

        result = await service.place_on_hold(order, "credit_check_failed", "user_1")

        assert result["status"] == "on_hold"
        assert result["hold_reason"] == "credit_check_failed"

    @pytest.mark.asyncio
    async def test_emits_on_hold_event(self):
        """place_on_hold emits an order_on_hold event."""
        service, repo, ws, _ = _build_service()
        order = _make_order(status="placed")

        await service.place_on_hold(order, "credit_check_failed")

        repo.append_event.assert_called_once()
        event = repo.append_event.call_args[0][1]
        assert event["event_type"] == "order_on_hold"


class TestReconcileDeliveryResult:
    @pytest.mark.asyncio
    async def test_persists_audits_and_replays_delivered_subscribers(self):
        service, repo, ws, _ = _build_service()
        subscriber = AsyncMock()
        service.subscribe("order.delivered", subscriber)
        order = _make_order(status="delivered")
        delivery_result = {
            "pod_id": "pod-1",
            "actual_gallons": 487.5,
        }

        result = await service.reconcile_delivery_result(
            order=order,
            delivery_result=delivery_result,
            actor_user_id="driver-1",
            client_event_timestamp="2026-05-10T11:59:00Z",
        )

        assert result["delivery_result"] == delivery_result
        event = repo.append_event.call_args.args[1]
        assert event["event_type"] == "order_delivery_result_reconciled"
        assert event["event_payload"]["actual_gallons"] == 487.5
        repo.upsert_with_last_event_timestamp.assert_awaited_once()
        subscriber.assert_awaited_once_with(result)

    @pytest.mark.asyncio
    async def test_rejects_non_delivered_order(self):
        service, repo, ws, _ = _build_service()

        with pytest.raises(ValueError, match="already delivered"):
            await service.reconcile_delivery_result(
                order=_make_order(status="in_transit"),
                delivery_result={"pod_id": "pod-1", "actual_gallons": 10},
            )

        repo.append_event.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: release_hold
# ---------------------------------------------------------------------------


class TestReleaseHold:
    """Tests for OrderService.release_hold."""

    @pytest.mark.asyncio
    async def test_transitions_back_to_placed_when_no_hooks(self):
        """release_hold transitions to placed when no hooks are registered."""
        service, repo, ws, _ = _build_service()
        order = _make_order(status="on_hold", hold_reason="credit_check_failed")

        result = await service.release_hold(order, "user_1")

        assert result["status"] == "placed"
        assert result["hold_reason"] is None

    @pytest.mark.asyncio
    async def test_remains_on_hold_when_hook_fails(self):
        """release_hold keeps on_hold when a hook returns a failure reason."""
        service, repo, ws, _ = _build_service()

        async def failing_hook(order):
            return "credit_still_bad"

        service.register_intake_hook(failing_hook)
        order = _make_order(status="on_hold", hold_reason="credit_check_failed")

        result = await service.release_hold(order, "user_1")

        assert result["status"] == "on_hold"
        assert result["hold_reason"] == "credit_still_bad"

    @pytest.mark.asyncio
    async def test_transitions_when_all_hooks_pass(self):
        """release_hold transitions when all hooks return None."""
        service, repo, ws, _ = _build_service()

        async def passing_hook(order):
            return None

        service.register_intake_hook(passing_hook)
        order = _make_order(status="on_hold", hold_reason="credit_check_failed")

        result = await service.release_hold(order, "user_1")

        assert result["status"] == "placed"

    @pytest.mark.asyncio
    async def test_hook_exception_keeps_on_hold(self):
        """release_hold keeps on_hold when a hook raises an exception."""
        service, repo, ws, _ = _build_service()

        async def exploding_hook(order):
            raise RuntimeError("service unavailable")

        service.register_intake_hook(exploding_hook)
        order = _make_order(status="on_hold", hold_reason="original_reason")

        result = await service.release_hold(order, "user_1")

        assert result["status"] == "on_hold"
        assert result["hold_reason"] == "service unavailable"

    @pytest.mark.asyncio
    async def test_multiple_hooks_first_failure_stops(self):
        """release_hold stops at the first failing hook."""
        service, repo, ws, _ = _build_service()
        call_order = []

        async def hook_a(order):
            call_order.append("a")
            return "hook_a_failed"

        async def hook_b(order):
            call_order.append("b")
            return None

        service.register_intake_hook(hook_a)
        service.register_intake_hook(hook_b)
        order = _make_order(status="on_hold", hold_reason="original")

        result = await service.release_hold(order, "user_1")

        assert result["status"] == "on_hold"
        assert result["hold_reason"] == "hook_a_failed"
        assert call_order == ["a"]  # hook_b was never called


# ---------------------------------------------------------------------------
# Tests: Broadcast failure resilience
# ---------------------------------------------------------------------------


class TestBroadcastResilience:
    """Tests that WebSocket broadcast failures don't block transitions."""

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_block(self):
        """WS broadcast failure does not prevent the transition."""
        service, repo, ws, _ = _build_service()
        ws.broadcast = AsyncMock(side_effect=RuntimeError("WS down"))
        order = _make_order(status="placed")

        result = await service.apply_status_transition(order, "confirmed")

        assert result["status"] == "confirmed"
        repo.upsert_with_last_event_timestamp.assert_called_once()
