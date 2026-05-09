"""Subscriber for the order.delivered event — triggers invoice generation.

Subscribes to the intake pipeline's ``order.delivered`` event via the
OrderService's public subscription helper. When the event fires and
``commerce.invoicing_enabled`` is on for the tenant, extracts order
details and calls ``InvoiceService.generate_from_order()``.

The subscription is wired during bootstrap when
``commerce.backbone_enabled`` is on. The ``commerce.invoicing_enabled``
flag is checked per-event (not just at startup) so a flag flip takes
effect without a restart.

Validates: Requirements 5.1, 8.1, 8.2
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from config.settings import get_settings
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


class OrderDeliveredInvoiceSubscriber:
    """Handles order.delivered events to generate invoices.

    This subscriber is registered on the OrderService's public
    subscription helper for the ``order.delivered`` event. On each
    event it:

    1. Checks ``commerce.invoicing_enabled`` for the tenant (per-event).
    2. Extracts order details (tenant_id, order_id, customer_id,
       account_id, line items, tax).
    3. Calls ``InvoiceService.generate_from_order()``.

    The handler is async and failures are logged but never block the
    main order transition path (the OrderService catches exceptions
    from subscribers).
    """

    def __init__(self, invoice_service: Any) -> None:
        """Initialize the subscriber.

        Args:
            invoice_service: An InvoiceService instance for generating
                invoices from delivered orders.
        """
        self._invoice_service = invoice_service

    async def __call__(self, order: Dict[str, Any]) -> None:
        """Handle an order.delivered event.

        Called by the OrderService's event subscriber mechanism after
        an order transitions to ``delivered``.

        Args:
            order: The order document dict (post-transition).
        """
        tenant_id = order.get("tenant_id", "")
        order_id = order.get("order_id", "")

        # Check commerce.invoicing_enabled per-event (Req 8.2)
        settings = get_settings()
        if not getattr(settings, "commerce_invoicing_enabled", False):
            logger.debug(
                "OrderDeliveredInvoiceSubscriber: commerce_invoicing_enabled "
                "is off, skipping invoice generation for order=%s tenant=%s",
                order_id,
                tenant_id,
            )
            return

        # Extract order details for invoice generation
        customer_id = order.get("customer_id", "")
        account_id = order.get("account_id", "")

        # If no account_id on the order, we cannot generate an invoice
        # (the invoice requires an account for billing)
        if not account_id:
            logger.debug(
                "OrderDeliveredInvoiceSubscriber: no account_id on order=%s "
                "tenant=%s, skipping invoice generation",
                order_id,
                tenant_id,
            )
            return

        # Build line items from the order's pricing fields
        line_items = self._extract_line_items(order)
        tax_cents = order.get("tax_cents") or 0

        # If no line items could be extracted (no pricing attached),
        # skip invoice generation
        if not line_items:
            logger.warning(
                "OrderDeliveredInvoiceSubscriber: no line items could be "
                "extracted from order=%s tenant=%s (pricing may not be "
                "attached), skipping invoice generation",
                order_id,
                tenant_id,
            )
            return

        # Call InvoiceService.generate_from_order (idempotent)
        try:
            invoice = await self._invoice_service.generate_from_order(
                tenant_id=tenant_id,
                order_id=order_id,
                customer_id=customer_id,
                account_id=account_id,
                line_items=line_items,
                tax_cents=tax_cents,
                actor="system",
            )
            logger.info(
                "OrderDeliveredInvoiceSubscriber: generated invoice %s "
                "from order=%s tenant=%s (total: %d cents)",
                invoice.get("invoice_id"),
                order_id,
                tenant_id,
                invoice.get("total_cents", 0),
            )
        except Exception as exc:
            logger.error(
                "OrderDeliveredInvoiceSubscriber: failed to generate "
                "invoice from order=%s tenant=%s: %s",
                order_id,
                tenant_id,
                exc,
            )
            raise

    @staticmethod
    def _extract_line_items(order: Dict[str, Any]) -> list:
        """Extract invoice line items from the order's pricing fields.

        Builds a single line item from the order's product_code,
        gallons_requested, unit_price_cents, and subtotal_cents fields.
        Returns an empty list if pricing fields are not present.

        Args:
            order: The order document dict.

        Returns:
            A list of line item dicts suitable for InvoiceService.
        """
        product_code = order.get("product_code")
        unit_price_cents = order.get("unit_price_cents")
        subtotal_cents = order.get("subtotal_cents")
        quantity_gallons = order.get("gallons_requested")

        # All pricing fields must be present to build a line item
        if unit_price_cents is None or subtotal_cents is None:
            return []

        line_item = {
            "product_code": product_code or "unknown",
            "quantity_gallons": quantity_gallons or 0.0,
            "unit_price_cents": int(unit_price_cents),
            "subtotal_cents": int(subtotal_cents),
        }

        return [line_item]
