"""Tests for DeliveryCompletedSubscriber — order.delivered event handler.

Validates:
- Requirement 12.7: WHEN a delivery is confirmed via POD, THE
  Notification_Template_Service SHALL send the delivery_completed
  notification to the customer's delivery contact.

Tests cover:
- Notification fires on order.delivered with correct event_type and data
- Deduplication: same order_id does not fire twice
- Missing customer_id skips notification
- Missing notification_service skips gracefully
- Notification failure does not propagate (non-blocking)
- Event data extraction maps order fields to template placeholders
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from compliance.hooks.delivery_completed_subscriber import (
    DeliveryCompletedSubscriber,
    _product_code_to_name,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(
    *,
    order_id: str = "ord-001",
    tenant_id: str = "tenant-abc",
    customer_id: str = "cust-123",
    customer_name: str = "Acme Fuel Co",
    product_code: str = "DIESEL_2",
    gallons_requested: float = 500.0,
    net_gallons: float = 495.0,
    unit_price_cents: int = 350,
    subtotal_cents: int = 175000,
    po_number: str = "PO-9876",
    assigned_driver_id: str = "drv-010",
    driver_name: str = "John Smith",
    updated_at: str = "2024-03-15T14:30:00Z",
    **kwargs,
) -> dict:
    """Build a minimal order dict for testing."""
    order = {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "product_code": product_code,
        "gallons_requested": gallons_requested,
        "net_gallons": net_gallons,
        "unit_price_cents": unit_price_cents,
        "subtotal_cents": subtotal_cents,
        "po_number": po_number,
        "assigned_driver_id": assigned_driver_id,
        "driver_name": driver_name,
        "updated_at": updated_at,
    }
    order.update(kwargs)
    return order


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeliveryCompletedSubscriber:
    """Tests for the DeliveryCompletedSubscriber handler."""

    @pytest.mark.asyncio
    async def test_fires_notification_on_delivered(self):
        """Notification fires with event_type=delivery_completed on order.delivered."""
        notification_service = MagicMock()
        notification_service.notify_event = AsyncMock(return_value=[])

        subscriber = DeliveryCompletedSubscriber(
            notification_service=notification_service
        )
        order = _make_order()

        await subscriber(order)

        notification_service.notify_event.assert_called_once()
        call_kwargs = notification_service.notify_event.call_args[1]
        assert call_kwargs["event_type"] == "delivery_completed"
        assert call_kwargs["tenant_id"] == "tenant-abc"

    @pytest.mark.asyncio
    async def test_event_data_contains_required_placeholders(self):
        """Event data includes all template placeholders for delivery_completed."""
        notification_service = MagicMock()
        notification_service.notify_event = AsyncMock(return_value=[])

        subscriber = DeliveryCompletedSubscriber(
            notification_service=notification_service
        )
        order = _make_order()

        await subscriber(order)

        call_kwargs = notification_service.notify_event.call_args[1]
        event_data = call_kwargs["event_data"]

        # All required placeholders from the template
        assert event_data["customer_id"] == "cust-123"
        assert event_data["customer_name"] == "Acme Fuel Co"
        assert "2024-03-15" in event_data["delivery_date"]
        assert event_data["product_name"] == "Diesel #2"
        assert event_data["gross_gallons"] == "500.0"
        assert event_data["net_gallons"] == "495.0"
        assert event_data["unit_price"] == "3.50"
        assert event_data["total_amount"] == "1750.00"
        assert event_data["PO_number"] == "PO-9876"
        assert event_data["driver_name"] == "John Smith"

    @pytest.mark.asyncio
    async def test_deduplication_same_order_id(self):
        """Same order_id does not fire notification twice."""
        notification_service = MagicMock()
        notification_service.notify_event = AsyncMock(return_value=[])

        subscriber = DeliveryCompletedSubscriber(
            notification_service=notification_service
        )
        order = _make_order(order_id="ord-dup-001")

        # First call fires
        await subscriber(order)
        assert notification_service.notify_event.call_count == 1

        # Second call for same order is deduplicated
        await subscriber(order)
        assert notification_service.notify_event.call_count == 1

    @pytest.mark.asyncio
    async def test_different_order_ids_both_fire(self):
        """Different order_ids each fire their own notification."""
        notification_service = MagicMock()
        notification_service.notify_event = AsyncMock(return_value=[])

        subscriber = DeliveryCompletedSubscriber(
            notification_service=notification_service
        )

        await subscriber(_make_order(order_id="ord-001"))
        await subscriber(_make_order(order_id="ord-002"))

        assert notification_service.notify_event.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_when_no_customer_id(self):
        """No notification when customer_id is missing."""
        notification_service = MagicMock()
        notification_service.notify_event = AsyncMock(return_value=[])

        subscriber = DeliveryCompletedSubscriber(
            notification_service=notification_service
        )
        order = _make_order(customer_id="")

        await subscriber(order)

        notification_service.notify_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_notification_service(self):
        """No error when notification_service is None."""
        subscriber = DeliveryCompletedSubscriber(notification_service=None)
        order = _make_order()

        # Should not raise
        await subscriber(order)

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_propagate(self):
        """Notification failure is logged but does not raise."""
        notification_service = MagicMock()
        notification_service.notify_event = AsyncMock(
            side_effect=RuntimeError("SendGrid down")
        )

        subscriber = DeliveryCompletedSubscriber(
            notification_service=notification_service
        )
        order = _make_order()

        # Should not raise — fault-tolerant
        await subscriber(order)

        # Notification was attempted
        notification_service.notify_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_mark_as_notified(self):
        """Failed notification does not add to dedup set — allows retry."""
        notification_service = MagicMock()
        notification_service.notify_event = AsyncMock(
            side_effect=RuntimeError("SendGrid down")
        )

        subscriber = DeliveryCompletedSubscriber(
            notification_service=notification_service
        )
        order = _make_order(order_id="ord-retry-001")

        # First call fails
        await subscriber(order)
        assert notification_service.notify_event.call_count == 1

        # Second call should retry (not deduplicated since first failed)
        await subscriber(order)
        assert notification_service.notify_event.call_count == 2

    @pytest.mark.asyncio
    async def test_fallback_values_when_fields_missing(self):
        """Graceful fallback when optional order fields are missing."""
        notification_service = MagicMock()
        notification_service.notify_event = AsyncMock(return_value=[])

        subscriber = DeliveryCompletedSubscriber(
            notification_service=notification_service
        )
        # Minimal order with only required fields
        order = {
            "order_id": "ord-minimal",
            "tenant_id": "tenant-xyz",
            "customer_id": "cust-min",
        }

        await subscriber(order)

        call_kwargs = notification_service.notify_event.call_args[1]
        event_data = call_kwargs["event_data"]

        assert event_data["customer_name"] == "Valued Customer"
        assert event_data["unit_price"] == "N/A"
        assert event_data["total_amount"] == "N/A"
        assert event_data["PO_number"] == "N/A"
        assert event_data["product_name"] == "Fuel"

    @pytest.mark.asyncio
    async def test_driver_name_falls_back_to_assigned_driver_id(self):
        """When driver_name is missing, falls back to assigned_driver_id."""
        notification_service = MagicMock()
        notification_service.notify_event = AsyncMock(return_value=[])

        subscriber = DeliveryCompletedSubscriber(
            notification_service=notification_service
        )
        order = _make_order(driver_name=None, assigned_driver_id="drv-099")
        # Remove driver_name key entirely
        del order["driver_name"]

        await subscriber(order)

        call_kwargs = notification_service.notify_event.call_args[1]
        event_data = call_kwargs["event_data"]
        assert event_data["driver_name"] == "drv-099"


class TestProductCodeToName:
    """Tests for the _product_code_to_name helper."""

    def test_known_product_codes(self):
        assert _product_code_to_name("DIESEL_2") == "Diesel #2"
        assert _product_code_to_name("PROPANE") == "Propane"
        assert _product_code_to_name("HEATING_OIL") == "Heating Oil"
        assert _product_code_to_name("OFF_ROAD_DIESEL") == "Off-Road Diesel (Dyed)"

    def test_unknown_product_code_returns_raw(self):
        assert _product_code_to_name("CUSTOM_FUEL") == "CUSTOM_FUEL"

    def test_empty_product_code_returns_fuel(self):
        assert _product_code_to_name("") == "Fuel"
