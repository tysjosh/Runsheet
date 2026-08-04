"""
Commerce External Sync — adapter bridging canonical commerce entities to
the existing QBO + Stripe connectors (Design §7).

This adapter sits between the commerce layer (InvoiceService, PaymentService)
and the integration connectors (QuickBooksOnlineConnector, StripeConnector).
It translates commerce-domain events into connector-compatible operations:

    * ``on_invoice_finalized(invoice)`` — enqueues a QBO push sync_run via
      the standard Integration_Scheduler path. Runs as a post-commit
      callback inside InvoiceService.finalize_draft so HTTP latency is
      unaffected by QBO push latency.

    * ``on_qbo_payment_observed(qbo_event)`` — extracts payment details
      from a QBO Payment event and hands off to
      PaymentService.ingest(source="qbo", ...).

    * ``on_stripe_charge_observed(stripe_event)`` — extracts payment
      details from a Stripe charge/payment_intent event and hands off to
      PaymentService.ingest(source="stripe", ...).

Cross-cutting invariants:

    * **Error isolation.** Each method logs errors but never raises —
      a failed QBO push or payment ingestion must not crash the calling
      service or block the HTTP request path.

    * **Feature-flag layering.** Commerce Backbone's own
      ``commerce.invoicing_enabled`` flag controls whether the canonical
      Invoice is generated. The connectors' overlay flags
      (``overlay.qbo_invoice_push``, ``overlay.stripe_autocharge``)
      control whether the external write actually fires. This adapter
      does NOT re-check the overlay flags — that's the connector's
      responsibility.

    * **Idempotency.** Payment ingestion is idempotent via the
      PaymentService's IdempotencyService key
      ``idemp:{tenant_id}:payment:{source}:{external_id}``.

Validates: Design §7, Requirements 5.6, 6.1, 6.2.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Dict, Optional

from services.money import unit_price_micros_from_record, unit_price_usd
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


class CommerceExternalSync:
    """Adapter bridging commerce events to QBO + Stripe connectors.

    Args:
        qbo_connector: The tenant's QuickBooksOnlineConnector instance
            (or None if QBO is not configured for this tenant).
        stripe_connector: The tenant's StripeConnector instance
            (or None if Stripe is not configured for this tenant).
        invoice_service: The commerce InvoiceService for reading invoice
            details when needed.
        payment_service: The commerce PaymentService for ingesting
            payments from external sources.
    """

    def __init__(
        self,
        qbo_connector: Optional[Any],
        stripe_connector: Optional[Any],
        invoice_service: Any,
        payment_service: Any,
        integration_repository: Optional[Any] = None,
        connector_factory: Optional[Any] = None,
    ) -> None:
        self._qbo_connector = qbo_connector
        self._stripe_connector = stripe_connector
        self._invoice_service = invoice_service
        self._payment_service = payment_service
        self._integration_repository = integration_repository
        self._connector_factory = connector_factory

    def set_integration_resolver(
        self, *, integration_repository: Any, connector_factory: Any
    ) -> None:
        """Late-wire tenant-specific connectors after integrations bootstrap."""
        self._integration_repository = integration_repository
        self._connector_factory = connector_factory

    async def on_invoice_finalized(self, invoice: Dict[str, Any]) -> None:
        """Enqueue a QBO push for a finalized invoice.

        Called as a post-commit callback inside
        InvoiceService.finalize_draft — NOT in-line with the HTTP
        request so HTTP latency is unaffected by QBO push latency.

        Builds a payload matching the existing QBO connector's
        sync_push contract and dispatches it. The connector's own
        overlay flag (``overlay.qbo_invoice_push``) gates whether the
        actual QBO API call fires.

        Args:
            invoice: The finalized Invoice document dict from
                invoices_current.
        """
        try:
            connector = await self._resolve_qbo_connector(
                invoice.get("tenant_id")
            )
            if connector is None:
                logger.debug(
                    "CommerceExternalSync.on_invoice_finalized: no QBO "
                    "connector configured, leaving invoice pending=%s",
                    invoice.get("invoice_id"),
                )
                return

            # Build the push payload matching the QBO connector's
            # expected shape. The connector translates this into a QBO
            # Invoice create call.
            payload = self._build_qbo_push_payload(invoice)

            # Dispatch via the connector's sync_push method. The
            # Integration_Scheduler path is the standard way to enqueue
            # this, but when called as a callback we invoke sync_push
            # directly — the connector handles feature-flag gating,
            # rate limiting, and retry internally.
            sync_run = await connector.sync_push(payload)

            # Update the invoice's qbo_push_state based on the result
            invoice_id = invoice.get("invoice_id")
            tenant_id = invoice.get("tenant_id")

            if sync_run and hasattr(sync_run, "status"):
                raw_counts = getattr(sync_run, "record_counts", {}) or {}
                counts = raw_counts if isinstance(raw_counts, Mapping) else {}
                skipped = int(counts.get("skipped_disabled", 0) or 0) > 0
                pushed = int(counts.get("invoices_pushed", 0) or 0) > 0
                if skipped and sync_run.status in ("success", "partial"):
                    # A tenant-controlled feature flag is not a transport
                    # failure. Leave the durable state pending so enabling the
                    # integration later lets the recovery worker export it.
                    logger.debug(
                        "CommerceExternalSync: QBO push disabled; leaving "
                        "invoice=%s pending",
                        invoice_id,
                    )
                    return
                if sync_run.status in ("success", "partial") and pushed:
                    raw_metadata = getattr(sync_run, "result_metadata", {}) or {}
                    result_metadata = (
                        raw_metadata if isinstance(raw_metadata, Mapping) else {}
                    )
                    external_invoice_id = result_metadata.get(
                        "external_invoice_id"
                    )
                    await self._mark_qbo_push_success(
                        invoice=invoice,
                        external_invoice_id=external_invoice_id,
                    )
                    logger.info(
                        "CommerceExternalSync: QBO push succeeded for "
                        "invoice=%s tenant=%s run_id=%s",
                        invoice_id,
                        tenant_id,
                        getattr(sync_run, "run_id", None),
                    )
                else:
                    # Track push attempts for dead-letter logic (Req 5.6b)
                    current_attempts = invoice.get("qbo_push_attempts", 0) or 0
                    new_attempts = current_attempts + 1
                    error_details = getattr(sync_run, "error_details", None)
                    if sync_run.status in ("success", "partial") and not pushed:
                        error_details = "QBO connector returned success without creating an invoice"

                    if new_attempts >= 3:
                        # Dead-letter after 3 consecutive failures
                        await self._mark_qbo_push_dead_letter(
                            tenant_id=tenant_id,
                            invoice_id=invoice_id,
                            attempts=new_attempts,
                            last_error=error_details,
                        )
                    else:
                        await self._mark_qbo_push_retry(
                            tenant_id=tenant_id,
                            invoice_id=invoice_id,
                            attempts=new_attempts,
                            last_error=error_details,
                        )

        except Exception as exc:
            # Never crash the caller — log and continue
            logger.error(
                "CommerceExternalSync.on_invoice_finalized: failed to push "
                "invoice=%s to QBO: %s",
                invoice.get("invoice_id"),
                exc,
                exc_info=True,
            )
            current_attempts = invoice.get("qbo_push_attempts", 0) or 0
            await self._mark_qbo_push_retry(
                tenant_id=invoice.get("tenant_id"),
                invoice_id=invoice.get("invoice_id"),
                attempts=current_attempts + 1,
                last_error=str(exc),
            )

    async def retry_invoice_push(self, invoice: Dict[str, Any]) -> None:
        """Retry a finalized invoice through the same tenant connector path."""
        await self.on_invoice_finalized(invoice)

    async def on_qbo_payment_observed(self, qbo_event: Dict[str, Any]) -> None:
        """Ingest a payment observed from a QBO sync_pull.

        Extracts payment details from the QBO Payment event and hands
        off to PaymentService.ingest(source="qbo", ...).

        Expected qbo_event shape (from QBO sync_pull output):
            {
                "Id": "123",                    # QBO Payment ID
                "TotalAmt": 1500.00,            # Payment amount in USD
                "TxnDate": "2025-01-15",        # Transaction date
                "PaymentMethodRef": {"value": "1", "name": "Check"},
                "LinkedTxn": [
                    {"TxnId": "456", "TxnType": "Invoice"}
                ],
                "MetaData": {...},
                # Commerce-specific fields added by the connector:
                "tenant_id": "...",
                "matched_invoice_id": "inv_...",  # Resolved canonical invoice_id
                "matched_account_id": "acct_...", # Resolved account_id
            }

        Args:
            qbo_event: The QBO Payment event dict.
        """
        try:
            tenant_id = qbo_event.get("tenant_id")
            if not tenant_id:
                logger.warning(
                    "CommerceExternalSync.on_qbo_payment_observed: "
                    "missing tenant_id in event, skipping"
                )
                return

            # Extract payment details from the QBO event
            external_id = str(qbo_event.get("Id", ""))
            if not external_id:
                logger.warning(
                    "CommerceExternalSync.on_qbo_payment_observed: "
                    "missing QBO Payment Id, skipping"
                )
                return

            invoice_id = qbo_event.get("matched_invoice_id")
            account_id = qbo_event.get("matched_account_id")

            if not invoice_id:
                # Try to resolve from LinkedTxn
                invoice_id = self._resolve_invoice_from_linked_txn(qbo_event)

            if not invoice_id:
                logger.warning(
                    "CommerceExternalSync.on_qbo_payment_observed: "
                    "could not resolve invoice_id for QBO Payment=%s "
                    "tenant=%s, skipping",
                    external_id,
                    tenant_id,
                )
                return

            # Convert QBO amount (USD float) to cents (int)
            total_amt = qbo_event.get("TotalAmt", 0)
            amount_cents = int(round(float(total_amt) * 100))

            if amount_cents <= 0:
                logger.warning(
                    "CommerceExternalSync.on_qbo_payment_observed: "
                    "non-positive amount for QBO Payment=%s tenant=%s, "
                    "skipping",
                    external_id,
                    tenant_id,
                )
                return

            # Derive payment method from QBO PaymentMethodRef
            method = self._derive_qbo_payment_method(qbo_event)

            # Parse received_at from TxnDate
            received_at = self._parse_qbo_txn_date(qbo_event.get("TxnDate"))

            await self._payment_service.ingest(
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                account_id=account_id or "",
                amount_cents=amount_cents,
                source="qbo",
                method=method,
                external_id=f"qbo:{external_id}",
                reference=f"QBO Payment {external_id}",
                received_at=received_at,
                actor="qbo",
            )

            logger.info(
                "CommerceExternalSync: ingested QBO payment=%s "
                "amount=%d cents invoice=%s tenant=%s",
                external_id,
                amount_cents,
                invoice_id,
                tenant_id,
            )

        except Exception as exc:
            # Never crash the caller — log and continue
            logger.error(
                "CommerceExternalSync.on_qbo_payment_observed: failed to "
                "ingest QBO payment event: %s",
                exc,
                exc_info=True,
            )

    async def on_stripe_charge_observed(self, stripe_event: Dict[str, Any]) -> None:
        """Ingest a payment observed from a Stripe webhook/sync_pull.

        Extracts payment details from the Stripe charge/payment_intent
        event and hands off to PaymentService.ingest(source="stripe", ...).

        Expected stripe_event shape (from Stripe webhook or sync_pull):
            {
                "id": "pi_...",                 # PaymentIntent ID
                "amount": 150000,               # Amount in cents
                "currency": "usd",
                "status": "succeeded",
                "payment_method_types": ["card"],
                "metadata": {
                    "invoice_id": "inv_...",     # Commerce invoice_id
                    "account_id": "acct_...",    # Commerce account_id
                    "tenant_id": "...",
                },
                # Or top-level fields added by the connector:
                "tenant_id": "...",
                "matched_invoice_id": "inv_...",
                "matched_account_id": "acct_...",
            }

        Args:
            stripe_event: The Stripe event dict.
        """
        try:
            # Resolve tenant_id from metadata or top-level
            metadata = stripe_event.get("metadata") or {}
            tenant_id = (
                stripe_event.get("tenant_id")
                or metadata.get("tenant_id")
            )
            if not tenant_id:
                logger.warning(
                    "CommerceExternalSync.on_stripe_charge_observed: "
                    "missing tenant_id in event, skipping"
                )
                return

            # Extract the Stripe charge/payment_intent ID
            external_id = str(stripe_event.get("id", ""))
            if not external_id:
                logger.warning(
                    "CommerceExternalSync.on_stripe_charge_observed: "
                    "missing Stripe event id, skipping"
                )
                return

            # Only process succeeded charges
            status = stripe_event.get("status", "")
            if status != "succeeded":
                logger.debug(
                    "CommerceExternalSync.on_stripe_charge_observed: "
                    "skipping non-succeeded event id=%s status=%s",
                    external_id,
                    status,
                )
                return

            # Resolve invoice_id from metadata or top-level
            invoice_id = (
                stripe_event.get("matched_invoice_id")
                or metadata.get("invoice_id")
            )
            account_id = (
                stripe_event.get("matched_account_id")
                or metadata.get("account_id")
            )

            if not invoice_id:
                logger.warning(
                    "CommerceExternalSync.on_stripe_charge_observed: "
                    "could not resolve invoice_id for Stripe event=%s "
                    "tenant=%s, skipping",
                    external_id,
                    tenant_id,
                )
                return

            # Stripe amounts are already in cents
            amount_cents = int(stripe_event.get("amount", 0))
            if amount_cents <= 0:
                logger.warning(
                    "CommerceExternalSync.on_stripe_charge_observed: "
                    "non-positive amount for Stripe event=%s tenant=%s, "
                    "skipping",
                    external_id,
                    tenant_id,
                )
                return

            # Derive payment method from Stripe payment_method_types
            method = self._derive_stripe_payment_method(stripe_event)

            await self._payment_service.ingest(
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                account_id=account_id or "",
                amount_cents=amount_cents,
                source="stripe",
                method=method,
                external_id=external_id,
                reference=f"Stripe {external_id}",
                received_at=utcnow(),
                actor="stripe",
            )

            logger.info(
                "CommerceExternalSync: ingested Stripe charge=%s "
                "amount=%d cents invoice=%s tenant=%s",
                external_id,
                amount_cents,
                invoice_id,
                tenant_id,
            )

        except Exception as exc:
            # Never crash the caller — log and continue
            logger.error(
                "CommerceExternalSync.on_stripe_charge_observed: failed to "
                "ingest Stripe charge event: %s",
                exc,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _resolve_qbo_connector(self, tenant_id: Optional[str]):
        if self._qbo_connector is not None:
            return self._qbo_connector
        if (
            not tenant_id
            or self._integration_repository is None
            or self._connector_factory is None
        ):
            return None

        instances = await self._integration_repository.list_for_tenant(
            tenant_id,
            provider_name="quickbooks_online",
            enabled=True,
            size=10,
        )
        if not instances:
            return None
        instance = next(
            (
                candidate
                for candidate in instances
                if getattr(candidate, "status", None) == "connected"
            ),
            instances[0],
        )
        connector = self._connector_factory(instance)
        if hasattr(connector, "__await__"):
            connector = await connector
        return connector

    def _build_qbo_push_payload(self, invoice: Dict[str, Any]) -> Dict[str, Any]:
        """Build the payload for the QBO connector's sync_push method.

        Maps the canonical Invoice document to the shape the QBO
        connector expects for creating a QBO Invoice.
        """
        line_items = invoice.get("line_items") or []
        first_line_price_micros = (
            unit_price_micros_from_record(line_items[0])
            if line_items
            else None
        )

        # Build the payload matching the QBO connector's expected shape
        payload: Dict[str, Any] = {
            "invoice_id": invoice.get("invoice_id"),
            "customer_id": invoice.get("customer_id"),
            "customer_name": invoice.get("customer_name", ""),
            "delivery_date": (
                str(invoice.get("delivered_at") or invoice.get("issued_at") or "")[:10]
            ),
            "product_code": (
                line_items[0].get("product_code", "")
                if line_items
                else ""
            ),
            "delivered_gallons": (
                line_items[0].get("quantity_gallons", 0)
                if line_items
                else 0
            ),
            "unit_price_usd": (
                float(unit_price_usd(first_line_price_micros))
                if first_line_price_micros is not None
                else 0.0
            ),
            "total_cents": invoice.get("total_cents", 0),
            "subtotal_cents": invoice.get("subtotal_cents", 0),
            "tax_cents": invoice.get("tax_cents", 0),
            "line_items": line_items,
            "memo": f"Invoice {invoice.get('invoice_number', '')}",
            "reconciliation_id": invoice.get("order_id"),
            "invoice_doc_number": invoice.get("invoice_number"),
            "tenant_id": invoice.get("tenant_id"),
            "account_id": invoice.get("account_id"),
            "external_refs": invoice.get("external_refs", {}),
        }
        return payload

    async def _mark_qbo_push_success(
        self,
        *,
        invoice: Dict[str, Any],
        external_invoice_id: Optional[str],
    ) -> None:
        """Persist the provider acknowledgement on the canonical invoice."""
        from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX

        tenant_id = invoice.get("tenant_id")
        invoice_id = invoice.get("invoice_id")
        external_refs = dict(invoice.get("external_refs") or {})
        if external_invoice_id:
            external_refs["qbo"] = f"inv:{external_invoice_id}"
        update_doc = {
            "qbo_push_state": "pushed",
            "qbo_push_attempts": int(invoice.get("qbo_push_attempts") or 0) + 1,
            "qbo_push_last_error": None,
            "external_refs": external_refs,
            "updated_at": utcnow().isoformat(),
        }
        await self._invoice_service._es.update_document(
            INVOICES_CURRENT_INDEX,
            invoice_id,
            update_doc,
        )
        try:
            from commerce.services.commerce_persistence_bridge import (
                mirror_invoice_fields,
            )

            await mirror_invoice_fields(
                tenant_id,
                invoice_id,
                {
                    key: value
                    for key, value in update_doc.items()
                    if key != "updated_at"
                },
                event_type="qbo_pushed",
            )
        except Exception:
            logger.exception(
                "CommerceExternalSync: failed to mirror QBO acknowledgement "
                "for invoice=%s",
                invoice_id,
            )

    async def _mark_qbo_push_dead_letter(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
        attempts: int,
        last_error: Optional[str],
    ) -> None:
        """Mark an invoice's QBO push as dead-lettered (Req 5.6b)."""
        try:
            from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX

            update_doc = {
                "qbo_push_state": "dead_letter",
                "qbo_push_attempts": attempts,
                "qbo_push_last_error": last_error or "max retries exceeded",
                "updated_at": utcnow().isoformat(),
            }
            await self._invoice_service._es.update_document(
                INVOICES_CURRENT_INDEX,
                invoice_id,
                update_doc,
            )
            logger.warning(
                "CommerceExternalSync: invoice=%s dead-lettered after %d "
                "QBO push attempts (tenant=%s)",
                invoice_id,
                attempts,
                tenant_id,
            )
        except Exception as exc:
            logger.error(
                "CommerceExternalSync: failed to mark invoice=%s as "
                "dead_letter: %s",
                invoice_id,
                exc,
            )

    async def _mark_qbo_push_retry(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
        attempts: int,
        last_error: Optional[str],
    ) -> None:
        """Update an invoice's QBO push retry state."""
        try:
            from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX

            update_doc = {
                "qbo_push_state": "retry",
                "qbo_push_attempts": attempts,
                "qbo_push_last_error": last_error,
                "updated_at": utcnow().isoformat(),
            }
            await self._invoice_service._es.update_document(
                INVOICES_CURRENT_INDEX,
                invoice_id,
                update_doc,
            )
            logger.info(
                "CommerceExternalSync: invoice=%s QBO push attempt %d "
                "failed, will retry (tenant=%s)",
                invoice_id,
                attempts,
                tenant_id,
            )
        except Exception as exc:
            logger.error(
                "CommerceExternalSync: failed to update qbo_push_state "
                "for invoice=%s: %s",
                invoice_id,
                exc,
            )

    def _resolve_invoice_from_linked_txn(
        self, qbo_event: Dict[str, Any]
    ) -> Optional[str]:
        """Try to resolve a canonical invoice_id from QBO LinkedTxn.

        QBO Payments link to QBO Invoices via LinkedTxn. If the
        platform has stored the QBO Invoice ID as
        Invoice.external_refs.qbo, we can resolve back to the
        canonical invoice_id.

        For now, this returns None — the full resolution requires an
        ES lookup that will be wired in task 9.3 when the subscriber
        registration is complete.
        """
        # LinkedTxn resolution will be wired when the subscriber
        # registration (task 9.3) provides the lookup infrastructure.
        # The matched_invoice_id field on the event is the primary
        # resolution path.
        return None

    def _derive_qbo_payment_method(self, qbo_event: Dict[str, Any]) -> str:
        """Derive the canonical payment method from QBO PaymentMethodRef.

        Maps QBO payment method names to the canonical method enum:
        check, wire, ach, other.
        """
        method_ref = qbo_event.get("PaymentMethodRef") or {}
        method_name = str(method_ref.get("name", "")).lower().strip()

        if "check" in method_name:
            return "check"
        elif "wire" in method_name:
            return "wire"
        elif "ach" in method_name or "bank" in method_name:
            return "ach"
        elif "card" in method_name or "credit" in method_name:
            return "card"
        else:
            return "other"

    def _derive_stripe_payment_method(self, stripe_event: Dict[str, Any]) -> str:
        """Derive the canonical payment method from Stripe event.

        Maps Stripe payment_method_types to the canonical method enum:
        card, ach, other.
        """
        pm_types = stripe_event.get("payment_method_types") or []

        if isinstance(pm_types, list) and pm_types:
            first_type = str(pm_types[0]).lower()
            if "card" in first_type:
                return "card"
            elif "ach" in first_type or "us_bank_account" in first_type:
                return "ach"
            elif "wire" in first_type:
                return "wire"

        # Fallback: check the payment_method_type field (singular)
        pm_type = str(stripe_event.get("payment_method_type", "")).lower()
        if "card" in pm_type:
            return "card"
        elif "ach" in pm_type or "bank" in pm_type:
            return "ach"

        return "card"  # Default for Stripe is card

    def _parse_qbo_txn_date(self, txn_date: Optional[str]) -> Optional[Any]:
        """Parse a QBO TxnDate string into a datetime, or None."""
        if not txn_date:
            return None
        try:
            from datetime import datetime, timezone

            # QBO dates are typically YYYY-MM-DD
            dt = datetime.strptime(str(txn_date)[:10], "%Y-%m-%d")
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
