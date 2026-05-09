"""Payment ingestion + application.

Implements the PaymentService with ingest, apply, reverse, get, and list
methods per design section 4.3 and the payments_current ES mapping (§3.5).

Every ingest call uses the IdempotencyService key
``idemp:{tenant_id}:payment:{source}:{external_id}`` (Req 6.5).

After applying a payment, calls InvoiceService.apply_payment to update
the invoice amounts and state transitions (Req 5.3).

After reversing, updates invoice amounts and re-evaluates state (Req 6.6).

Overpayment handling (Req 6.4): if amount_cents > invoice.remaining_cents,
only apply remaining_cents to the invoice, accrue the excess to
Account.credit_balance_cents, and emit account_credit_balance_applied event.

Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, C1, C2, C3, C4
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from commerce.models.events import AccountEventType, InvoiceEventType
from commerce.models.payment import PaymentMethod, PaymentSource, PaymentStatus
from commerce.services.commerce_es_mappings import (
    ACCOUNTS_CURRENT_INDEX,
    ACCOUNT_EVENTS_INDEX,
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


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PaymentService:
    """Service layer for Payment ingestion, application, and reversal.

    Every public method takes ``tenant_id`` and every ES query passes
    through ``inject_tenant_filter`` (Constraint C3).

    Every ingest call uses the IdempotencyService key
    ``idemp:{tenant_id}:payment:{source}:{external_id}`` (Req 6.5, C4).
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        idempotency_service=None,
        invoice_service=None,
        account_service=None,
    ) -> None:
        self._es = es_service
        self._idempotency = idempotency_service
        self._invoice_service = invoice_service
        self._account_service = account_service

    # ------------------------------------------------------------------
    # Ingest (Req 6.1, 6.2, 6.3, 6.5)
    # ------------------------------------------------------------------

    async def ingest(
        self,
        *,
        tenant_id: str,
        invoice_id: str,
        account_id: str,
        amount_cents: int,
        source: str,
        method: str,
        external_id: Optional[str] = None,
        reference: Optional[str] = None,
        received_at: Optional[datetime] = None,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Ingest a payment and apply it to an invoice.

        Creates a Payment record in payments_current and applies it to
        the referenced invoice via InvoiceService.apply_payment.

        Idempotent via IdempotencyService key
        ``idemp:{tenant_id}:payment:{source}:{external_id}`` (Req 6.5).

        Args:
            tenant_id: Tenant identifier for data isolation.
            invoice_id: Invoice this payment applies to.
            account_id: Billing account identifier.
            amount_cents: Payment amount in integer cents (C1).
            source: System-of-record: stripe|qbo|manual|account_credit|void_cascade.
            method: Payment instrument: card|ach|wire|check|credit_balance|other.
            external_id: External system identifier (e.g. Stripe charge ID).
            reference: Free-text reference (check number, wire memo).
            received_at: When the payment was received externally.
            actor: Who triggered the ingestion.

        Returns:
            The created Payment document dict.

        Raises:
            ValidationError: If amount_cents <= 0 or source/method invalid.
            ConflictError: If the payment is a duplicate (idempotent skip).

        Validates: Requirements 6.1, 6.2, 6.3, 6.5, C1, C2, C3, C4
        """
        # Validate amount (C1)
        if amount_cents <= 0:
            raise validation_error(
                "amount_cents must be positive",
                details={"amount_cents": amount_cents},
            )

        # Validate source enum
        try:
            source_enum = PaymentSource(source)
        except ValueError:
            raise validation_error(
                f"Invalid payment source: '{source}'",
                details={
                    "source": source,
                    "allowed": [s.value for s in PaymentSource],
                },
            )

        # Validate method enum
        try:
            method_enum = PaymentMethod(method)
        except ValueError:
            raise validation_error(
                f"Invalid payment method: '{method}'",
                details={
                    "method": method,
                    "allowed": [m.value for m in PaymentMethod],
                },
            )

        # Idempotency check (Req 6.5, C4)
        # Key format: idemp:{tenant_id}:payment:{source}:{external_id}
        idemp_key = f"payment:{source}:{external_id}" if external_id else None
        if idemp_key and self._idempotency:
            is_dup = await self._idempotency.is_duplicate(idemp_key, tenant_id)
            if is_dup:
                # Return the existing payment
                existing = await self._find_payment_by_external_id(
                    tenant_id, source, external_id
                )
                if existing:
                    logger.info(
                        "Idempotent skip: payment already exists for "
                        "source=%s external_id=%s tenant=%s",
                        source,
                        external_id,
                        tenant_id,
                    )
                    return existing
                # If marked processed but can't find the payment, fall through
                # (edge case: partial failure on first attempt)

        now = utcnow()
        payment_id = f"pay_{uuid4()}"

        # Build the payment document
        doc: Dict[str, Any] = {
            "payment_id": payment_id,
            "tenant_id": tenant_id,
            "invoice_id": invoice_id,
            "account_id": account_id,
            "amount_cents": amount_cents,
            "source": source_enum.value,
            "method": method_enum.value,
            "external_id": external_id,
            "reference": reference,
            "status": PaymentStatus.APPLIED.value,
            "received_at": received_at.isoformat() if received_at else None,
            "applied_at": now.isoformat(),
            "reversed_at": None,
        }

        # Persist the payment record
        await self._es.index_document(PAYMENTS_CURRENT_INDEX, payment_id, doc)

        # Overpayment handling (Req 6.4):
        # If amount_cents > invoice.remaining_cents, only apply remaining_cents
        # to the invoice, accrue the excess to Account.credit_balance_cents,
        # and emit account_credit_balance_applied event.
        applied_to_invoice = amount_cents
        excess_cents = 0

        if self._invoice_service:
            # Fetch the invoice to check remaining_cents
            invoice = await self._invoice_service.get(
                tenant_id=tenant_id, invoice_id=invoice_id
            )
            remaining_cents = invoice.get("remaining_cents", 0)

            if amount_cents > remaining_cents and remaining_cents >= 0:
                # Overpayment: only apply what the invoice needs
                applied_to_invoice = remaining_cents
                excess_cents = amount_cents - remaining_cents
            else:
                applied_to_invoice = amount_cents

            # Apply the capped amount to the invoice (skip if nothing to apply)
            if applied_to_invoice > 0:
                await self._invoice_service.apply_payment(
                    tenant_id=tenant_id,
                    invoice_id=invoice_id,
                    amount_cents=applied_to_invoice,
                    payment_id=payment_id,
                    actor=actor,
                )

            # Accrue excess to Account.credit_balance_cents
            if excess_cents > 0:
                await self._accrue_credit_balance(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    excess_cents=excess_cents,
                    payment_id=payment_id,
                    invoice_id=invoice_id,
                    actor=actor,
                )

        # Mark as processed for idempotency
        if idemp_key and self._idempotency:
            await self._idempotency.mark_processed(idemp_key, tenant_id)

        logger.info(
            "Ingested payment %s (source=%s, method=%s, amount=%d cents) "
            "for invoice %s tenant %s",
            payment_id,
            source,
            method,
            amount_cents,
            invoice_id,
            tenant_id,
        )
        return doc

    # ------------------------------------------------------------------
    # Apply (applies an existing payment to its invoice)
    # ------------------------------------------------------------------

    async def apply(
        self,
        *,
        tenant_id: str,
        payment_id: str,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Apply an existing payment to its invoice.

        Retrieves the payment, verifies it is in 'applied' status, and
        calls InvoiceService.apply_payment to update the invoice amounts.

        This is used when a payment record exists but hasn't yet been
        applied to the invoice (e.g. re-processing after a partial failure).

        Args:
            tenant_id: Tenant identifier for data isolation.
            payment_id: The payment to apply.
            actor: Who triggered the application.

        Returns:
            The updated Invoice document dict after payment application.

        Raises:
            ResourceNotFoundError: If payment not found.
            ConflictError: If payment is already reversed.

        Validates: Requirements 5.3, C3
        """
        payment = await self.get(tenant_id=tenant_id, payment_id=payment_id)

        # Cannot apply a reversed payment
        if payment.get("status") == PaymentStatus.REVERSED.value:
            raise conflict(
                "Cannot apply a reversed payment",
                error_code="PAYMENT_ALREADY_REVERSED",
                details={"payment_id": payment_id, "status": "reversed"},
            )

        invoice_id = payment["invoice_id"]
        amount_cents = payment["amount_cents"]

        # Apply to the invoice via InvoiceService
        if self._invoice_service:
            result = await self._invoice_service.apply_payment(
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                amount_cents=amount_cents,
                payment_id=payment_id,
                actor=actor,
            )
            return result

        # If no invoice_service is configured, return the payment itself
        return payment

    # ------------------------------------------------------------------
    # Reverse (Req 6.6)
    # ------------------------------------------------------------------

    async def reverse(
        self,
        *,
        tenant_id: str,
        payment_id: str,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Reverse a payment.

        Transitions the payment to 'reversed', subtracts its amount from
        the Invoice's amount_paid_cents, re-evaluates the Invoice's state
        (paid → partial or partial → open), and emits 'payment_reversed'
        to invoice_events.

        Args:
            tenant_id: Tenant identifier for data isolation.
            payment_id: The payment to reverse.
            actor: Who triggered the reversal.

        Returns:
            The updated Payment document dict with status='reversed'.

        Raises:
            ResourceNotFoundError: If payment not found.
            ConflictError: If payment is already reversed.

        Validates: Requirements 6.6, C2, C3, C7
        """
        payment = await self.get(tenant_id=tenant_id, payment_id=payment_id)

        # Cannot reverse an already-reversed payment
        if payment.get("status") == PaymentStatus.REVERSED.value:
            raise conflict(
                "Payment is already reversed",
                error_code="PAYMENT_ALREADY_REVERSED",
                details={"payment_id": payment_id, "status": "reversed"},
            )

        now = utcnow()
        invoice_id = payment["invoice_id"]
        amount_cents = payment["amount_cents"]

        # Mark the payment as reversed
        reversal_update: Dict[str, Any] = {
            "status": PaymentStatus.REVERSED.value,
            "reversed_at": now.isoformat(),
        }
        await self._es.update_document(
            PAYMENTS_CURRENT_INDEX, payment_id, reversal_update
        )

        # Subtract from the invoice and re-evaluate state
        if self._invoice_service:
            invoice = await self._invoice_service.get(
                tenant_id=tenant_id, invoice_id=invoice_id
            )

            current_paid = invoice.get("amount_paid_cents", 0)
            total_cents = invoice.get("total_cents", 0)
            new_paid = max(0, current_paid - amount_cents)
            new_remaining = max(0, total_cents - new_paid)

            # Determine new invoice status
            current_status = invoice.get("status")
            if new_paid <= 0:
                new_status = "open"
            elif new_paid < total_cents:
                new_status = "partial"
            else:
                new_status = current_status

            # Write payment_reversed event to invoice_events (C7)
            event_id = f"ievt_{uuid4()}"
            event_doc: Dict[str, Any] = {
                "event_id": event_id,
                "invoice_id": invoice_id,
                "tenant_id": tenant_id,
                "event_type": InvoiceEventType.PAYMENT_REVERSED.value,
                "payload": {
                    "payment_id": payment_id,
                    "amount_cents": amount_cents,
                    "previous_amount_paid_cents": current_paid,
                    "new_amount_paid_cents": new_paid,
                    "previous_status": current_status,
                    "new_status": new_status,
                },
                "occurred_at": now.isoformat(),
                "actor": actor,
            }
            await self._es.index_document(
                INVOICE_EVENTS_INDEX, event_id, event_doc
            )

            # Update the invoice projection
            invoice_update: Dict[str, Any] = {
                "amount_paid_cents": new_paid,
                "remaining_cents": new_remaining,
                "status": new_status,
                "updated_at": now.isoformat(),
            }
            await self._es.update_document(
                INVOICES_CURRENT_INDEX, invoice_id, invoice_update
            )

        # Return the updated payment
        updated_payment = {**payment, **reversal_update}
        logger.info(
            "Reversed payment %s (amount=%d cents) for invoice %s tenant %s",
            payment_id,
            amount_cents,
            invoice_id,
            tenant_id,
        )
        return updated_payment

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    async def get(
        self,
        *,
        tenant_id: str,
        payment_id: str,
    ) -> Dict[str, Any]:
        """Retrieve a single Payment by ID, scoped to tenant.

        Args:
            tenant_id: Tenant identifier for data isolation.
            payment_id: The payment to retrieve.

        Returns:
            The Payment document dict.

        Raises:
            ResourceNotFoundError: If payment not found.

        Validates: Constraint C3
        """
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"payment_id": payment_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            PAYMENTS_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Payment '{payment_id}' not found",
                details={"payment_id": payment_id},
            )

        return hits[0]["_source"]

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list(
        self,
        *,
        tenant_id: str,
        invoice_id: Optional[str] = None,
        account_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> Dict[str, Any]:
        """List Payments for a tenant with cursor/limit pagination.

        Default limit is 50, max 200. Supports filtering by invoice_id
        and account_id.

        Args:
            tenant_id: Tenant identifier for data isolation.
            invoice_id: Optional filter by invoice.
            account_id: Optional filter by account.
            cursor: Pagination cursor (payment_id of last item).
            limit: Page size (default 50, max 200).

        Returns:
            Dict with 'items', 'next_cursor', and 'limit'.

        Validates: Constraint C3
        """
        # Clamp limit
        if limit < 1:
            limit = _DEFAULT_PAGE_LIMIT
        if limit > _MAX_PAGE_LIMIT:
            limit = _MAX_PAGE_LIMIT

        must_clauses: List[Dict[str, Any]] = []
        if invoice_id:
            must_clauses.append({"term": {"invoice_id": invoice_id}})
        if account_id:
            must_clauses.append({"term": {"account_id": account_id}})

        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": must_clauses if must_clauses else [{"match_all": {}}],
                }
            },
            "size": limit,
            "sort": [
                {"applied_at": {"order": "desc"}},
                {"payment_id": {"order": "asc"}},
            ],
        }

        # Cursor-based pagination using search_after
        if cursor:
            base_query["search_after"] = [cursor, cursor]

        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            PAYMENTS_CURRENT_INDEX, query, size=limit
        )

        hits = response["hits"]["hits"]
        items = [hit["_source"] for hit in hits]

        # Determine next cursor
        next_cursor: Optional[str] = None
        if hits and len(hits) == limit:
            last_sort = hits[-1].get("sort")
            if last_sort and len(last_sort) >= 2:
                next_cursor = hits[-1]["_source"]["payment_id"]

        return {
            "items": items,
            "next_cursor": next_cursor,
            "limit": limit,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _accrue_credit_balance(
        self,
        *,
        tenant_id: str,
        account_id: str,
        excess_cents: int,
        payment_id: str,
        invoice_id: str,
        actor: str = "system",
    ) -> None:
        """Accrue overpayment excess to Account.credit_balance_cents.

        Updates the account's credit_balance_cents field and emits an
        ``account_credit_balance_applied`` event to ``account_events``.

        Validates: Requirement 6.4
        """
        # Fetch current account to get existing credit_balance_cents
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
            logger.warning(
                "Cannot accrue credit balance: account %s not found for tenant %s",
                account_id,
                tenant_id,
            )
            return

        account = hits[0]["_source"]
        current_balance = account.get("credit_balance_cents", 0)
        new_balance = current_balance + excess_cents

        # Update the account's credit_balance_cents
        now = utcnow()
        account_update: Dict[str, Any] = {
            "credit_balance_cents": new_balance,
            "updated_at": now.isoformat(),
        }
        await self._es.update_document(
            ACCOUNTS_CURRENT_INDEX, account_id, account_update
        )

        # Emit account_credit_balance_applied event to account_events
        event_id = f"aevt_{uuid4()}"

        # Get next sequence number for account events
        seq_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"account_id": account_id}},
                    ]
                }
            },
            "size": 0,
            "aggs": {
                "max_seq": {"max": {"field": "sequence_number"}},
            },
        }
        seq_query = inject_tenant_filter(seq_query, tenant_id)

        seq_response = await self._es.search_documents(
            ACCOUNT_EVENTS_INDEX, seq_query, size=0
        )
        aggs = seq_response.get("aggregations", {})
        max_seq = aggs.get("max_seq", {}).get("value")
        next_seq = 1 if max_seq is None else int(max_seq) + 1

        event_doc: Dict[str, Any] = {
            "event_id": event_id,
            "account_id": account_id,
            "tenant_id": tenant_id,
            "event_type": AccountEventType.CREDIT_BALANCE_APPLIED.value,
            "payload": {
                "payment_id": payment_id,
                "invoice_id": invoice_id,
                "excess_cents": excess_cents,
                "previous_credit_balance_cents": current_balance,
                "new_credit_balance_cents": new_balance,
            },
            "occurred_at": now.isoformat(),
            "actor": actor,
            "sequence_number": next_seq,
        }
        await self._es.index_document(
            ACCOUNT_EVENTS_INDEX, event_id, event_doc
        )

        logger.info(
            "Accrued %d cents excess to account %s credit balance "
            "(was: %d, now: %d) from payment %s on invoice %s tenant %s",
            excess_cents,
            account_id,
            current_balance,
            new_balance,
            payment_id,
            invoice_id,
            tenant_id,
        )

    async def _find_payment_by_external_id(
        self,
        tenant_id: str,
        source: str,
        external_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Find an existing payment by source + external_id (idempotency lookup)."""
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"source": source}},
                        {"term": {"external_id": external_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            PAYMENTS_CURRENT_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if hits:
            return hits[0]["_source"]
        return None
