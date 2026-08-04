"""Repositories: transactional writes against the Postgres source-of-truth.

Each ``create`` / ``update`` method performs the business write AND enqueues a
transactional-outbox event in the SAME ``session_scope``, so the row and its
ES-projection event commit atomically. The outbox relay then makes ES
eventually consistent with Postgres.

These repositories are the seam the commerce services call when
``settings.commerce_dual_write_postgres`` is on. When off, the services keep
their legacy direct-to-ES path untouched.

Tenant isolation: every read and write is scoped by ``tenant_id``; no method
exposes a cross-tenant query.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from persistence import outbox
from persistence.models import (
    AccountEventORM,
    AccountORM,
    ArAgingSnapshotORM,
    AssetCertificationORM,
    CompliancePricingRuleORM,
    CustomerORM,
    DepotORM,
    DriverMasterORM,
    DunningEventORM,
    FuelOrderCurrentORM,
    IdempotencyKeyORM,
    IntakeChannelORM,
    InvoiceCounterORM,
    InvoiceEventORM,
    InvoiceLineItemORM,
    InvoiceORM,
    JobCurrentORM,
    LocationORM,
    PaymentORM,
    PriceBookORM,
    PriceProtectionContractORM,
    PricingRuleORM,
    SupplierContractORM,
    TaxExemptionORM,
    TaxJurisdictionORM,
    TenantJobPolicyORM,
    TerminalORM,
    TruckORM,
)

logger = logging.getLogger(__name__)


# Invoice columns whose values may arrive as ISO-8601 strings from the ES-shaped
# service docs but must be native datetime/date for the Postgres column types.
_INVOICE_DATETIME_FIELDS = {
    "issued_at",
    "finalized_at",
    "voided_at",
    "delivered_at",
}
_INVOICE_DATE_FIELDS = {"due_date"}


def _coerce_temporal_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``fields`` with ISO strings coerced to datetime/date.

    Only the known invoice temporal columns are touched; everything else passes
    through untouched. ``None`` and already-native values are preserved.
    """
    from datetime import date as _date, datetime as _datetime

    out: Dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, str) and value:
            if key in _INVOICE_DATETIME_FIELDS:
                try:
                    out[key] = _datetime.fromisoformat(value)
                    continue
                except ValueError:
                    pass
            elif key in _INVOICE_DATE_FIELDS:
                try:
                    out[key] = _date.fromisoformat(value)
                    continue
                except ValueError:
                    pass
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


class CustomerRepository:
    """Persistence for :class:`CustomerORM` with outbox projection."""

    async def create(self, session: AsyncSession, *, customer_id: str, tenant_id: str,
                     display_name: str, legal_name: Optional[str] = None,
                     primary_email: Optional[str] = None, tax_id: Optional[str] = None,
                     status: str = "active",
                     external_refs: Optional[Dict[str, Any]] = None,
                     metadata: Optional[Dict[str, Any]] = None) -> CustomerORM:
        row = CustomerORM(
            customer_id=customer_id,
            tenant_id=tenant_id,
            display_name=display_name,
            legal_name=legal_name,
            primary_email=primary_email,
            tax_id=tax_id,
            status=status,
            external_refs=external_refs or {},
            customer_metadata=metadata or {},
        )
        session.add(row)
        await session.flush()
        outbox.enqueue(
            session,
            aggregate_type="customer",
            aggregate_id=customer_id,
            tenant_id=tenant_id,
            event_type="created",
            row=row,
        )
        return row

    async def get(self, session: AsyncSession, tenant_id: str,
                  customer_id: str) -> Optional[CustomerORM]:
        result = await session.execute(
            select(CustomerORM).where(
                CustomerORM.tenant_id == tenant_id,
                CustomerORM.customer_id == customer_id,
            )
        )
        return result.scalar_one_or_none()

    async def update(self, session: AsyncSession, tenant_id: str, customer_id: str,
                     **fields: Any) -> Optional[CustomerORM]:
        row = await self.get(session, tenant_id, customer_id)
        if row is None:
            return None
        # ``metadata`` maps to the customer_metadata attribute (reserved name).
        if "metadata" in fields:
            fields["customer_metadata"] = fields.pop("metadata")
        for key, value in fields.items():
            setattr(row, key, value)
        await session.flush()
        outbox.enqueue(
            session,
            aggregate_type="customer",
            aggregate_id=customer_id,
            tenant_id=tenant_id,
            event_type="updated",
            row=row,
        )
        return row


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


class AccountRepository:
    """Persistence for :class:`AccountORM` with outbox projection."""

    async def create(self, session: AsyncSession, *, account_id: str, tenant_id: str,
                     customer_id: str, display_name: str, status: str = "active",
                     credit_limit_cents: int = 0, net_terms_days: int = 30,
                     tier: str = "default",
                     billing_address: Optional[Dict[str, Any]] = None,
                     payment_method_preference: str = "invoice",
                     external_refs: Optional[Dict[str, Any]] = None) -> AccountORM:
        row = AccountORM(
            account_id=account_id,
            tenant_id=tenant_id,
            customer_id=customer_id,
            display_name=display_name,
            status=status,
            credit_limit_cents=credit_limit_cents,
            open_balance_cents=0,
            available_credit_cents=credit_limit_cents,
            credit_balance_cents=0,
            credit_state="ok",
            net_terms_days=net_terms_days,
            tier=tier,
            billing_address=billing_address,
            payment_method_preference=payment_method_preference,
            external_refs=external_refs or {},
        )
        session.add(row)
        await session.flush()
        outbox.enqueue(
            session,
            aggregate_type="account",
            aggregate_id=account_id,
            tenant_id=tenant_id,
            event_type="created",
            row=row,
        )
        return row

    async def get(self, session: AsyncSession, tenant_id: str,
                  account_id: str) -> Optional[AccountORM]:
        result = await session.execute(
            select(AccountORM).where(
                AccountORM.tenant_id == tenant_id,
                AccountORM.account_id == account_id,
            )
        )
        return result.scalar_one_or_none()

    async def set_fields(self, session: AsyncSession, tenant_id: str, account_id: str,
                         *, event_type: str = "updated", **fields: Any
                         ) -> Optional[AccountORM]:
        """Apply explicit field values to an account and enqueue a projection.

        Used by the dual-write bridge to mirror the service's already-computed
        values (balance, available credit, credit state) so the Postgres row
        and the ES doc cannot disagree. Unknown keys are ignored defensively.
        """
        row = await self.get(session, tenant_id, account_id)
        if row is None:
            return None
        for key, value in fields.items():
            if hasattr(row, key):
                setattr(row, key, value)
        await session.flush()
        outbox.enqueue(
            session,
            aggregate_type="account",
            aggregate_id=account_id,
            tenant_id=tenant_id,
            event_type=event_type,
            row=row,
        )
        return row

    async def get_for_update(self, session: AsyncSession, tenant_id: str,
                             account_id: str) -> Optional[AccountORM]:
        """Fetch an account with a row lock for safe credit/balance mutation.

        ``SELECT ... FOR UPDATE`` serialises concurrent balance changes — the
        exact guarantee ES could not provide. SQLite ignores the lock clause
        (single-writer anyway), so tests behave correctly too.
        """
        stmt = select(AccountORM).where(
            AccountORM.tenant_id == tenant_id,
            AccountORM.account_id == account_id,
        )
        # with_for_update is a no-op on SQLite but real on PostgreSQL.
        stmt = stmt.with_for_update()
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def apply_balance_delta(self, session: AsyncSession, tenant_id: str,
                                  account_id: str, *, open_balance_delta_cents: int
                                  ) -> Optional[AccountORM]:
        """Atomically adjust open balance and recompute available credit."""
        row = await self.get_for_update(session, tenant_id, account_id)
        if row is None:
            return None
        row.open_balance_cents += open_balance_delta_cents
        row.available_credit_cents = row.credit_limit_cents - row.open_balance_cents
        await session.flush()
        outbox.enqueue(
            session,
            aggregate_type="account",
            aggregate_id=account_id,
            tenant_id=tenant_id,
            event_type="balance_changed",
            row=row,
        )
        return row


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------


class InvoiceRepository:
    """Persistence for :class:`InvoiceORM` (+ line items) with outbox projection."""

    async def create(self, session: AsyncSession, *, invoice_id: str, tenant_id: str,
                     customer_id: str, account_id: str,
                     line_items: List[Dict[str, Any]],
                     order_id: Optional[str] = None,
                     pod_id: Optional[str] = None,
                     delivered_at: Any = None,
                     delivery_result: Optional[Dict[str, Any]] = None,
                     invoice_number: Optional[str] = None,
                     status: str = "draft", tax_cents: int = 0,
                     subtotal_cents: Optional[int] = None,
                     total_cents: Optional[int] = None,
                     amount_paid_cents: int = 0,
                     remaining_cents: Optional[int] = None,
                     due_date: Any = None,
                     external_refs: Optional[Dict[str, Any]] = None) -> InvoiceORM:
        # When the caller (e.g. the dual-write bridge mirroring an already-built
        # invoice) supplies authoritative totals, trust them — the service may
        # have computed tax via a TaxEngine that the repo cannot reproduce.
        # Otherwise derive subtotal/total from the line items.
        subtotal = subtotal_cents if subtotal_cents is not None else sum(
            int(li["subtotal_cents"]) for li in line_items
        )
        total = total_cents if total_cents is not None else subtotal + tax_cents
        remaining = remaining_cents if remaining_cents is not None else total - amount_paid_cents
        _due = _coerce_temporal_fields({"due_date": due_date}).get("due_date") if due_date else None
        _delivered_at = (
            _coerce_temporal_fields({"delivered_at": delivered_at}).get(
                "delivered_at"
            )
            if delivered_at
            else None
        )
        row = InvoiceORM(
            invoice_id=invoice_id,
            tenant_id=tenant_id,
            customer_id=customer_id,
            account_id=account_id,
            order_id=order_id,
            pod_id=pod_id,
            delivered_at=_delivered_at,
            delivery_result=delivery_result,
            invoice_number=invoice_number,
            status=status,
            subtotal_cents=subtotal,
            tax_cents=tax_cents,
            total_cents=total,
            amount_paid_cents=amount_paid_cents,
            remaining_cents=remaining,
            due_date=_due,
            external_refs=external_refs or {},
        )
        for position, li in enumerate(line_items):
            row.line_items.append(
                InvoiceLineItemORM(
                    line_id=li["line_id"],
                    position=position,
                    product_code=li["product_code"],
                    quantity_gallons=float(li["quantity_gallons"]),
                    unit_price_cents=int(li["unit_price_cents"]),
                    unit_price_micros=(
                        int(li["unit_price_micros"])
                        if li.get("unit_price_micros") is not None
                        else None
                    ),
                    subtotal_cents=int(li["subtotal_cents"]),
                )
            )
        session.add(row)
        await session.flush()
        outbox.enqueue(
            session,
            aggregate_type="invoice",
            aggregate_id=invoice_id,
            tenant_id=tenant_id,
            event_type="created",
            row=row,
        )
        return row

    async def get(self, session: AsyncSession, tenant_id: str,
                  invoice_id: str) -> Optional[InvoiceORM]:
        result = await session.execute(
            select(InvoiceORM)
            .where(
                InvoiceORM.tenant_id == tenant_id,
                InvoiceORM.invoice_id == invoice_id,
            )
            # Eager-load line items so the outbox projector (which iterates
            # row.line_items) never triggers a lazy load in async context.
            .options(selectinload(InvoiceORM.line_items))
        )
        return result.scalar_one_or_none()

    async def record_payment_applied(self, session: AsyncSession, tenant_id: str,
                                     invoice_id: str, *, amount_cents: int
                                     ) -> Optional[InvoiceORM]:
        """Apply a payment amount and advance the invoice status machine."""
        row = await self.get(session, tenant_id, invoice_id)
        if row is None:
            return None
        row.amount_paid_cents += amount_cents
        row.remaining_cents = max(row.total_cents - row.amount_paid_cents, 0)
        if row.remaining_cents == 0:
            row.status = "paid"
        elif row.amount_paid_cents > 0:
            row.status = "partial"
        await session.flush()
        outbox.enqueue(
            session,
            aggregate_type="invoice",
            aggregate_id=invoice_id,
            tenant_id=tenant_id,
            event_type="payment_applied",
            row=row,
        )
        return row

    async def set_fields(self, session: AsyncSession, tenant_id: str, invoice_id: str,
                         *, event_type: str = "updated", **fields: Any
                         ) -> Optional[InvoiceORM]:
        """Apply explicit field values to an invoice and enqueue a projection.

        Used by the dual-write bridge to mirror the service's already-computed
        status-transition values (status, amount_paid_cents, remaining_cents,
        issued_at, voided_at, invoice_number, …) so the Postgres row and the ES
        doc cannot disagree. ISO-8601 strings for datetime/date columns are
        coerced to native objects so the write is valid on PostgreSQL (which,
        unlike SQLite, rejects string-to-timestamp). Unknown keys are ignored.
        """
        row = await self.get(session, tenant_id, invoice_id)
        if row is None:
            return None
        coerced = _coerce_temporal_fields(fields)
        for key, value in coerced.items():
            if hasattr(row, key):
                setattr(row, key, value)
        await session.flush()
        outbox.enqueue(
            session,
            aggregate_type="invoice",
            aggregate_id=invoice_id,
            tenant_id=tenant_id,
            event_type=event_type,
            row=row,
        )
        return row

    async def allocate_number(self, session: AsyncSession, tenant_id: str) -> int:
        """Allocate the next monotonic invoice number for a tenant.

        Increments a per-tenant counter row under a row lock, in the caller's
        transaction. Because the allocation and the invoice's finalize update
        commit together, a number is never skipped on rollback nor issued
        twice under concurrency — the guarantee the Redis/ES numbering path
        could only approximate.

        Returns the allocated number (starts at 1).
        """
        stmt = (
            select(InvoiceCounterORM)
            .where(InvoiceCounterORM.tenant_id == tenant_id)
            .with_for_update()
        )
        counter = (await session.execute(stmt)).scalar_one_or_none()
        if counter is None:
            # First invoice for this tenant: seed at 1, next becomes 2.
            counter = InvoiceCounterORM(tenant_id=tenant_id, next_seq=2)
            session.add(counter)
            await session.flush()
            return 1
        allocated = counter.next_seq
        counter.next_seq = allocated + 1
        await session.flush()
        return allocated


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


class DuplicatePaymentError(Exception):
    """Raised when a payment with the same (tenant, source, external_id) exists.

    Signals an idempotent re-delivery: the caller should fetch and return the
    existing payment rather than creating a second row.
    """

    def __init__(self, tenant_id: str, source: str, external_id: Optional[str]) -> None:
        self.tenant_id = tenant_id
        self.source = source
        self.external_id = external_id
        super().__init__(
            f"Duplicate payment for tenant={tenant_id} source={source} "
            f"external_id={external_id}"
        )


class PaymentRepository:
    """Persistence for :class:`PaymentORM` with outbox projection."""

    async def get(self, session: AsyncSession, tenant_id: str,
                  payment_id: str) -> Optional[PaymentORM]:
        result = await session.execute(
            select(PaymentORM).where(
                PaymentORM.tenant_id == tenant_id,
                PaymentORM.payment_id == payment_id,
            )
        )
        return result.scalar_one_or_none()

    async def find_by_external_id(self, session: AsyncSession, tenant_id: str,
                                  source: str, external_id: str
                                  ) -> Optional[PaymentORM]:
        """Look up a payment by its idempotency tuple (tenant, source, external_id)."""
        result = await session.execute(
            select(PaymentORM).where(
                PaymentORM.tenant_id == tenant_id,
                PaymentORM.source == source,
                PaymentORM.external_id == external_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, session: AsyncSession, *, payment_id: str, tenant_id: str,
                     invoice_id: str, account_id: str, amount_cents: int,
                     source: str, method: str, external_id: Optional[str] = None,
                     reference: Optional[str] = None,
                     status: str = "applied") -> PaymentORM:
        row = PaymentORM(
            payment_id=payment_id,
            tenant_id=tenant_id,
            invoice_id=invoice_id,
            account_id=account_id,
            amount_cents=amount_cents,
            source=source,
            method=method,
            external_id=external_id,
            reference=reference,
            status=status,
        )
        session.add(row)
        await session.flush()
        outbox.enqueue(
            session,
            aggregate_type="payment",
            aggregate_id=payment_id,
            tenant_id=tenant_id,
            event_type="created",
            row=row,
        )
        return row

    async def create_idempotent(self, session: AsyncSession, **kwargs: Any) -> PaymentORM:
        """Create a payment, mapping a unique-constraint violation to a clear signal.

        Pre-checks the idempotency tuple and lets the DB unique constraint be
        the final arbiter under concurrency: if two requests race, exactly one
        INSERT wins and the loser's flush raises ``IntegrityError`` on
        ``uq_payment_tenant_source_external``, which we translate into
        :class:`DuplicatePaymentError` so the caller can return the existing
        payment idempotently.
        """
        tenant_id = kwargs["tenant_id"]
        source = kwargs["source"]
        external_id = kwargs.get("external_id")

        if external_id is not None:
            existing = await self.find_by_external_id(
                session, tenant_id, source, external_id
            )
            if existing is not None:
                raise DuplicatePaymentError(tenant_id, source, external_id)
        try:
            return await self.create(session, **kwargs)
        except IntegrityError as exc:
            # Concurrent insert won the race on the unique constraint.
            if "uq_payment_tenant_source_external" in str(exc.orig):
                raise DuplicatePaymentError(tenant_id, source, external_id) from exc
            raise

    async def set_fields(self, session: AsyncSession, tenant_id: str, payment_id: str,
                         *, event_type: str = "updated", **fields: Any
                         ) -> Optional[PaymentORM]:
        """Apply explicit field values to a payment and enqueue a projection.

        Used to mirror the service's reversal transition (status, reversed_at).
        ``reversed_at`` arriving as an ISO string is coerced to a datetime so
        the write is valid on PostgreSQL.
        """
        from datetime import datetime as _datetime

        row = await self.get(session, tenant_id, payment_id)
        if row is None:
            return None
        for key, value in fields.items():
            if key == "reversed_at" and isinstance(value, str) and value:
                try:
                    value = _datetime.fromisoformat(value)
                except ValueError:
                    pass
            if hasattr(row, key):
                setattr(row, key, value)
        await session.flush()
        outbox.enqueue(
            session,
            aggregate_type="payment",
            aggregate_id=payment_id,
            tenant_id=tenant_id,
            event_type=event_type,
            row=row,
        )
        return row


class IdempotencyRepository:
    """Concurrency-safe idempotency-key store backed by a UNIQUE PK.

    Unlike the ES version, a duplicate ``(tenant_id, idempotency_key)`` insert
    fails at the database level, so two concurrent requests with the same key
    cannot both be admitted.
    """

    async def get(self, session: AsyncSession, tenant_id: str,
                  idempotency_key: str) -> Optional[IdempotencyKeyORM]:
        result = await session.execute(
            select(IdempotencyKeyORM).where(
                IdempotencyKeyORM.tenant_id == tenant_id,
                IdempotencyKeyORM.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    async def put(self, session: AsyncSession, *, tenant_id: str,
                  idempotency_key: str, request_fingerprint: Optional[str] = None,
                  response_status: Optional[int] = None,
                  response_body: Optional[Dict[str, Any]] = None,
                  expires_at=None) -> IdempotencyKeyORM:
        row = IdempotencyKeyORM(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            response_status=response_status,
            response_body=response_body,
            expires_at=expires_at,
        )
        session.add(row)
        await session.flush()
        return row


# ---------------------------------------------------------------------------
# Pricing config: price books + rules
# ---------------------------------------------------------------------------


class PriceBookRepository:
    """Persistence for :class:`PriceBookORM` with outbox projection."""

    async def get(self, session: AsyncSession, tenant_id: str,
                  price_book_id: str) -> Optional[PriceBookORM]:
        result = await session.execute(
            select(PriceBookORM).where(
                PriceBookORM.tenant_id == tenant_id,
                PriceBookORM.price_book_id == price_book_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(self, session: AsyncSession, *, price_book_id: str, tenant_id: str,
                     name: str, description: Optional[str] = None,
                     status: str = "draft", rule_count: int = 0) -> PriceBookORM:
        row = PriceBookORM(
            price_book_id=price_book_id, tenant_id=tenant_id, name=name,
            description=description, status=status, rule_count=rule_count,
        )
        session.add(row)
        await session.flush()
        outbox.enqueue(session, aggregate_type="price_book", aggregate_id=price_book_id,
                       tenant_id=tenant_id, event_type="created", row=row)
        return row

    async def set_fields(self, session: AsyncSession, tenant_id: str, price_book_id: str,
                         *, event_type: str = "updated", **fields: Any
                         ) -> Optional[PriceBookORM]:
        row = await self.get(session, tenant_id, price_book_id)
        if row is None:
            return None
        for key, value in fields.items():
            if hasattr(row, key):
                setattr(row, key, value)
        await session.flush()
        outbox.enqueue(session, aggregate_type="price_book", aggregate_id=price_book_id,
                       tenant_id=tenant_id, event_type=event_type, row=row)
        return row


class PricingRuleRepository:
    """Persistence for :class:`PricingRuleORM` with outbox projection."""

    @staticmethod
    def _coerce_effective(fields: Dict[str, Any]) -> Dict[str, Any]:
        from datetime import datetime as _dt
        out = dict(fields)
        for key in ("effective_from", "effective_to"):
            v = out.get(key)
            if isinstance(v, str) and v:
                try:
                    out[key] = _dt.fromisoformat(v)
                except ValueError:
                    pass
        return out

    async def upsert(self, session: AsyncSession, *, rule: Dict[str, Any]) -> PricingRuleORM:
        """Insert or update a pricing rule from the service's rule dict."""
        rule_id = rule["rule_id"]
        tenant_id = rule["tenant_id"]
        existing = (
            await session.execute(
                select(PricingRuleORM).where(PricingRuleORM.rule_id == rule_id)
            )
        ).scalar_one_or_none()
        vals = self._coerce_effective(rule)
        if existing is None:
            row = PricingRuleORM(
                rule_id=rule_id,
                price_book_id=vals["price_book_id"],
                tenant_id=tenant_id,
                product_code=vals["product_code"],
                scope_type=vals["scope_type"],
                scope_value=vals["scope_value"],
                effective_from=vals.get("effective_from"),
                effective_to=vals.get("effective_to"),
                min_quantity_gallons=vals.get("min_quantity_gallons"),
                unit_price_cents=int(vals["unit_price_cents"]),
                unit_price_micros=(
                    int(vals["unit_price_micros"])
                    if vals.get("unit_price_micros") is not None
                    else None
                ),
            )
            session.add(row)
        else:
            row = existing
            for key in ("price_book_id", "product_code", "scope_type", "scope_value",
                        "effective_from", "effective_to", "min_quantity_gallons"):
                if key in vals:
                    setattr(row, key, vals[key])
            if "unit_price_cents" in vals:
                row.unit_price_cents = int(vals["unit_price_cents"])
            if "unit_price_micros" in vals:
                row.unit_price_micros = (
                    int(vals["unit_price_micros"])
                    if vals["unit_price_micros"] is not None
                    else None
                )
        await session.flush()
        outbox.enqueue(session, aggregate_type="pricing_rule", aggregate_id=rule_id,
                       tenant_id=tenant_id, event_type="upserted", row=row)
        return row

    async def delete(self, session: AsyncSession, tenant_id: str, rule_id: str) -> bool:
        row = (
            await session.execute(
                select(PricingRuleORM).where(
                    PricingRuleORM.tenant_id == tenant_id,
                    PricingRuleORM.rule_id == rule_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        await session.delete(row)
        await session.flush()
        return True


# ---------------------------------------------------------------------------
# Event ledgers (append-only)
# ---------------------------------------------------------------------------


class InvoiceEventRepository:
    """Append-only invoice ledger writes with outbox projection."""

    async def append(self, session: AsyncSession, *, doc: Dict[str, Any]) -> InvoiceEventORM:
        from datetime import datetime as _dt
        occurred = doc.get("occurred_at")
        if isinstance(occurred, str):
            try:
                occurred = _dt.fromisoformat(occurred)
            except ValueError:
                occurred = None
        row = InvoiceEventORM(
            event_id=doc["event_id"],
            invoice_id=doc["invoice_id"],
            tenant_id=doc["tenant_id"],
            event_type=doc["event_type"],
            payload=doc.get("payload") or {},
            occurred_at=occurred,
            actor=doc.get("actor", "system"),
            sequence_number=int(doc["sequence_number"]),
        )
        session.add(row)
        await session.flush()
        outbox.enqueue(session, aggregate_type="invoice_event", aggregate_id=row.event_id,
                       tenant_id=row.tenant_id, event_type="appended", row=row)
        return row


class AccountEventRepository:
    """Append-only account audit writes with outbox projection."""

    async def append(self, session: AsyncSession, *, doc: Dict[str, Any]) -> AccountEventORM:
        from datetime import datetime as _dt
        occurred = doc.get("occurred_at")
        if isinstance(occurred, str):
            try:
                occurred = _dt.fromisoformat(occurred)
            except ValueError:
                occurred = None
        row = AccountEventORM(
            event_id=doc["event_id"],
            account_id=doc["account_id"],
            tenant_id=doc["tenant_id"],
            event_type=doc["event_type"],
            payload=doc.get("payload") or {},
            occurred_at=occurred,
            actor=doc.get("actor", "system"),
            sequence_number=int(doc["sequence_number"]),
        )
        session.add(row)
        await session.flush()
        outbox.enqueue(session, aggregate_type="account_event", aggregate_id=row.event_id,
                       tenant_id=row.tenant_id, event_type="appended", row=row)
        return row


class DunningEventRepository:
    """Dunning event writes with outbox projection."""

    @staticmethod
    def _dt(value):
        from datetime import datetime as _dt
        if isinstance(value, str) and value:
            try:
                return _dt.fromisoformat(value)
            except ValueError:
                return None
        return value

    async def create(self, session: AsyncSession, *, doc: Dict[str, Any]) -> DunningEventORM:
        row = DunningEventORM(
            event_id=doc["event_id"],
            invoice_id=doc["invoice_id"],
            account_id=doc["account_id"],
            tenant_id=doc["tenant_id"],
            threshold_days=doc.get("threshold_days"),
            template_key=doc.get("template_key"),
            queued_at=self._dt(doc.get("queued_at")),
            cancelled_at=self._dt(doc.get("cancelled_at")),
            cancellation_reason=doc.get("cancellation_reason"),
        )
        session.add(row)
        await session.flush()
        outbox.enqueue(session, aggregate_type="dunning_event", aggregate_id=row.event_id,
                       tenant_id=row.tenant_id, event_type="created", row=row)
        return row

    async def set_fields(self, session: AsyncSession, tenant_id: str, event_id: str,
                         **fields: Any) -> Optional[DunningEventORM]:
        row = (
            await session.execute(
                select(DunningEventORM).where(
                    DunningEventORM.tenant_id == tenant_id,
                    DunningEventORM.event_id == event_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        for key, value in fields.items():
            if key in ("queued_at", "cancelled_at"):
                value = self._dt(value)
            if hasattr(row, key):
                setattr(row, key, value)
        await session.flush()
        outbox.enqueue(session, aggregate_type="dunning_event", aggregate_id=event_id,
                       tenant_id=tenant_id, event_type="updated", row=row)
        return row


# ---------------------------------------------------------------------------
# AR aging snapshots
# ---------------------------------------------------------------------------


class ArAgingSnapshotRepository:
    """Daily AR aging snapshot writes with outbox projection (idempotent upsert)."""

    @staticmethod
    def _date(value):
        from datetime import date as _date, datetime as _dt
        if isinstance(value, str) and value:
            try:
                return _date.fromisoformat(value[:10])
            except ValueError:
                return None
        return value

    async def upsert(self, session: AsyncSession, *, doc: Dict[str, Any]) -> ArAgingSnapshotORM:
        snapshot_id = doc["snapshot_id"]
        existing = await session.get(ArAgingSnapshotORM, snapshot_id)
        fields = dict(
            tenant_id=doc["tenant_id"],
            snapshot_date=self._date(doc.get("snapshot_date")),
            total_open_cents=doc.get("total_open_cents", 0),
            bucket_0_30_cents=doc.get("bucket_0_30_cents", 0),
            bucket_31_60_cents=doc.get("bucket_31_60_cents", 0),
            bucket_61_90_cents=doc.get("bucket_61_90_cents", 0),
            bucket_90_plus_cents=doc.get("bucket_90_plus_cents", 0),
            account_count_with_balance=doc.get("account_count_with_balance", 0),
        )
        if existing is None:
            row = ArAgingSnapshotORM(snapshot_id=snapshot_id, **fields)
            session.add(row)
        else:
            row = existing
            for key, value in fields.items():
                setattr(row, key, value)
        await session.flush()
        outbox.enqueue(session, aggregate_type="ar_aging_snapshot", aggregate_id=snapshot_id,
                       tenant_id=row.tenant_id, event_type="snapshotted", row=row)
        return row


# ---------------------------------------------------------------------------
# Compliance config (hybrid document tables)
# ---------------------------------------------------------------------------


class ComplianceConfigRepository:
    """Generic upsert/delete for the hybrid compliance-config tables.

    Each table stores typed identity/index columns plus a ``document`` JSON
    column holding the full ES doc. The projector returns ``document``
    verbatim, so the ES projection is byte-identical. One repository instance
    is bound to a specific (aggregate_type, ORM model, id field) triple.
    """

    # aggregate_type -> (ORM model, primary-key field, extra typed columns
    # to lift out of the document for indexing/constraints)
    _SPECS = {
        "tax_jurisdiction": (
            TaxJurisdictionORM, "jurisdiction_id",
            ("fips_code", "tax_type", "status"),
        ),
        "tax_exemption": (
            TaxExemptionORM, "exemption_id",
            ("customer_id", "certificate_number", "status"),
        ),
        "price_protection_contract": (
            PriceProtectionContractORM, "contract_id",
            ("customer_id", "product_code", "status", "version"),
        ),
        "compliance_pricing_rule": (
            CompliancePricingRuleORM, "rule_id",
            ("customer_id", "product_code", "strategy", "status"),
        ),
        "supplier_contract": (
            SupplierContractORM, "contract_id",
            ("supplier_name", "product_code", "status"),
        ),
    }

    def __init__(self, aggregate_type: str) -> None:
        if aggregate_type not in self._SPECS:
            raise ValueError(f"Unknown compliance aggregate_type: {aggregate_type!r}")
        self.aggregate_type = aggregate_type
        self.model, self.pk_field, self.typed_cols = self._SPECS[aggregate_type]

    async def get(self, session: AsyncSession, tenant_id: str, doc_id: str):
        row = await session.get(self.model, doc_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def upsert(self, session: AsyncSession, *, doc: Dict[str, Any]):
        """Insert or update a config row from its full ES document."""
        doc_id = doc[self.pk_field]
        tenant_id = doc["tenant_id"]
        existing = await session.get(self.model, doc_id)
        typed = {col: doc.get(col) for col in self.typed_cols if col in doc}
        # version defaults to 0 when the source doc omits it.
        if "version" in self.typed_cols and typed.get("version") is None:
            typed["version"] = doc.get("version", 0) or 0
        if existing is None:
            row = self.model(
                **{self.pk_field: doc_id},
                tenant_id=tenant_id,
                document=dict(doc),
                **typed,
            )
            session.add(row)
        else:
            row = existing
            row.document = dict(doc)
            for col, value in typed.items():
                setattr(row, col, value)
        await session.flush()
        outbox.enqueue(session, aggregate_type=self.aggregate_type, aggregate_id=doc_id,
                       tenant_id=tenant_id, event_type="upserted", row=row)
        return row

    async def delete(self, session: AsyncSession, tenant_id: str, doc_id: str) -> bool:
        row = await self.get(session, tenant_id, doc_id)
        if row is None:
            return False
        await session.delete(row)
        await session.flush()
        return True


# ---------------------------------------------------------------------------
# Orders / jobs current-state (hybrid document tables)
# ---------------------------------------------------------------------------


class CurrentStateRepository:
    """Generic upsert for the orders/jobs current-state hybrid tables.

    Like :class:`ComplianceConfigRepository` but with an optional
    **stale-event guard**: aggregates that carry ``last_event_timestamp``
    (orders, jobs) reject an upsert whose incoming timestamp is
    older-or-equal to the stored one — mirroring the ES scripted-upsert
    out-of-order protection so the Postgres row and ES projection converge to
    the same final state regardless of event delivery order.
    """

    # aggregate_type -> (ORM model, pk field, typed columns, has_last_event_ts)
    #
    # The typed columns MUST cover every mirror column the ORM model declares
    # (everything but the pk, tenant_id, document and the two timestamps).
    # ``HybridReadRepository.list`` resolves a filter key to a typed column when
    # the model has one, so a declared-but-never-written column turns that
    # filter into a silent empty result rather than an error. Six aggregates
    # here were missing one, and ``depot``'s missing ``status`` is what made
    # every ``list_for_tenant(status="active")`` return nothing under
    # COMMERCE_READ_FROM_POSTGRES — which is how route planning lost its depot.
    # ``test_typed_mirror_columns_are_populated.py`` pins the invariant.
    _SPECS = {
        "fuel_order": (
            FuelOrderCurrentORM, "order_id",
            ("customer_id", "assigned_driver_id", "assigned_asset_id", "status",
             "last_event_timestamp"),
            True,
        ),
        "job": (
            JobCurrentORM, "job_id",
            ("asset_id", "status", "last_event_timestamp"),
            True,
        ),
        # ``shipment`` was retired with the ``shipments_current`` table (rev 0007).
        "tenant_job_policy": (
            TenantJobPolicyORM, "policy_id", ("status",), False,
        ),
        # Master data (no stale-event guard).
        "driver": (DriverMasterORM, "driver_id", ("cdl_number", "status"), False),
        "depot": (DepotORM, "depot_id", ("is_default", "status"), False),
        "terminal": (TerminalORM, "terminal_id", ("status",), False),
        "asset_certification": (
            AssetCertificationORM, "cert_id", ("asset_id", "status"), False,
        ),
        "intake_channel": (IntakeChannelORM, "channel_id", ("status",), False),
        "truck": (TruckORM, "truck_id", ("status",), False),
        "location": (LocationORM, "location_id", ("status",), False),
    }

    def __init__(self, aggregate_type: str) -> None:
        if aggregate_type not in self._SPECS:
            raise ValueError(f"Unknown current-state aggregate_type: {aggregate_type!r}")
        self.aggregate_type = aggregate_type
        self.model, self.pk_field, self.typed_cols, self.has_event_ts = (
            self._SPECS[aggregate_type]
        )

    async def get(self, session: AsyncSession, tenant_id: str, doc_id: str):
        row = await session.get(self.model, doc_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def upsert(self, session: AsyncSession, *, doc: Dict[str, Any],
                     doc_id: Optional[str] = None) -> Optional[Any]:
        """Insert/update a current-state row from its full ES document.

        Returns the row, or ``None`` when a stale-event guard discards the
        write (incoming ``last_event_timestamp`` <= stored).
        """
        resolved_id = doc_id or doc.get(self.pk_field)
        if resolved_id is None and self.aggregate_type == "tenant_job_policy":
            # tenant_job_policies is keyed by tenant_id (one per tenant).
            resolved_id = doc["tenant_id"]
        # Legacy trucks/locations docs may omit tenant_id; default it so the
        # NOT NULL column is satisfied and tenant-scoped reads still work.
        tenant_id = doc.get("tenant_id") or "unknown"
        existing = await session.get(self.model, resolved_id)

        # Stale-event guard.
        if self.has_event_ts and existing is not None:
            incoming = doc.get("last_event_timestamp")
            stored = existing.last_event_timestamp
            if incoming is not None and stored is not None and incoming <= stored:
                return None

        typed = {c: doc.get(c) for c in self.typed_cols if c in doc}
        if existing is None:
            row = self.model(
                **{self.pk_field: resolved_id},
                tenant_id=tenant_id,
                document=dict(doc),
                **typed,
            )
            session.add(row)
        else:
            row = existing
            row.document = dict(doc)
            for col, value in typed.items():
                setattr(row, col, value)
        await session.flush()
        outbox.enqueue(session, aggregate_type=self.aggregate_type, aggregate_id=resolved_id,
                       tenant_id=tenant_id, event_type="upserted", row=row)
        return row

    async def set_fields(self, session: AsyncSession, tenant_id: str, doc_id: str,
                         **fields: Any) -> Optional[Any]:
        """Merge partial ``fields`` into a current-state row's document.

        For status-transition writes that apply an ES partial update (e.g. the
        asset-certification expiry sweep marking a cert ``expiring_soon`` /
        ``expired`` / ``superseded``). The fields are merged into the verbatim
        ``document`` JSON and lifted into any matching typed column, so the PG
        source-of-truth and the ES projection converge. Tenant-scoped; returns
        the row, or ``None`` when the row is absent under this tenant.
        """
        row = await session.get(self.model, doc_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        merged = dict(row.document or {})
        merged.update(fields)
        row.document = merged
        for col in self.typed_cols:
            if col in fields:
                setattr(row, col, fields[col])
        await session.flush()
        outbox.enqueue(session, aggregate_type=self.aggregate_type, aggregate_id=doc_id,
                       tenant_id=tenant_id, event_type="upserted", row=row)
        return row

    async def delete(self, session: AsyncSession, tenant_id: str, doc_id: str) -> bool:
        """Delete a current-state row (tenant-scoped). Returns True if removed.

        Removes the authoritative Postgres row so a read-cutover deployment
        does not keep serving a deleted aggregate. We do NOT enqueue an outbox
        tombstone here: the ES projection is deleted directly by the caller
        (the repository's ``delete_document``) during the soak, and once the ES
        index is dropped the projection no longer exists to reconcile.
        """
        row = await session.get(self.model, doc_id)
        if row is None:
            return False
        if not getattr(self, "_tenant_optional", False) and row.tenant_id != tenant_id:
            return False
        await session.delete(row)
        await session.flush()
        return True


# ---------------------------------------------------------------------------
# Shared spec lookup
# ---------------------------------------------------------------------------


def hybrid_spec_for(aggregate_type: str) -> Tuple[Any, str, Tuple[str, ...]]:
    """Return ``(ORM model, pk field, typed columns)`` for a hybrid aggregate.

    One lookup across both hybrid writers so callers outside the repositories
    — notably :mod:`persistence.backfill` — cannot drift from the spec the live
    write path uses. Backfill previously repeated the model name, pk field and
    typed-column tuple for every aggregate, and depot's copy went stale in both
    places at once: neither wrote the ``status`` mirror column that
    ``HybridReadRepository.list`` filters on.

    Raises:
        ValueError: when ``aggregate_type`` belongs to neither writer.
    """
    if aggregate_type in CurrentStateRepository._SPECS:
        model, pk_field, typed_cols, _has_event_ts = (
            CurrentStateRepository._SPECS[aggregate_type]
        )
        return model, pk_field, typed_cols
    if aggregate_type in ComplianceConfigRepository._SPECS:
        return ComplianceConfigRepository._SPECS[aggregate_type]
    raise ValueError(f"Unknown hybrid aggregate_type: {aggregate_type!r}")
