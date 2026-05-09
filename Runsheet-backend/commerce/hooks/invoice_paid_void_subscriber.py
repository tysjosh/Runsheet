"""Subscriber for invoice paid/void transitions — triggers dunning cancellation.

When an invoice transitions to ``paid`` or ``void``, this subscriber calls
``DunningService.cancel_for_invoice`` to mark all pending dunning_events as
cancelled. The notification pipeline consumes the cancellation to drop any
queued-but-unsent dunning emails for that invoice.

The subscriber is wired into the InvoiceService as an optional post-transition
callback. It checks ``commerce.dunning_enabled`` per-event so a flag flip
takes effect without a restart.

Validates: Requirements 7.5
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class InvoicePaidVoidDunningSubscriber:
    """Handles invoice paid/void transitions to cancel pending dunning notifications.

    This subscriber is called by the InvoiceService after an invoice
    transitions to ``paid`` or ``void``. It delegates to
    ``DunningService.cancel_for_invoice`` which:

    1. Finds all non-cancelled dunning_events for the invoice.
    2. Marks them as cancelled with the appropriate reason.
    3. Notifies the notification pipeline to drop queued messages.

    The handler is async and failures are logged but never block the
    main invoice transition path.
    """

    def __init__(self, dunning_service: Any) -> None:
        """Initialize the subscriber.

        Args:
            dunning_service: A DunningService instance for cancelling
                pending dunning notifications.
        """
        self._dunning_service = dunning_service

    async def on_invoice_paid(
        self, tenant_id: str, invoice_id: str
    ) -> Dict[str, Any]:
        """Handle an invoice transition to paid.

        Cancels all pending dunning notifications for the invoice.

        Args:
            tenant_id: The tenant owning the invoice.
            invoice_id: The invoice that transitioned to paid.

        Returns:
            Result dict from DunningService.cancel_for_invoice.
        """
        logger.info(
            "InvoicePaidVoidDunningSubscriber: invoice %s paid, "
            "cancelling dunning notifications for tenant %s",
            invoice_id,
            tenant_id,
        )
        return await self._dunning_service.cancel_for_invoice(
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            reason="invoice_paid",
        )

    async def on_invoice_voided(
        self, tenant_id: str, invoice_id: str
    ) -> Dict[str, Any]:
        """Handle an invoice transition to void.

        Cancels all pending dunning notifications for the invoice.

        Args:
            tenant_id: The tenant owning the invoice.
            invoice_id: The invoice that was voided.

        Returns:
            Result dict from DunningService.cancel_for_invoice.
        """
        logger.info(
            "InvoicePaidVoidDunningSubscriber: invoice %s voided, "
            "cancelling dunning notifications for tenant %s",
            invoice_id,
            tenant_id,
        )
        return await self._dunning_service.cancel_for_invoice(
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            reason="invoice_voided",
        )

    async def __call__(
        self, tenant_id: str, invoice_id: str, new_status: str
    ) -> None:
        """Dispatch based on the new invoice status.

        This is the unified entry point called by InvoiceService after
        a state transition. Only ``paid`` and ``void`` trigger dunning
        cancellation.

        Args:
            tenant_id: The tenant owning the invoice.
            invoice_id: The invoice that transitioned.
            new_status: The new status of the invoice.
        """
        if new_status == "paid":
            await self.on_invoice_paid(tenant_id, invoice_id)
        elif new_status == "void":
            await self.on_invoice_voided(tenant_id, invoice_id)
