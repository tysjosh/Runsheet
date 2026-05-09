"""Tests for OrderDeliveredInvoiceSubscriber and OrderService subscription helper.

Validates:
- The subscriber calls InvoiceService.generate_from_order when
  commerce.invoicing_enabled is on and the order has pricing fields.
- The subscriber skips invoice generation when commerce.invoicing_enabled
  is off.
- The subscriber skips when no account_id is present on the order.
- The subscriber skips when no pricing fields are present.
- The OrderService.subscribe method correctly registers and fires
  subscribers on status transitions.
- Subscriber failures do not block the main transition path.

Validates: Requirements 5.1, 8.1, 8.2
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from commerce.hooks.order_delivered_subscriber import OrderDeliveredInvoiceSubscriber
from fuel.services.order_service import OrderService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_order(
    *,
    status: str = "delivered",
    tenant_id: str = "tenant_abc",
    order_id: str = "ord_123",
    customer_id: str = "cust_456",
    account_id: str = "acct_789",
    product_code: str = "ULSD",
    gallons_requested: float = 500.0,
    unit_price_cents: int = 350,
    subtotal_cents: int = 175000,
    tax_cents: int = 10938,
    total_cents: int = 185938,
) -> dict:
    """Build a minimal delivered order dict for testing."""
    return {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "account_id": account_id,
        "product_code": product_code,
        "gallons_requested": gallons_requested,
        "unit_price_cents": unit_price_cents,
        "subtotal_cents": subtotal_cents,
        "tax_cents": tax_cents,
        "total_cents": total_cents,
        "status": status,
    }


def _make_settings(invoicing_enabled: bool = True):
    """Build a mock settings object."""
    settings = MagicMock()
    settings.commerce_invoicing_enabled = invoicing_enabled
    return settings


# ---------------------------------------------------------------------------
# OrderDeliveredInvoiceSubscriber tests
# ---------------------------------------------------------------------------


class TestOrderDeliveredInvoiceSubscriber:
    """Tests for the OrderDeliveredInvoiceSubscriber handler."""

    @pytest.mark.asyncio
    async def test_generates_invoice_when_invoicing_enabled(self):
        """When invoicing is enabled and order has pricing, generate invoice."""
        invoice_service = MagicMock()
        invoice_service.generate_from_order = AsyncMock(
            return_value={"invoice_id": "inv_abc", "total_cents": 185938}
        )

        subscriber = OrderDeliveredInvoiceSubscriber(invoice_service=invoice_service)
        order = _make_order()

        with patch(
            "commerce.hooks.order_delivered_subscriber.get_settings",
            return_value=_make_settings(invoicing_enabled=True),
        ):
            await subscriber(order)

        invoice_service.generate_from_order.assert_called_once_with(
            tenant_id="tenant_abc",
            order_id="ord_123",
            customer_id="cust_456",
            account_id="acct_789",
            line_items=[
                {
                    "product_code": "ULSD",
                    "quantity_gallons": 500.0,
                    "unit_price_cents": 350,
                    "subtotal_cents": 175000,
                }
            ],
            tax_cents=10938,
            actor="system",
        )

    @pytest.mark.asyncio
    async def test_skips_when_invoicing_disabled(self):
        """When commerce.invoicing_enabled is off, skip invoice generation."""
        invoice_service = MagicMock()
        invoice_service.generate_from_order = AsyncMock()

        subscriber = OrderDeliveredInvoiceSubscriber(invoice_service=invoice_service)
        order = _make_order()

        with patch(
            "commerce.hooks.order_delivered_subscriber.get_settings",
            return_value=_make_settings(invoicing_enabled=False),
        ):
            await subscriber(order)

        invoice_service.generate_from_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_account_id(self):
        """When order has no account_id, skip invoice generation."""
        invoice_service = MagicMock()
        invoice_service.generate_from_order = AsyncMock()

        subscriber = OrderDeliveredInvoiceSubscriber(invoice_service=invoice_service)
        order = _make_order(account_id="")

        with patch(
            "commerce.hooks.order_delivered_subscriber.get_settings",
            return_value=_make_settings(invoicing_enabled=True),
        ):
            await subscriber(order)

        invoice_service.generate_from_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_pricing_fields(self):
        """When order has no pricing fields, skip invoice generation."""
        invoice_service = MagicMock()
        invoice_service.generate_from_order = AsyncMock()

        subscriber = OrderDeliveredInvoiceSubscriber(invoice_service=invoice_service)
        order = _make_order()
        # Remove pricing fields
        order["unit_price_cents"] = None
        order["subtotal_cents"] = None

        with patch(
            "commerce.hooks.order_delivered_subscriber.get_settings",
            return_value=_make_settings(invoicing_enabled=True),
        ):
            await subscriber(order)

        invoice_service.generate_from_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_on_invoice_service_failure(self):
        """When InvoiceService.generate_from_order raises, the exception propagates."""
        invoice_service = MagicMock()
        invoice_service.generate_from_order = AsyncMock(
            side_effect=RuntimeError("ES unavailable")
        )

        subscriber = OrderDeliveredInvoiceSubscriber(invoice_service=invoice_service)
        order = _make_order()

        with patch(
            "commerce.hooks.order_delivered_subscriber.get_settings",
            return_value=_make_settings(invoicing_enabled=True),
        ):
            with pytest.raises(RuntimeError, match="ES unavailable"):
                await subscriber(order)

    @pytest.mark.asyncio
    async def test_extracts_line_items_correctly(self):
        """Line items are extracted from order pricing fields."""
        subscriber = OrderDeliveredInvoiceSubscriber(invoice_service=MagicMock())
        order = _make_order(
            product_code="JET-A",
            gallons_requested=1000.0,
            unit_price_cents=425,
            subtotal_cents=425000,
        )

        line_items = subscriber._extract_line_items(order)

        assert len(line_items) == 1
        assert line_items[0] == {
            "product_code": "JET-A",
            "quantity_gallons": 1000.0,
            "unit_price_cents": 425,
            "subtotal_cents": 425000,
        }

    @pytest.mark.asyncio
    async def test_checks_flag_per_event_not_at_startup(self):
        """The invoicing_enabled flag is checked on every event call."""
        invoice_service = MagicMock()
        invoice_service.generate_from_order = AsyncMock(
            return_value={"invoice_id": "inv_1", "total_cents": 100}
        )

        subscriber = OrderDeliveredInvoiceSubscriber(invoice_service=invoice_service)
        order = _make_order()

        # First call: enabled
        with patch(
            "commerce.hooks.order_delivered_subscriber.get_settings",
            return_value=_make_settings(invoicing_enabled=True),
        ):
            await subscriber(order)

        assert invoice_service.generate_from_order.call_count == 1

        # Second call: disabled (flag flipped)
        with patch(
            "commerce.hooks.order_delivered_subscriber.get_settings",
            return_value=_make_settings(invoicing_enabled=False),
        ):
            await subscriber(order)

        # Still only 1 call — second was skipped
        assert invoice_service.generate_from_order.call_count == 1


# ---------------------------------------------------------------------------
# OrderService.subscribe integration tests
# ---------------------------------------------------------------------------


class TestOrderServiceSubscribe:
    """Tests for the OrderService public subscription helper."""

    def _make_service(self, overlay_state="active_auto"):
        """Build an OrderService with mocked dependencies."""
        order_repo = MagicMock()
        order_repo.append_event = AsyncMock()
        order_repo.upsert_with_last_event_timestamp = AsyncMock()

        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()

        feature_flag_service = MagicMock()
        feature_flag_service.get_overlay_state = AsyncMock(return_value=overlay_state)

        service = OrderService(
            order_repo=order_repo,
            ws_manager=ws_manager,
            feature_flag_service=feature_flag_service,
        )
        return service

    @pytest.mark.asyncio
    async def test_subscribe_registers_handler(self):
        """subscribe() registers a handler for the given event name."""
        service = self._make_service()
        handler = AsyncMock()

        service.subscribe("order.delivered", handler)

        assert "order.delivered" in service._event_subscribers
        assert handler in service._event_subscribers["order.delivered"]

    @pytest.mark.asyncio
    async def test_subscriber_called_on_delivered_transition(self):
        """Subscriber is called when order transitions to delivered."""
        service = self._make_service()
        handler = AsyncMock()
        service.subscribe("order.delivered", handler)

        order = {
            "order_id": "ord_test",
            "tenant_id": "tenant_1",
            "status": "in_transit",
            "delivery_window_start": "2026-01-01T08:00:00",
            "delivery_window_end": "2026-01-01T12:00:00",
            "source_schema_version": "1.0",
            "trace_id": "trace_1",
        }

        await service.apply_status_transition(order, "delivered")

        handler.assert_called_once_with(order)
        assert order["status"] == "delivered"

    @pytest.mark.asyncio
    async def test_subscriber_not_called_for_other_events(self):
        """Subscriber for order.delivered is NOT called on other transitions."""
        service = self._make_service()
        handler = AsyncMock()
        service.subscribe("order.delivered", handler)

        order = {
            "order_id": "ord_test",
            "tenant_id": "tenant_1",
            "status": "placed",
            "source_schema_version": "1.0",
            "trace_id": "trace_1",
        }

        await service.apply_status_transition(order, "confirmed")

        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_subscriber_failure_does_not_block_transition(self):
        """If a subscriber raises, the transition still completes."""
        service = self._make_service()
        failing_handler = AsyncMock(side_effect=RuntimeError("subscriber boom"))
        service.subscribe("order.delivered", failing_handler)

        order = {
            "order_id": "ord_test",
            "tenant_id": "tenant_1",
            "status": "in_transit",
            "delivery_window_start": "2026-01-01T08:00:00",
            "delivery_window_end": "2026-01-01T12:00:00",
            "source_schema_version": "1.0",
            "trace_id": "trace_1",
        }

        # Should not raise — subscriber failure is caught
        result = await service.apply_status_transition(order, "delivered")

        assert result["status"] == "delivered"
        failing_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_called(self):
        """Multiple subscribers for the same event are all called."""
        service = self._make_service()
        handler_a = AsyncMock()
        handler_b = AsyncMock()
        service.subscribe("order.delivered", handler_a)
        service.subscribe("order.delivered", handler_b)

        order = {
            "order_id": "ord_test",
            "tenant_id": "tenant_1",
            "status": "in_transit",
            "delivery_window_start": "2026-01-01T08:00:00",
            "delivery_window_end": "2026-01-01T12:00:00",
            "source_schema_version": "1.0",
            "trace_id": "trace_1",
        }

        await service.apply_status_transition(order, "delivered")

        handler_a.assert_called_once_with(order)
        handler_b.assert_called_once_with(order)
