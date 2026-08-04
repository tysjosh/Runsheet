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
from services.money import (
    legacy_unit_price_cents,
    line_subtotal_cents,
    unit_price_micros_from_record,
)
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

        # Invoicing must be based on measured delivery, not the planned order
        # quantity. A dispatcher may mark an order delivered before an offline
        # driver's POD syncs; OrderService.reconcile_delivery_result replays
        # this subscriber after the snapshot arrives.
        delivery_result = order.get("delivery_result")
        if not delivery_result:
            logger.info(
                "OrderDeliveredInvoiceSubscriber: no POD delivery_result on "
                "order=%s tenant=%s; invoice deferred until POD reconciliation",
                order_id,
                tenant_id,
            )
            return

        # Build line items from the order's pricing fields
        line_items = self._extract_line_items(order)
        tax_cents = self._extract_tax_cents(order)

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
            invoice_args = {
                "tenant_id": tenant_id,
                "order_id": order_id,
                "customer_id": customer_id,
                "account_id": account_id,
                "line_items": line_items,
                "tax_cents": tax_cents,
                "actor": "system",
            }
            invoice_args["delivery_result"] = delivery_result

            invoice = await self._invoice_service.generate_from_order(
                **invoice_args,
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

        A canonical ``delivery_result.actual_gallons`` is authoritative. The
        fallback remains useful for pure extraction callers, but the event
        handler defers invoice creation until a delivery snapshot exists.

        Args:
            order: The order document dict.

        Returns:
            A list of line item dicts suitable for InvoiceService.
        """
        product_code = order.get("product_code")
        unit_price_micros = unit_price_micros_from_record(order)
        delivery_result = order.get("delivery_result") or {}
        quantity_gallons = delivery_result.get("actual_gallons")
        if quantity_gallons is None:
            quantity_gallons = order.get("gallons_requested")

        # A precise unit price and delivered quantity are sufficient. The
        # original order subtotal is intentionally not reused because it is
        # based on planned rather than measured gallons.
        if (
            unit_price_micros is None
            or quantity_gallons is None
            or float(quantity_gallons) <= 0
        ):
            return []

        subtotal_cents = line_subtotal_cents(
            quantity_gallons,
            unit_price_micros,
        )
        line_item = {
            "product_code": product_code or "unknown",
            "quantity_gallons": float(quantity_gallons),
            # Retain whole cents for old readers, but never use it for
            # quantity multiplication when the micro price is present.
            "unit_price_cents": legacy_unit_price_cents(unit_price_micros),
            "unit_price_micros": unit_price_micros,
            "subtotal_cents": int(subtotal_cents),
        }

        return [line_item]

    @staticmethod
    def _extract_tax_cents(order: Dict[str, Any]) -> int:
        """Scale legacy order tax to actual gallons when no TaxEngine reruns it."""
        tax_cents = int(order.get("tax_cents") or 0)
        delivery_result = order.get("delivery_result") or {}
        actual = delivery_result.get("actual_gallons")
        requested = order.get("gallons_requested")
        if actual is None or not requested or tax_cents == 0:
            return tax_cents
        return round(tax_cents * float(actual) / float(requested))
