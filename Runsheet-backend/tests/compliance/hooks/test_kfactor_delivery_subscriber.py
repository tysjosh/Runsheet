"""Tests for KFactorDeliverySubscriber — order.delivered event handler.

Validates:
- The subscriber calls KFactorCalibrationService.compute_variance for
  auto-fill and keep-full deliveries.
- The subscriber skips will-call deliveries.
- The subscriber is fault-tolerant: errors are logged but do not propagate.
- The subscriber sends operator notifications when variance is flagged.
- The subscriber does not send notifications when variance is within threshold.

Validates: Requirement 9.1
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from compliance.hooks.kfactor_delivery_subscriber import (
    AUTOFILL_CALL_TYPES,
    KFactorDeliverySubscriber,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_order(
    *,
    order_id: str = "ord_123",
    tenant_id: str = "tenant_abc",
    call_type: str = "auto_fill",
    customer_id: str = "cust_456",
    product_code: str = "PROPANE",
    gallons_requested: float = 250.0,
    status: str = "delivered",
) -> dict:
    """Build a minimal delivered order dict for testing."""
    return {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "call_type": call_type,
        "customer_id": customer_id,
        "product_code": product_code,
        "gallons_requested": gallons_requested,
        "status": status,
    }


def _make_variance(
    *,
    delivery_id: str = "ord_123",
    tank_id: str = "tank_789",
    predicted_gallons: float = 200.0,
    actual_gallons: float = 250.0,
    variance_percent: float = 25.0,
    suggested_kfactor: float = 0.85,
    flagged: bool = True,
):
    """Build a mock KFactorVariance result."""
    variance = MagicMock()
    variance.delivery_id = delivery_id
    variance.tank_id = tank_id
    variance.predicted_gallons = predicted_gallons
    variance.actual_gallons = actual_gallons
    variance.variance_percent = variance_percent
    variance.suggested_kfactor = suggested_kfactor
    variance.flagged = flagged
    return variance


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestKFactorDeliverySubscriber:
    """Tests for the KFactorDeliverySubscriber handler."""

    @pytest.mark.asyncio
    async def test_computes_variance_for_auto_fill_delivery(self):
        """When call_type is auto_fill, compute_variance is called."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock(
            return_value=_make_variance(flagged=False, variance_percent=5.0)
        )

        subscriber = KFactorDeliverySubscriber(kfactor_service=kfactor_service)
        order = _make_order(call_type="auto_fill")

        await subscriber(order)

        kfactor_service.compute_variance.assert_called_once_with(
            delivery_id="ord_123",
            tenant_id="tenant_abc",
        )

    @pytest.mark.asyncio
    async def test_computes_variance_for_keep_full_delivery(self):
        """When call_type is keep_full, compute_variance is called."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock(
            return_value=_make_variance(flagged=False, variance_percent=3.0)
        )

        subscriber = KFactorDeliverySubscriber(kfactor_service=kfactor_service)
        order = _make_order(call_type="keep_full")

        await subscriber(order)

        kfactor_service.compute_variance.assert_called_once_with(
            delivery_id="ord_123",
            tenant_id="tenant_abc",
        )

    @pytest.mark.asyncio
    async def test_skips_will_call_delivery(self):
        """When call_type is will_call, compute_variance is NOT called."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock()

        subscriber = KFactorDeliverySubscriber(kfactor_service=kfactor_service)
        order = _make_order(call_type="will_call")

        await subscriber(order)

        kfactor_service.compute_variance.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_empty_call_type(self):
        """When call_type is empty/missing, compute_variance is NOT called."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock()

        subscriber = KFactorDeliverySubscriber(kfactor_service=kfactor_service)
        order = _make_order(call_type="")

        await subscriber(order)

        kfactor_service.compute_variance.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_unknown_call_type(self):
        """When call_type is an unknown value, compute_variance is NOT called."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock()

        subscriber = KFactorDeliverySubscriber(kfactor_service=kfactor_service)
        order = _make_order(call_type="one_time")

        await subscriber(order)

        kfactor_service.compute_variance.assert_not_called()

    @pytest.mark.asyncio
    async def test_fault_tolerant_on_compute_variance_error(self):
        """When compute_variance raises, the error is logged but not propagated."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock(
            side_effect=ValueError("No previous delivery found")
        )

        subscriber = KFactorDeliverySubscriber(kfactor_service=kfactor_service)
        order = _make_order(call_type="auto_fill")

        # Should NOT raise — fault-tolerant
        await subscriber(order)

        kfactor_service.compute_variance.assert_called_once()

    @pytest.mark.asyncio
    async def test_fault_tolerant_on_runtime_error(self):
        """When compute_variance raises RuntimeError, the error is logged but not propagated."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock(
            side_effect=RuntimeError("Weather provider not configured")
        )

        subscriber = KFactorDeliverySubscriber(kfactor_service=kfactor_service)
        order = _make_order(call_type="keep_full")

        # Should NOT raise — fault-tolerant
        await subscriber(order)

        kfactor_service.compute_variance.assert_called_once()

    @pytest.mark.asyncio
    async def test_sends_notification_when_flagged(self):
        """When variance is flagged, notification is sent to operator."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock(
            return_value=_make_variance(
                flagged=True,
                variance_percent=25.0,
                suggested_kfactor=0.85,
            )
        )

        notification_service = MagicMock()
        notification_service.send = AsyncMock()

        subscriber = KFactorDeliverySubscriber(
            kfactor_service=kfactor_service,
            notification_service=notification_service,
        )
        order = _make_order(call_type="auto_fill")

        await subscriber(order)

        notification_service.send.assert_called_once()
        call_kwargs = notification_service.send.call_args[1]
        assert call_kwargs["template_key"] == "kfactor_variance_alert"
        assert call_kwargs["tenant_id"] == "tenant_abc"
        assert call_kwargs["context"]["tank_id"] == "tank_789"
        assert call_kwargs["context"]["variance_percent"] == 25.0

    @pytest.mark.asyncio
    async def test_no_notification_when_not_flagged(self):
        """When variance is within threshold, no notification is sent."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock(
            return_value=_make_variance(flagged=False, variance_percent=5.0)
        )

        notification_service = MagicMock()
        notification_service.send = AsyncMock()

        subscriber = KFactorDeliverySubscriber(
            kfactor_service=kfactor_service,
            notification_service=notification_service,
        )
        order = _make_order(call_type="auto_fill")

        await subscriber(order)

        notification_service.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_notification_when_service_not_configured(self):
        """When notification_service is None, no notification attempt is made."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock(
            return_value=_make_variance(flagged=True)
        )

        subscriber = KFactorDeliverySubscriber(
            kfactor_service=kfactor_service,
            notification_service=None,
        )
        order = _make_order(call_type="auto_fill")

        # Should not raise
        await subscriber(order)

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_propagate(self):
        """When notification_service.send raises, the error is logged but not propagated."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock(
            return_value=_make_variance(flagged=True)
        )

        notification_service = MagicMock()
        notification_service.send = AsyncMock(
            side_effect=RuntimeError("Notification service unavailable")
        )

        subscriber = KFactorDeliverySubscriber(
            kfactor_service=kfactor_service,
            notification_service=notification_service,
        )
        order = _make_order(call_type="auto_fill")

        # Should NOT raise — notification failure is caught
        await subscriber(order)

    @pytest.mark.asyncio
    async def test_autofill_call_types_constant(self):
        """AUTOFILL_CALL_TYPES contains the expected values."""
        assert "auto_fill" in AUTOFILL_CALL_TYPES
        assert "keep_full" in AUTOFILL_CALL_TYPES
        assert "will_call" not in AUTOFILL_CALL_TYPES

    @pytest.mark.asyncio
    async def test_passes_correct_delivery_id_and_tenant(self):
        """compute_variance receives the correct order_id and tenant_id."""
        kfactor_service = MagicMock()
        kfactor_service.compute_variance = AsyncMock(
            return_value=_make_variance(flagged=False)
        )

        subscriber = KFactorDeliverySubscriber(kfactor_service=kfactor_service)
        order = _make_order(
            order_id="ord_custom_999",
            tenant_id="tenant_xyz",
            call_type="keep_full",
        )

        await subscriber(order)

        kfactor_service.compute_variance.assert_called_once_with(
            delivery_id="ord_custom_999",
            tenant_id="tenant_xyz",
        )
