"""Invoice lifecycle + event log.

Implements the InvoiceService with generate_from_order, finalize_draft,
apply_payment, void, mark_overdue, get, and list methods per design section 4.3.

Every state-changing method writes an InvoiceEvent first, then updates the
invoices_current projection. The projection update is idempotent via
sequence_number CAS (if event already applied, skip).

Includes the force=true void-with-applied-payments flow from Req 5.5.

Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, C1, C2, C3, C7
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from commerce.models.events import InvoiceEvent, InvoiceEventType
from commerce.models.invoice import InvoiceStatus, QBOPushState
from commerce.services.commerce_es_mappings import (
    ACCOUNTS_CURRENT_INDEX,
    INVOICE_EVENTS_INDEX,
    INVOICES_CURRENT_INDEX,
    PAYMENTS_CURRENT_INDEX,
)
from errors.exceptions import conflict, resource_not_found, validation_error
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 200
_DEFAULT_DRAFT_GRACE_SECONDS = 300


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class InvoiceService:
    """Service layer for Invoice lifecycle management with event sourcing.

    Every state-changing method writes an InvoiceEvent to invoice_events
    FIRST, then updates the invoices_current projection. The projection
    update is idempotent via event sequence_number check so replayed
    events never double-apply (Constraint C7).

    Every public method takes ``tenant_id`` and every ES query passes
    through ``inject_tenant_filter`` (Constraint C3).
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        idempotency_service=None,
        account_service=None,
        payment_service=None,
        external_sync=None,
        dunning_service=None,
        invoice_ws_manager=None,
        tax_engine_factory: Optional[Callable[[str], Any]] = None,
        sales_pricing_engine_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._es = es_service
        self._idempotency = idempotency_service
        self._account_service = account_service
        self._payment_service = payment_service
        self._external_sync = external_sync
        self._dunning_service = dunning_service
        self._invoice_ws_manager = invoice_ws_manager
        # Optional TaxEngine factory (tenant_id) -> TaxEngine. When
        # supplied, generate_from_order() computes a tax breakdown via
        # the engine and appends it to the invoice projection. When
        # None, InvoiceService falls back to the legacy behaviour of
        # honoring the caller-provided ``tax_cents`` value — this
        # preserves backwards compatibility for the commerce-backbone
        # tests and any caller that has not yet opted into the
        # fuel-compliance-backbone tax wiring (task 3.10).
        self._tax_engine_factory = tax_engine_factory
        # Optional SalesPricingEngine factory (tenant_id) ->
        # SalesPricingEngine. When supplied, generate_from_order()
        # resolves the sell price for each line item before tax
        # computation and updates unit_price_cents on the line.
        # When None, existing line item prices are used as-is —
        # backwards compatible (task 5.11).
        self._sales_pricing_engine_factory = sales_pricing_engine_factory
        # Optional DyedDieselEnforcer instance. When supplied,
        # generate_from_order() performs a post-generation validation
        # to confirm that dyed-diesel invoices exclude road-use excise
        # tax (Req 6.5) and logs the sale for IRS audit (Req 6.7).
        # Injected via set_dyed_diesel_enforcer() from bootstrap.
        self._dyed_diesel_enforcer: Optional[Any] = None

    # ------------------------------------------------------------------
    # Dependency injection setters
    # ------------------------------------------------------------------

    def set_dyed_diesel_enforcer(self, enforcer) -> None:
        """Inject the DyedDieselEnforcer for post-generation invoice validation.

        When set, generate_from_order() will:
        1. Call validate_invoice() to confirm tax exemption was applied (Req 6.5)
        2. If validation passes and the invoice contains dyed diesel,
           call log_dyed_sale() to persist the audit record (Req 6.7)

        This is a post-generation check — validation failures are logged
        as warnings but do not block the invoice.

        Validates: Requirements 6.5, 6.7
        """
        self._dyed_diesel_enforcer = enforcer

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    async def _get_next_sequence_number(
        self, tenant_id: str, invoice_id: str
    ) -> int:
        """Get the next sequence number for an invoice's event log.

        Queries invoice_events for the highest sequence_number for this
        invoice and returns max + 1.
        """
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"invoice_id": invoice_id}},
                    ]
                }
            },
            "size": 0,
            "aggs": {
                "max_seq": {"max": {"field": "sequence_number"}},
            },
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            INVOICE_EVENTS_INDEX, query, size=0
        )

        aggs = response.get("aggregations", {})
        max_seq = aggs.get("max_seq", {}).get("value")

        if max_seq is None:
            return 1
        return int(max_seq) + 1

    async def _write_invoice_event(
        self,
        tenant_id: str,
        invoice_id: str,
        event_type: InvoiceEventType,
        payload: Dict[str, Any],
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Write an InvoiceEvent to the invoice_events index.

        Returns the event document as a dict. This MUST be called before
        updating the projection (Constraint C7).
        """
        seq = await self._get_next_sequence_number(tenant_id, invoice_id)
        now = utcnow()

        event = InvoiceEvent(
            invoice_id=invoice_id,
            tenant_id=tenant_id,
            event_type=event_type,
            payload=payload,
            occurred_at=now,
            actor=actor,
            sequence_number=seq,
        )

        event_doc = event.model_dump()
        # Serialize datetime to ISO string for ES
        event_doc["occurred_at"] = event_doc["occurred_at"].isoformat()

        await self._es.index_document(
            INVOICE_EVENTS_INDEX, event_doc["event_id"], event_doc
        )

        logger.info(
            "Wrote invoice event %s (type=%s, seq=%d) for invoice %s tenant %s",
            event_doc["event_id"],
            event_type.value,
            seq,
            invoice_id,
            tenant_id,
        )
        return event_doc

    async def _get_current_sequence_on_projection(
        self, tenant_id: str, invoice_id: str
    ) -> int:
        """Get the last applied sequence_number from the projection.

        Used for idempotent projection updates: if the event's
        sequence_number <= this value, the projection is already up to date.
        """
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"invoice_id": invoice_id}},
                    ]
                }
            },
            "size": 0,
            "aggs": {
                "max_seq": {"max": {"field": "sequence_number"}},
            },
        }
        query = inject_tenant_filter(query, tenant_id)

        response = await self._es.search_documents(
            INVOICE_EVENTS_INDEX, query, size=0
        )

        aggs = response.get("aggregations", {})
        max_seq = aggs.get("max_seq", {}).get("value")
        if max_seq is None:
            return 0
        return int(max_seq)

    async def _update_projection(
        self,
        invoice_id: str,
        partial: Dict[str, Any],
        event_sequence: int,
    ) -> None:
        """Update the invoices_current projection idempotently.

        The projection stores the last applied sequence_number in a
        metadata field. If event_sequence <= stored sequence, the update
        is skipped (CAS idempotency).
        """
        # Always stamp updated_at and the last applied sequence
        partial["updated_at"] = utcnow().isoformat()
        partial["_last_applied_seq"] = event_sequence

        await self._es.update_document(
            INVOICES_CURRENT_INDEX, invoice_id, partial
        )

    async def _broadcast_invoice_ws(self, invoice_doc: Dict[str, Any]) -> None:
        """Non-blocking broadcast of the updated invoice projection on the WS channel.

        Failures are logged but never propagate — broadcast errors must not
        affect service operation (Design §6).
        """
        if self._invoice_ws_manager is None:
            return
        try:
            await self._invoice_ws_manager.broadcast_invoice_update(invoice_doc)
        except Exception as exc:
            logger.warning(
                "WS broadcast failed for invoice %s: %s",
                invoice_doc.get("invoice_id"),
                exc,
            )

    # ------------------------------------------------------------------
    # Dyed diesel post-generation check (Task 9.9 / Req 6.5, 6.7)
    # ------------------------------------------------------------------

    async def _run_dyed_diesel_post_check(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
        customer_id: str,
        line_items: List[Dict[str, Any]],
        doc: Dict[str, Any],
    ) -> None:
        """Post-generation dyed diesel validation and audit logging.

        Called after the invoice is persisted. This is a non-blocking
        check — failures are logged as warnings but never block the
        invoice generation pipeline.

        Steps:
        1. Call validate_invoice() to confirm tax exemption was applied.
        2. If validation fails, log a warning (don't block the invoice).
        3. If the invoice contains dyed diesel and passes validation,
           call log_dyed_sale() to persist the IRS audit record.

        Validates: Requirements 6.5, 6.7
        """
        from compliance.services.dyed_diesel_enforcer import DyedDieselEnforcer

        enforcer = self._dyed_diesel_enforcer

        try:
            # Step 1: Validate that dyed-diesel invoices exclude
            # road-use excise tax (Req 6.5)
            result = await enforcer.validate_invoice(
                tenant_id=tenant_id,
                invoice_id=invoice_id,
            )

            if not result.valid:
                # Step 2: Validation failed — log warning but don't block
                logger.warning(
                    "InvoiceService: dyed diesel validation failed for "
                    "invoice %s (tenant %s): [%s] %s",
                    invoice_id,
                    tenant_id,
                    result.error_code,
                    result.message,
                )
                return

            # Step 3: Check if the invoice contains dyed diesel and
            # log the sale for IRS audit readiness (Req 6.7)
            dyed_items = [
                item for item in line_items
                if DyedDieselEnforcer.is_dyed_diesel(
                    item.get("product_code", "")
                )
            ]

            if not dyed_items:
                # No dyed diesel on this invoice — nothing to log
                return

            # Sum gallons across all dyed-diesel line items
            total_dyed_gallons = sum(
                item.get("quantity_gallons", item.get("quantity", 0))
                for item in dyed_items
            )

            # Retrieve certificate info from the invoice's
            # exemptions_applied or query the enforcer's ES index.
            # For the audit log, we use the first dyed product code
            # and attempt to extract certificate details from the
            # tax_breakdown exemptions if available.
            product_code = dyed_items[0].get("product_code", "DYED_DIESEL")
            certificate_id = "unknown"
            certificate_expiry = "unknown"

            # Try to extract certificate info from exemptions_applied
            exemptions = doc.get("exemptions_applied", [])
            if exemptions:
                # The exemption id may encode the certificate number
                certificate_id = exemptions[0]

            await enforcer.log_dyed_sale(
                tenant_id=tenant_id,
                customer_id=customer_id,
                certificate_id=certificate_id,
                certificate_expiry=certificate_expiry,
                gallons=total_dyed_gallons,
                invoice_id=invoice_id,
                product_code=product_code,
            )

        except Exception as exc:
            # Non-blocking: log the failure but never raise.
            # The invoice generation must not be blocked by a
            # dyed-diesel validation or audit log failure.
            logger.warning(
                "InvoiceService: dyed diesel post-check failed for "
                "invoice %s (tenant %s): %s",
                invoice_id,
                tenant_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Generate from order (Req 5.1)
    # ------------------------------------------------------------------

    async def generate_from_order(
        self,
        *,
        tenant_id: str,
        order_id: str,
        customer_id: str,
        account_id: str,
        line_items: List[Dict[str, Any]],
        tax_cents: int = 0,
        net_terms_days: int = 30,
        actor: str = "system",
        destination_fips: Optional[str] = None,
        effective_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Generate an Invoice from a delivered order.

        Idempotent via IdempotencyService key
        'idemp:{tenant_id}:invoice_from_order:{order_id}'.

        Creates the invoice in draft status. Line items are carried from
        the source order. total_cents = sum of line subtotals + tax_cents.

        When a ``TaxEngine`` factory is wired via the constructor
        (fuel-compliance-backbone task 3.10) AND ``destination_fips`` is
        provided, the TaxEngine computes a per-invoice
        :class:`TaxBreakdown` that supersedes the caller-provided
        ``tax_cents`` value. The breakdown is persisted on the invoice
        as ``tax_breakdown`` and the honored exemption ids on
        ``exemptions_applied`` for IRS / operator audit (Req 6.7).

        The wiring is strictly opt-in — when no TaxEngine factory is
        configured, or when ``destination_fips`` is missing, the method
        falls back to the legacy behaviour (trust the caller's
        ``tax_cents``) so commerce-backbone tests and callers that have
        not yet adopted the compliance backbone continue to pass.

        Args:
            tenant_id: Tenant scope.
            order_id: Delivered order the invoice is generated from.
                Used as the idempotency key.
            customer_id: Parent customer of the invoice.
            account_id: Billing account the invoice is posted against.
            line_items: Per-product line items (already priced).
            tax_cents: Fallback tax total when no TaxEngine is wired or
                when ``destination_fips`` is missing. Ignored when the
                TaxEngine computes a breakdown.
            net_terms_days: Days until the invoice due date.
            actor: Audit actor for the event log.
            destination_fips: Delivery destination FIPS code used by
                the TaxEngine to resolve jurisdictional rates. When
                ``None``, tax computation is skipped (a warning is
                logged) and ``tax_cents`` is honored as-is.
            effective_date: Invoice / delivery date used by the
                TaxEngine for jurisdiction + exemption lookups.
                Defaults to today when not provided.

        Raises:
            TaxJurisdictionNotFoundError: Propagated from
                :meth:`TaxEngine.compute_tax` when a required
                jurisdiction row is missing from the
                ``tax_jurisdictions`` index (Req 1.9). Operators must
                add the missing row before the invoice finalizes.

        Validates: Requirements 5.1, 5.2, C1, C4, C7, 1.9, 1.10
        """
        # Idempotency check
        idemp_key = f"invoice_from_order:{order_id}"
        if self._idempotency:
            is_dup = await self._idempotency.is_duplicate(idemp_key, tenant_id)
            if is_dup:
                # Return the existing invoice
                existing = await self._find_invoice_by_order(tenant_id, order_id)
                if existing:
                    logger.info(
                        "Idempotent skip: invoice already exists for order %s tenant %s",
                        order_id,
                        tenant_id,
                    )
                    return existing
                # If we marked processed but can't find the invoice, fall through
                # (edge case: partial failure on first attempt)

        now = utcnow()
        invoice_id = f"inv_{uuid4()}"

        # --- Pricing resolution (optional, via injected SalesPricingEngine) ---
        # When a SalesPricingEngine factory is wired (task 5.11),
        # resolve the sell price for each line item before tax
        # computation and update unit_price_cents on the line.
        # Backwards compatible — if no factory, use existing prices.
        if self._sales_pricing_engine_factory is not None:
            try:
                pricing_engine = self._sales_pricing_engine_factory(tenant_id)
            except Exception as exc:
                logger.warning(
                    "InvoiceService: SalesPricingEngine factory raised "
                    "for tenant %s order %s: %s — using existing prices",
                    tenant_id,
                    order_id,
                    exc,
                )
                pricing_engine = None

            if pricing_engine is not None:
                _eff_date = effective_date or now.date()
                for item in line_items:
                    try:
                        resolution = await pricing_engine.resolve_price(
                            customer_id=customer_id,
                            product_code=item.get("product_code", ""),
                            gallons=item.get("quantity", 0),
                            terminal_id=item.get("terminal_id", ""),
                            route_miles=item.get("route_miles", 0.0),
                            effective_date=_eff_date,
                            market_price_cents=item.get(
                                "market_price_cents"
                            ),
                            account_id=account_id,
                        )
                        item["unit_price_cents"] = (
                            resolution.effective_price_cents
                        )
                        # Recompute subtotal for the line
                        qty = item.get("quantity", 0)
                        item["subtotal_cents"] = round(
                            resolution.effective_price_cents * qty
                        )
                    except Exception as exc:
                        # Pricing failure for a single line item should
                        # not block the entire invoice — log and keep
                        # the existing price.
                        logger.warning(
                            "InvoiceService: pricing resolution failed "
                            "for line item product=%s tenant=%s order=%s: "
                            "%s — using existing unit_price_cents",
                            item.get("product_code"),
                            tenant_id,
                            order_id,
                            exc,
                        )

        # Compute totals from line items (integer cents only, C1)
        subtotal_cents = sum(item.get("subtotal_cents", 0) for item in line_items)

        # --- Tax computation (optional, via injected TaxEngine) ------
        # When a TaxEngine factory is wired AND the caller supplied a
        # destination_fips, compute a per-invoice TaxBreakdown via
        # TaxEngine.compute_tax and persist it on the invoice. When
        # either is missing, fall back to the legacy behaviour: trust
        # the caller's ``tax_cents`` value.
        tax_breakdown_doc: Optional[Dict[str, Any]] = None
        exemptions_applied: List[str] = []
        computed_tax_cents: Optional[int] = None
        if self._tax_engine_factory is not None and destination_fips:
            try:
                tax_engine = self._tax_engine_factory(tenant_id)
            except Exception as exc:
                # Factory construction failure should not block invoice
                # generation — degrade to legacy tax_cents and log.
                logger.warning(
                    "InvoiceService: TaxEngine factory raised for tenant "
                    "%s order %s: %s — falling back to tax_cents=%s",
                    tenant_id,
                    order_id,
                    exc,
                    tax_cents,
                )
                tax_engine = None

            if tax_engine is not None:
                tax_breakdown_doc, exemptions_applied, computed_tax_cents = (
                    await self._compute_tax_for_line_items(
                        tax_engine=tax_engine,
                        tenant_id=tenant_id,
                        order_id=order_id,
                        customer_id=customer_id,
                        line_items=line_items,
                        destination_fips=destination_fips,
                        effective_date=effective_date,
                    )
                )
        elif self._tax_engine_factory is not None and not destination_fips:
            # Graceful-miss path: TaxEngine is wired but the caller did
            # not supply a destination_fips — log a warning and skip
            # tax computation rather than raising. The caller's
            # ``tax_cents`` (if any) is honored as-is.
            logger.warning(
                "InvoiceService: TaxEngine wired but destination_fips is "
                "missing for tenant=%s order=%s — skipping tax "
                "computation; honoring tax_cents=%d",
                tenant_id,
                order_id,
                tax_cents,
            )

        # When the TaxEngine produced a breakdown, the computed total
        # supersedes the caller-provided tax_cents.
        effective_tax_cents = (
            computed_tax_cents if computed_tax_cents is not None else tax_cents
        )
        total_cents = subtotal_cents + effective_tax_cents
        remaining_cents = total_cents

        # Compute due_date from net_terms_days
        due_date_val = (now + timedelta(days=net_terms_days)).date().isoformat()

        # Build the invoice document
        doc: Dict[str, Any] = {
            "invoice_id": invoice_id,
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "account_id": account_id,
            "order_id": order_id,
            "invoice_number": None,  # Assigned by invoice_numbering service
            "status": InvoiceStatus.DRAFT.value,
            "total_cents": total_cents,
            "amount_paid_cents": 0,
            "remaining_cents": remaining_cents,
            "tax_cents": effective_tax_cents,
            "subtotal_cents": subtotal_cents,
            "line_items": line_items,
            "issued_at": None,
            "due_date": due_date_val,
            "finalized_at": None,
            "voided_at": None,
            "void_reason": None,
            "qbo_push_state": QBOPushState.PENDING.value,
            "qbo_push_attempts": 0,
            "qbo_push_last_error": None,
            "external_refs": {},
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "_last_applied_seq": 1,
        }
        # Only attach tax_breakdown / exemptions_applied when the
        # TaxEngine actually produced a breakdown — otherwise omit the
        # fields so the legacy invoice shape is preserved for callers
        # that don't compute tax here.
        if tax_breakdown_doc is not None:
            doc["tax_breakdown"] = tax_breakdown_doc
            doc["exemptions_applied"] = exemptions_applied

        # Write event FIRST (Constraint C7)
        event_payload: Dict[str, Any] = {
            "order_id": order_id,
            "customer_id": customer_id,
            "account_id": account_id,
            "total_cents": total_cents,
            "subtotal_cents": subtotal_cents,
            "tax_cents": effective_tax_cents,
            "line_item_count": len(line_items),
        }
        if tax_breakdown_doc is not None:
            # Record the tax-engine computation on the event so the
            # event log captures the breakdown at the moment of
            # invoice creation (audit-ready).
            event_payload["tax_source"] = "tax_engine"
            event_payload["exemptions_applied"] = exemptions_applied
        else:
            event_payload["tax_source"] = "tax_cents_param"

        event_doc = await self._write_invoice_event(
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            event_type=InvoiceEventType.CREATED,
            payload=event_payload,
            actor=actor,
        )

        # Then update projection
        await self._es.index_document(INVOICES_CURRENT_INDEX, invoice_id, doc)

        # Mark as processed for idempotency
        if self._idempotency:
            await self._idempotency.mark_processed(idemp_key, tenant_id)

        # --- Dyed diesel post-generation validation (task 9.9) --------
        # When a DyedDieselEnforcer is wired, perform a post-generation
        # check to confirm that dyed-diesel invoices exclude road-use
        # excise tax (Req 6.5) and log the sale for IRS audit (Req 6.7).
        # This is non-blocking: failures are logged as warnings but do
        # not block the invoice generation.
        if self._dyed_diesel_enforcer is not None:
            await self._run_dyed_diesel_post_check(
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                customer_id=customer_id,
                line_items=line_items,
                doc=doc,
            )

        logger.info(
            "Generated invoice %s from order %s for tenant %s (total: %d cents)",
            invoice_id,
            order_id,
            tenant_id,
            total_cents,
        )
        return doc

    # ------------------------------------------------------------------
    # Finalize draft (Req 5.2)
    # ------------------------------------------------------------------

    async def finalize_draft(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Transition an invoice from draft to open.

        This is the idempotency cutoff: once finalized, the invoice
        cannot return to draft. Sets issued_at and finalized_at timestamps.

        Before finalizing, drains Account.credit_balance_cents into a
        synthetic Payment with source=account_credit, method=credit_balance
        against this invoice (Req 6.4).

        Validates: Requirements 5.2, 6.4, C2, C7
        """
        invoice = await self.get(tenant_id=tenant_id, invoice_id=invoice_id)

        current_status = invoice.get("status")

        # Idempotent: if already open or beyond, return as-is
        if current_status != InvoiceStatus.DRAFT.value:
            if current_status == InvoiceStatus.OPEN.value:
                return invoice
            raise conflict(
                f"Cannot finalize invoice in status '{current_status}'; must be 'draft'",
                error_code="INVALID_STATUS_TRANSITION",
                details={
                    "invoice_id": invoice_id,
                    "current_status": current_status,
                    "expected_status": "draft",
                },
            )

        # Drain Account.credit_balance_cents before finalizing (Req 6.4)
        await self._drain_credit_balance(
            tenant_id=tenant_id,
            invoice=invoice,
            actor=actor,
        )

        # Re-fetch the invoice in case credit balance was applied
        invoice = await self.get(tenant_id=tenant_id, invoice_id=invoice_id)

        now = utcnow()

        # Write event FIRST (Constraint C7)
        event_doc = await self._write_invoice_event(
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            event_type=InvoiceEventType.FINALIZED,
            payload={
                "previous_status": InvoiceStatus.DRAFT.value,
                "new_status": InvoiceStatus.OPEN.value,
            },
            actor=actor,
        )

        # Update projection
        partial: Dict[str, Any] = {
            "status": InvoiceStatus.OPEN.value,
            "issued_at": now.isoformat(),
            "finalized_at": now.isoformat(),
        }
        await self._update_projection(
            invoice_id, partial, event_doc["sequence_number"]
        )

        merged = {**invoice, **partial, "updated_at": utcnow().isoformat()}
        logger.info(
            "Finalized invoice %s (draft -> open) for tenant %s",
            invoice_id,
            tenant_id,
        )

        # Broadcast updated projection on WS channel (Design §6)
        await self._broadcast_invoice_ws(merged)

        # Post-commit callback: fire external sync as a non-blocking
        # asyncio task so HTTP latency is unaffected by QBO push latency.
        # Errors are isolated — a failed push never affects the finalize
        # response (Design §7, Task 9.2).
        if self._external_sync is not None:
            asyncio.ensure_future(
                self._safe_external_sync_callback(merged)
            )

        return merged

    async def _safe_external_sync_callback(
        self, invoice: Dict[str, Any]
    ) -> None:
        """Fire-and-forget wrapper for external sync on_invoice_finalized.

        Catches all exceptions so a QBO push failure never propagates
        back to the caller or crashes the event loop. This is the
        error-isolation boundary required by Design §7.
        """
        try:
            await self._external_sync.on_invoice_finalized(invoice)
        except Exception as exc:
            logger.error(
                "External sync callback failed for invoice %s: %s",
                invoice.get("invoice_id"),
                exc,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Apply payment (Req 5.3)
    # ------------------------------------------------------------------

    async def apply_payment(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
        amount_cents: int,
        payment_id: Optional[str] = None,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Apply a payment amount to an invoice.

        Transitions:
        - open -> partial when 0 < amount_paid < total
        - open/partial -> paid when amount_paid >= total

        Validates: Requirements 5.3, C1, C7
        """
        if amount_cents <= 0:
            raise validation_error(
                "amount_cents must be positive",
                details={"amount_cents": amount_cents},
            )

        invoice = await self.get(tenant_id=tenant_id, invoice_id=invoice_id)
        current_status = invoice.get("status")

        # Can only apply payments to open, partial, or overdue invoices
        allowed_statuses = {
            InvoiceStatus.OPEN.value,
            InvoiceStatus.PARTIAL.value,
            InvoiceStatus.OVERDUE.value,
        }
        if current_status not in allowed_statuses:
            raise conflict(
                f"Cannot apply payment to invoice in status '{current_status}'",
                error_code="INVALID_STATUS_TRANSITION",
                details={
                    "invoice_id": invoice_id,
                    "current_status": current_status,
                    "allowed_statuses": list(allowed_statuses),
                },
            )

        # Compute new amounts (integer cents only, C1)
        current_paid = invoice.get("amount_paid_cents", 0)
        total_cents = invoice.get("total_cents", 0)
        new_paid = current_paid + amount_cents
        new_remaining = max(0, total_cents - new_paid)

        # Determine new status
        if new_paid >= total_cents:
            new_status = InvoiceStatus.PAID.value
        elif new_paid > 0:
            new_status = InvoiceStatus.PARTIAL.value
        else:
            new_status = current_status

        # Write event FIRST (Constraint C7)
        event_doc = await self._write_invoice_event(
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            event_type=InvoiceEventType.PAYMENT_APPLIED,
            payload={
                "amount_cents": amount_cents,
                "payment_id": payment_id,
                "previous_amount_paid_cents": current_paid,
                "new_amount_paid_cents": new_paid,
                "previous_status": current_status,
                "new_status": new_status,
            },
            actor=actor,
        )

        # Update projection
        partial: Dict[str, Any] = {
            "amount_paid_cents": new_paid,
            "remaining_cents": new_remaining,
            "status": new_status,
        }
        await self._update_projection(
            invoice_id, partial, event_doc["sequence_number"]
        )

        merged = {**invoice, **partial, "updated_at": utcnow().isoformat()}
        logger.info(
            "Applied payment of %d cents to invoice %s (status: %s -> %s) tenant %s",
            amount_cents,
            invoice_id,
            current_status,
            new_status,
            tenant_id,
        )

        # Broadcast updated projection on WS channel (Design §6)
        await self._broadcast_invoice_ws(merged)

        # Cancel dunning notifications when invoice transitions to paid (Req 7.5)
        if new_status == InvoiceStatus.PAID.value and self._dunning_service:
            try:
                await self._dunning_service.cancel_for_invoice(
                    tenant_id=tenant_id,
                    invoice_id=invoice_id,
                    reason="invoice_paid",
                )
            except Exception as exc:
                logger.warning(
                    "Failed to cancel dunning for paid invoice %s: %s",
                    invoice_id,
                    exc,
                )

        return merged

    # ------------------------------------------------------------------
    # Void (Req 5.5)
    # ------------------------------------------------------------------

    async def void(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
        reason: str,
        actor: str,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Void an invoice.

        If amount_paid_cents == 0: transitions directly to void.
        If amount_paid_cents > 0 and force=False: rejects with HTTP 409.
        If amount_paid_cents > 0 and force=True: auto-reverses all applied
        payments with source=void_cascade before voiding (Req 5.5).

        Void is a terminal state.

        Validates: Requirements 5.5, C7
        """
        invoice = await self.get(tenant_id=tenant_id, invoice_id=invoice_id)
        current_status = invoice.get("status")

        # Cannot void an already-voided invoice
        if current_status == InvoiceStatus.VOID.value:
            raise conflict(
                "Invoice is already voided",
                error_code="INVALID_STATUS_TRANSITION",
                details={"invoice_id": invoice_id, "current_status": "void"},
            )

        # Cannot void a paid invoice without force
        amount_paid = invoice.get("amount_paid_cents", 0)

        if amount_paid > 0 and not force:
            raise conflict(
                "Cannot void invoice with applied payments without force=true",
                error_code="INVALID_STATUS_TRANSITION",
                details={
                    "invoice_id": invoice_id,
                    "amount_paid_cents": amount_paid,
                    "hint": "Use force=true with authorized_by to void with applied payments",
                },
            )

        now = utcnow()
        reversed_payments: List[Dict[str, Any]] = []

        # If force=true and there are applied payments, reverse them
        if amount_paid > 0 and force:
            reversed_payments = await self._reverse_applied_payments(
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                actor=actor,
            )

        # Write void event FIRST (Constraint C7)
        event_doc = await self._write_invoice_event(
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            event_type=InvoiceEventType.VOIDED,
            payload={
                "reason": reason,
                "previous_status": current_status,
                "force": force,
                "reversed_payment_count": len(reversed_payments),
                "reversed_payment_ids": [
                    p.get("payment_id") for p in reversed_payments
                ],
            },
            actor=actor,
        )

        # Update projection
        partial: Dict[str, Any] = {
            "status": InvoiceStatus.VOID.value,
            "voided_at": now.isoformat(),
            "void_reason": reason,
            "amount_paid_cents": 0,
            "remaining_cents": 0,
        }
        await self._update_projection(
            invoice_id, partial, event_doc["sequence_number"]
        )

        merged = {**invoice, **partial, "updated_at": utcnow().isoformat()}
        logger.info(
            "Voided invoice %s (reason: %s, force: %s, reversed %d payments) tenant %s",
            invoice_id,
            reason,
            force,
            len(reversed_payments),
            tenant_id,
        )

        # Broadcast updated projection on WS channel (Design §6)
        await self._broadcast_invoice_ws(merged)

        # Cancel dunning notifications when invoice is voided (Req 7.5)
        if self._dunning_service:
            try:
                await self._dunning_service.cancel_for_invoice(
                    tenant_id=tenant_id,
                    invoice_id=invoice_id,
                    reason="invoice_voided",
                )
            except Exception as exc:
                logger.warning(
                    "Failed to cancel dunning for voided invoice %s: %s",
                    invoice_id,
                    exc,
                )

        return merged

    # ------------------------------------------------------------------
    # Mark overdue (Req 5.4)
    # ------------------------------------------------------------------

    async def mark_overdue(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Transition an invoice to overdue status.

        Only applies to invoices in open or partial status whose due_date
        has passed. Overdue is not terminal; subsequent payments can
        transition back to partial/paid.

        Validates: Requirements 5.4, C7
        """
        invoice = await self.get(tenant_id=tenant_id, invoice_id=invoice_id)
        current_status = invoice.get("status")

        # Can only mark open or partial invoices as overdue
        allowed_statuses = {
            InvoiceStatus.OPEN.value,
            InvoiceStatus.PARTIAL.value,
        }
        if current_status not in allowed_statuses:
            # Idempotent: if already overdue, return as-is
            if current_status == InvoiceStatus.OVERDUE.value:
                return invoice
            raise conflict(
                f"Cannot mark invoice as overdue in status '{current_status}'",
                error_code="INVALID_STATUS_TRANSITION",
                details={
                    "invoice_id": invoice_id,
                    "current_status": current_status,
                    "allowed_statuses": list(allowed_statuses),
                },
            )

        # Write event FIRST (Constraint C7)
        event_doc = await self._write_invoice_event(
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            event_type=InvoiceEventType.OVERDUE_MARKED,
            payload={
                "previous_status": current_status,
                "new_status": InvoiceStatus.OVERDUE.value,
                "due_date": invoice.get("due_date"),
            },
            actor=actor,
        )

        # Update projection
        partial: Dict[str, Any] = {
            "status": InvoiceStatus.OVERDUE.value,
        }
        await self._update_projection(
            invoice_id, partial, event_doc["sequence_number"]
        )

        merged = {**invoice, **partial, "updated_at": utcnow().isoformat()}
        logger.info(
            "Marked invoice %s as overdue (was: %s) tenant %s",
            invoice_id,
            current_status,
            tenant_id,
        )

        # Broadcast updated projection on WS channel (Design §6)
        await self._broadcast_invoice_ws(merged)

        return merged

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    async def get(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
    ) -> Dict[str, Any]:
        """Retrieve a single Invoice by ID, scoped to tenant.

        Validates: Constraint C3
        """
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"invoice_id": invoice_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Invoice '{invoice_id}' not found",
                details={"invoice_id": invoice_id},
            )

        return hits[0]["_source"]

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list(
        self,
        *,
        tenant_id: str,
        status: Optional[str] = None,
        customer_id: Optional[str] = None,
        account_id: Optional[str] = None,
        order_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> Dict[str, Any]:
        """List Invoices for a tenant with cursor/limit pagination.

        Default limit is 50, max 200. Supports filtering by status,
        customer_id, account_id, and order_id.

        Validates: Constraint C3
        """
        # Clamp limit
        if limit < 1:
            limit = _DEFAULT_PAGE_LIMIT
        if limit > _MAX_PAGE_LIMIT:
            limit = _MAX_PAGE_LIMIT

        must_clauses: List[Dict[str, Any]] = []
        if status:
            must_clauses.append({"term": {"status": status}})
        if customer_id:
            must_clauses.append({"term": {"customer_id": customer_id}})
        if account_id:
            must_clauses.append({"term": {"account_id": account_id}})
        if order_id:
            must_clauses.append({"term": {"order_id": order_id}})

        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": must_clauses if must_clauses else [{"match_all": {}}],
                }
            },
            "size": limit,
            "sort": [
                {"created_at": {"order": "desc"}},
                {"invoice_id": {"order": "asc"}},
            ],
        }

        # Cursor-based pagination using search_after
        if cursor:
            base_query["search_after"] = [cursor, cursor]

        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, query, size=limit
        )

        hits = response["hits"]["hits"]
        items = [hit["_source"] for hit in hits]

        # Determine next cursor
        next_cursor: Optional[str] = None
        if hits and len(hits) == limit:
            last_sort = hits[-1].get("sort")
            if last_sort and len(last_sort) >= 2:
                next_cursor = hits[-1]["_source"]["invoice_id"]

        return {
            "items": items,
            "next_cursor": next_cursor,
            "limit": limit,
        }

    # ------------------------------------------------------------------
    # Get events timeline
    # ------------------------------------------------------------------

    async def get_events(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
    ) -> List[Dict[str, Any]]:
        """Retrieve the full event log for an invoice, ordered by sequence.

        Validates: Constraint C3, C7
        """
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"invoice_id": invoice_id}},
                    ]
                }
            },
            "size": 1000,
            "sort": [
                {"sequence_number": {"order": "asc"}},
            ],
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            INVOICE_EVENTS_INDEX, query, size=1000
        )

        hits = response["hits"]["hits"]
        return [hit["_source"] for hit in hits]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _compute_tax_for_line_items(
        self,
        *,
        tax_engine: Any,
        tenant_id: str,
        order_id: str,
        customer_id: str,
        line_items: List[Dict[str, Any]],
        destination_fips: str,
        effective_date: Optional[date],
    ) -> "tuple[Optional[Dict[str, Any]], List[str], Optional[int]]":
        """Compute the aggregate :class:`TaxBreakdown` for an invoice.

        Iterates the invoice's line items and invokes
        :meth:`TaxEngine.compute_tax` once per ``(product_code,
        quantity_gallons)`` so the computed tax matches the delivered
        volume of each line (fuel-compliance-backbone task 3.10).

        The per-line breakdowns are aggregated into a single invoice-
        level breakdown — component cents are summed, detail line items
        are concatenated, and exemption ids are de-duplicated. The
        resulting dict is persisted as ``tax_breakdown`` on the invoice
        projection and the exemption id list as ``exemptions_applied``
        (Req 1.10, Req 6.7).

        Args:
            tax_engine: Tenant-scoped TaxEngine instance already built
                via the factory.
            tenant_id / order_id / customer_id: Invoice context for
                logging and exemption lookup.
            line_items: Line items on the invoice, each expected to
                carry ``product_code`` and ``quantity_gallons``.
            destination_fips: Delivery destination FIPS code.
            effective_date: Invoice / delivery date; defaulted to
                today by the TaxEngine when ``None``.

        Returns:
            ``(breakdown_doc, exemptions_applied, total_tax_cents)``.
            ``breakdown_doc`` is the serialized :class:`TaxBreakdown`
            aggregate, or ``None`` when no taxable line items were
            present. ``total_tax_cents`` is the computed tax total that
            supersedes the caller-provided ``tax_cents``, or ``None``
            when no computation ran.

        Raises:
            TaxJurisdictionNotFoundError: Propagated unchanged from
                :meth:`TaxEngine.compute_tax` when a required
                jurisdiction row is missing. The caller (invoice
                generation pipeline) surfaces this as a structured
                rejection so operators can fix the tax_jurisdictions
                index before the invoice finalizes (Req 1.9).
        """
        # Aggregated component buckets (integer cents, Constraint C1).
        federal_cents = 0
        state_cents = 0
        county_cents = 0
        city_cents = 0
        ust_cents = 0
        spcc_cents = 0
        environmental_cents = 0
        aggregated_line_items: List[Dict[str, Any]] = []
        exemption_ids_seen: List[str] = []

        any_computed = False
        for item in line_items:
            product_code = item.get("product_code")
            quantity_gallons = item.get("quantity_gallons")

            # Skip rows that lack the inputs TaxEngine requires. The
            # commerce line item schema allows both fields to be
            # present; we guard defensively because some callers
            # (e.g. flat-fee lines) may omit them.
            if not isinstance(product_code, str) or not product_code:
                logger.debug(
                    "InvoiceService: skipping tax computation for line "
                    "without product_code (order=%s tenant=%s)",
                    order_id,
                    tenant_id,
                )
                continue
            if (
                quantity_gallons is None
                or not isinstance(quantity_gallons, (int, float))
                or quantity_gallons <= 0
            ):
                logger.debug(
                    "InvoiceService: skipping tax computation for line "
                    "with non-positive quantity_gallons (product=%s "
                    "order=%s tenant=%s)",
                    product_code,
                    order_id,
                    tenant_id,
                )
                continue

            # TaxJurisdictionNotFoundError is propagated — the invoice
            # generation pipeline is expected to surface it as a
            # structured rejection (Req 1.9).
            breakdown = await tax_engine.compute_tax(
                product_code=product_code,
                net_gallons=float(quantity_gallons),
                destination_fips=destination_fips,
                customer_id=customer_id,
                effective_date=effective_date,
            )
            any_computed = True

            federal_cents += breakdown.federal_cents
            state_cents += breakdown.state_cents
            county_cents += breakdown.county_cents
            city_cents += breakdown.city_cents
            ust_cents += breakdown.ust_cents
            spcc_cents += breakdown.spcc_cents
            environmental_cents += breakdown.environmental_cents

            # Serialize line items to plain dicts for ES storage.
            for tli in breakdown.line_items:
                aggregated_line_items.append(tli.model_dump())

            for exemption_id in breakdown.exemptions_applied:
                if exemption_id and exemption_id not in exemption_ids_seen:
                    exemption_ids_seen.append(exemption_id)

        if not any_computed:
            return None, [], None

        total_tax_cents = (
            federal_cents
            + state_cents
            + county_cents
            + city_cents
            + ust_cents
            + spcc_cents
            + environmental_cents
        )

        breakdown_doc: Dict[str, Any] = {
            "federal_cents": federal_cents,
            "state_cents": state_cents,
            "county_cents": county_cents,
            "city_cents": city_cents,
            "ust_cents": ust_cents,
            "spcc_cents": spcc_cents,
            "environmental_cents": environmental_cents,
            "total_tax_cents": total_tax_cents,
            "exemptions_applied": list(exemption_ids_seen),
            "line_items": aggregated_line_items,
            "destination_fips": destination_fips,
        }

        logger.info(
            "InvoiceService: computed tax breakdown for tenant=%s order=%s "
            "customer=%s destination_fips=%s total_tax_cents=%d "
            "exemptions=%s",
            tenant_id,
            order_id,
            customer_id,
            destination_fips,
            total_tax_cents,
            exemption_ids_seen,
        )
        return breakdown_doc, list(exemption_ids_seen), total_tax_cents

    async def _drain_credit_balance(
        self,
        *,
        tenant_id: str,
        invoice: Dict[str, Any],
        actor: str = "system",
    ) -> None:
        """Drain Account.credit_balance_cents into a synthetic Payment.

        Before finalizing an invoice, checks if the Account has
        credit_balance_cents > 0. If so, creates a synthetic Payment with
        source=account_credit, method=credit_balance, applies it against
        the invoice (up to the invoice remaining or the credit balance,
        whichever is less), and reduces Account.credit_balance_cents.

        The synthetic Payment is recorded in payments_current for full
        auditability.

        Since the invoice is still in draft status at this point, we
        directly update the invoice amounts rather than going through
        apply_payment (which requires open/partial/overdue status).

        Validates: Requirement 6.4
        """
        account_id = invoice.get("account_id")
        invoice_id = invoice.get("invoice_id")

        if not account_id or not invoice_id:
            return

        # Fetch the account to check credit_balance_cents
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"account_id": account_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            ACCOUNTS_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            return

        account = hits[0]["_source"]
        credit_balance = account.get("credit_balance_cents", 0)

        if credit_balance <= 0:
            return

        # Determine how much to apply: min(credit_balance, invoice.remaining_cents)
        remaining_cents = invoice.get("remaining_cents", 0)
        if remaining_cents <= 0:
            return

        apply_amount = min(credit_balance, remaining_cents)

        # Create a synthetic Payment with source=account_credit, method=credit_balance
        from uuid import uuid4 as _uuid4

        now = utcnow()
        payment_id = f"pay_{_uuid4()}"

        payment_doc: Dict[str, Any] = {
            "payment_id": payment_id,
            "tenant_id": tenant_id,
            "invoice_id": invoice_id,
            "account_id": account_id,
            "amount_cents": apply_amount,
            "source": "account_credit",
            "method": "credit_balance",
            "external_id": None,
            "reference": "Auto-applied from account credit balance",
            "status": "applied",
            "received_at": now.isoformat(),
            "applied_at": now.isoformat(),
            "reversed_at": None,
        }

        # Persist the synthetic payment
        await self._es.index_document(
            PAYMENTS_CURRENT_INDEX, payment_id, payment_doc
        )

        # Directly update the invoice amounts (invoice is still in draft,
        # so we can't use apply_payment which requires open/partial/overdue)
        current_paid = invoice.get("amount_paid_cents", 0)
        total_cents = invoice.get("total_cents", 0)
        new_paid = current_paid + apply_amount
        new_remaining = max(0, total_cents - new_paid)

        # Write a payment_applied event (C7)
        event_doc = await self._write_invoice_event(
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            event_type=InvoiceEventType.PAYMENT_APPLIED,
            payload={
                "amount_cents": apply_amount,
                "payment_id": payment_id,
                "source": "account_credit",
                "method": "credit_balance",
                "previous_amount_paid_cents": current_paid,
                "new_amount_paid_cents": new_paid,
                "previous_status": "draft",
                "new_status": "draft",
            },
            actor=actor,
        )

        # Update the invoice projection
        invoice_update: Dict[str, Any] = {
            "amount_paid_cents": new_paid,
            "remaining_cents": new_remaining,
        }
        await self._update_projection(
            invoice_id, invoice_update, event_doc["sequence_number"]
        )

        # Reduce Account.credit_balance_cents
        new_balance = credit_balance - apply_amount
        account_update: Dict[str, Any] = {
            "credit_balance_cents": new_balance,
            "updated_at": now.isoformat(),
        }
        await self._es.update_document(
            ACCOUNTS_CURRENT_INDEX, account_id, account_update
        )

        logger.info(
            "Drained %d cents from account %s credit balance into "
            "synthetic payment %s for invoice %s (balance: %d -> %d) tenant %s",
            apply_amount,
            account_id,
            payment_id,
            invoice_id,
            credit_balance,
            new_balance,
            tenant_id,
        )

    async def _find_invoice_by_order(
        self, tenant_id: str, order_id: str
    ) -> Optional[Dict[str, Any]]:
        """Find an existing invoice for a given order_id (idempotency lookup)."""
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"order_id": order_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            INVOICES_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if hits:
            return hits[0]["_source"]
        return None

    async def _reverse_applied_payments(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
        actor: str,
    ) -> List[Dict[str, Any]]:
        """Reverse all applied payments for an invoice (force void flow).

        Creates reversal records with source=void_cascade for each
        applied payment. Emits payment_reversed events.

        Validates: Requirement 5.5
        """
        # Find all applied payments for this invoice
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"invoice_id": invoice_id}},
                        {"term": {"status": "applied"}},
                    ]
                }
            },
            "size": 200,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            PAYMENTS_CURRENT_INDEX, query, size=200
        )

        hits = response["hits"]["hits"]
        reversed_payments: List[Dict[str, Any]] = []
        now = utcnow()

        for hit in hits:
            payment = hit["_source"]
            payment_id = payment.get("payment_id")

            # Mark the payment as reversed
            reversal_update: Dict[str, Any] = {
                "status": "reversed",
                "reversed_at": now.isoformat(),
            }
            await self._es.update_document(
                PAYMENTS_CURRENT_INDEX, payment_id, reversal_update
            )

            # Write a payment_reversed event on the invoice
            await self._write_invoice_event(
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                event_type=InvoiceEventType.PAYMENT_REVERSED,
                payload={
                    "payment_id": payment_id,
                    "amount_cents": payment.get("amount_cents", 0),
                    "source": "void_cascade",
                    "original_source": payment.get("source"),
                },
                actor=actor,
            )

            reversed_payments.append(payment)
            logger.info(
                "Reversed payment %s (%d cents, source=void_cascade) for invoice %s",
                payment_id,
                payment.get("amount_cents", 0),
                invoice_id,
            )

        return reversed_payments
