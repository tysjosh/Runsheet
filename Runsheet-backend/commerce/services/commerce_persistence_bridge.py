"""Bridge from commerce services to the Postgres source-of-truth.

This module is the seam that lets the existing commerce services adopt the
PostgreSQL source-of-truth incrementally and reversibly. It is invoked ONLY
when both of the following hold:

    settings.database_url is set                  (persistence layer active)
    settings.commerce_dual_write_postgres is True (dual-write opted in)

During the dual-write soak phase the commerce services keep writing to
Elasticsearch directly (so the read path is unchanged and read-after-write is
immediate), and ADDITIONALLY persist to Postgres + the transactional outbox
here. The outbox relay then reconciles the ES projection. Once parity is
verified, reads can be cut over to Postgres and the direct ES write removed.

To avoid a Postgres hiccup taking down a request during early rollout, write
failures are logged and swallowed (best-effort). This is intentional for the
soak phase: ES remains authoritative until cutover. Flip behavior by raising
in :func:`_run` if you want strict dual-write.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    """True when the persistence layer is active AND dual-write is opted in."""
    from config.settings import get_settings
    from persistence.database import is_persistence_enabled

    if not is_persistence_enabled():
        return False
    return bool(get_settings().commerce_dual_write_postgres)


def _payments_authoritative() -> bool:
    """True when payments should be written to Postgres FIRST (authoritative).

    Requires the persistence layer to be active. Independent of the dual-write
    flag so payments can be promoted to authoritative on its own schedule.
    """
    from config.settings import get_settings
    from persistence.database import is_persistence_enabled

    if not is_persistence_enabled():
        return False
    return bool(get_settings().commerce_payments_authoritative)


def read_from_postgres() -> bool:
    """True when commerce reads should be served from Postgres (read-cutover).

    Requires the persistence layer to be active. When False, callers keep their
    ES read path (legacy). This is the gate for Phase 4 of the migration.
    """
    from config.settings import get_settings
    from persistence.database import is_persistence_enabled

    if not is_persistence_enabled():
        return False
    return bool(get_settings().commerce_read_from_postgres)


async def mirror_customer_create(doc: Dict[str, Any]) -> None:
    """Best-effort: persist a created customer to Postgres + outbox.

    ``doc`` is the same document the service indexed into ``customers_current``;
    we re-use it so the Postgres row and ES doc cannot disagree on field values.
    """
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import CustomerRepository

    repo = CustomerRepository()
    try:
        async with session_scope() as session:
            existing = await repo.get(session, doc["tenant_id"], doc["customer_id"])
            if existing is not None:
                return  # already mirrored (idempotent)
            await repo.create(
                session,
                customer_id=doc["customer_id"],
                tenant_id=doc["tenant_id"],
                display_name=doc["display_name"],
                legal_name=doc.get("legal_name"),
                primary_email=doc.get("primary_email"),
                tax_id=doc.get("tax_id"),
                status=doc.get("status", "active"),
                external_refs=doc.get("external_refs") or {},
                metadata=doc.get("metadata") or {},
            )
    except Exception:  # noqa: BLE001 — best-effort during soak; ES is authoritative
        logger.exception(
            "Postgres dual-write failed for customer %s (tenant %s); "
            "ES write already succeeded, continuing",
            doc.get("customer_id"), doc.get("tenant_id"),
        )


async def mirror_customer_update(
    tenant_id: str, customer_id: str, fields: Dict[str, Any]
) -> None:
    """Best-effort: apply a customer update to Postgres + outbox."""
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import CustomerRepository

    repo = CustomerRepository()
    try:
        async with session_scope() as session:
            updated = await repo.update(session, tenant_id, customer_id, **fields)
            if updated is None:
                logger.warning(
                    "Postgres dual-write update skipped: customer %s not found "
                    "in source-of-truth (tenant %s)",
                    customer_id, tenant_id,
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-write update failed for customer %s (tenant %s)",
            customer_id, tenant_id,
        )


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


async def mirror_account_create(doc: Dict[str, Any]) -> None:
    """Best-effort: persist a created account to Postgres + outbox.

    The account row has a FK to its customer. During the soak the parent
    customer may not yet be mirrored (dual-write enabled mid-stream); in that
    case we skip rather than emit a noisy FK violation — the backfill job
    (or a later customer create) will reconcile.
    """
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import AccountRepository, CustomerRepository

    accounts = AccountRepository()
    customers = CustomerRepository()
    try:
        async with session_scope() as session:
            if await accounts.get(session, doc["tenant_id"], doc["account_id"]):
                return  # already mirrored (idempotent)
            parent = await customers.get(session, doc["tenant_id"], doc["customer_id"])
            if parent is None:
                logger.warning(
                    "Postgres dual-write skipped account %s: parent customer %s "
                    "not yet mirrored (tenant %s)",
                    doc.get("account_id"), doc.get("customer_id"), doc.get("tenant_id"),
                )
                return
            await accounts.create(
                session,
                account_id=doc["account_id"],
                tenant_id=doc["tenant_id"],
                customer_id=doc["customer_id"],
                display_name=doc["display_name"],
                status=doc.get("status", "active"),
                credit_limit_cents=doc.get("credit_limit_cents", 0),
                net_terms_days=doc.get("net_terms_days", 30),
                tier=doc.get("tier", "default"),
                billing_address=doc.get("billing_address"),
                payment_method_preference=doc.get("payment_method_preference", "invoice"),
                external_refs=doc.get("external_refs") or {},
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-write failed for account %s (tenant %s)",
            doc.get("account_id"), doc.get("tenant_id"),
        )


async def mirror_account_fields(
    tenant_id: str, account_id: str, fields: Dict[str, Any], *, event_type: str = "updated"
) -> None:
    """Best-effort: apply already-computed account field values to Postgres.

    The AccountService computes absolute values (open_balance_cents,
    available_credit_cents, credit_state, etc.) before writing ES. We mirror
    those exact values rather than recomputing, so the Postgres row and the ES
    doc are guaranteed identical. ``updated_at`` is managed by the ORM and is
    stripped by the caller.
    """
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import AccountRepository

    repo = AccountRepository()
    try:
        async with session_scope() as session:
            updated = await repo.set_fields(
                session, tenant_id, account_id, event_type=event_type, **fields
            )
            if updated is None:
                logger.warning(
                    "Postgres dual-write %s skipped: account %s not found in "
                    "source-of-truth (tenant %s)",
                    event_type, account_id, tenant_id,
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-write %s failed for account %s (tenant %s)",
            event_type, account_id, tenant_id,
        )


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------


def _normalize_line_items(raw: list) -> list:
    """Coerce a service invoice doc's line_items into the repository shape.

    The service may carry ``quantity`` or ``quantity_gallons`` and may omit a
    ``line_id``; the Postgres line-item rows require both. We fill defensively
    so the mirror never fails on a benign shape difference.
    """
    from uuid import uuid4

    items = []
    for li in raw or []:
        qty = li.get("quantity_gallons", li.get("quantity", 0)) or 0
        items.append({
            "line_id": li.get("line_id") or f"line_{uuid4()}",
            "product_code": li.get("product_code", ""),
            "quantity_gallons": qty,
            "unit_price_cents": int(li.get("unit_price_cents", 0)),
            "unit_price_micros": (
                int(li["unit_price_micros"])
                if li.get("unit_price_micros") is not None
                else None
            ),
            "subtotal_cents": int(li.get("subtotal_cents", 0)),
        })
    return items


async def mirror_invoice_create(doc: Dict[str, Any]) -> None:
    """Best-effort: persist a generated invoice to Postgres + outbox.

    The invoice row has FKs to its customer and account. During the soak those
    parents may not yet be mirrored; in that case we skip rather than emit an
    FK violation. The service's authoritative totals (which may include
    TaxEngine-computed tax) are passed through verbatim.
    """
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import (
        AccountRepository,
        CustomerRepository,
        InvoiceRepository,
    )

    invoices = InvoiceRepository()
    accounts = AccountRepository()
    customers = CustomerRepository()
    try:
        async with session_scope() as session:
            if await invoices.get(session, doc["tenant_id"], doc["invoice_id"]):
                return  # already mirrored (idempotent)
            parent_customer = await customers.get(session, doc["tenant_id"], doc["customer_id"])
            parent_account = await accounts.get(session, doc["tenant_id"], doc["account_id"])
            if parent_customer is None or parent_account is None:
                logger.warning(
                    "Postgres dual-write skipped invoice %s: parent customer/account "
                    "not yet mirrored (tenant %s)",
                    doc.get("invoice_id"), doc.get("tenant_id"),
                )
                return
            await invoices.create(
                session,
                invoice_id=doc["invoice_id"],
                tenant_id=doc["tenant_id"],
                customer_id=doc["customer_id"],
                account_id=doc["account_id"],
                line_items=_normalize_line_items(doc.get("line_items")),
                order_id=doc.get("order_id"),
                pod_id=doc.get("pod_id"),
                delivered_at=doc.get("delivered_at"),
                delivery_result=doc.get("delivery_result"),
                invoice_number=doc.get("invoice_number"),
                status=doc.get("status", "draft"),
                tax_cents=doc.get("tax_cents", 0),
                subtotal_cents=doc.get("subtotal_cents"),
                total_cents=doc.get("total_cents"),
                amount_paid_cents=doc.get("amount_paid_cents", 0),
                remaining_cents=doc.get("remaining_cents"),
                due_date=doc.get("due_date"),
                external_refs=doc.get("external_refs") or {},
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-write failed for invoice %s (tenant %s)",
            doc.get("invoice_id"), doc.get("tenant_id"),
        )


async def mirror_invoice_fields(
    tenant_id: str, invoice_id: str, fields: Dict[str, Any], *, event_type: str = "updated"
) -> None:
    """Best-effort: apply already-computed invoice field values to Postgres."""
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import InvoiceRepository

    repo = InvoiceRepository()
    try:
        async with session_scope() as session:
            updated = await repo.set_fields(
                session, tenant_id, invoice_id, event_type=event_type, **fields
            )
            if updated is None:
                logger.warning(
                    "Postgres dual-write %s skipped: invoice %s not found in "
                    "source-of-truth (tenant %s)",
                    event_type, invoice_id, tenant_id,
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-write %s failed for invoice %s (tenant %s)",
            event_type, invoice_id, tenant_id,
        )


class InvoiceNumberingUnavailable(Exception):
    """Numbering is configured but the counter could not be read.

    Distinct from "numbering is off". When the persistence layer is dormant or
    dual-write is disabled, :func:`allocate_invoice_number` returns ``None`` and
    the caller keeps its legacy unnumbered behaviour — a deliberate posture for
    an ES-only deployment. But once numbering IS configured, a failure to
    allocate must not silently degrade to that posture: finalizing an invoice
    without a number produces a legally defective record, and it does so while
    reporting success.
    """


async def allocate_invoice_number(tenant_id: str) -> Optional[int]:
    """Allocate the next monotonic invoice number from the Postgres counter.

    Returns:
        The allocated integer, or ``None`` when the persistence layer is dormant
        / dual-write is off (the caller keeps its legacy behavior — today that
        means ``invoice_number`` stays ``None``).

    Raises:
        InvoiceNumberingUnavailable: when numbering is configured but the
            counter could not be allocated. Previously this was logged and
            ``None`` was returned, which is indistinguishable from "numbering is
            switched off" — so a database blip finalized an unnumbered invoice
            and the caller could not tell.
    """
    if not _enabled():
        return None
    from persistence.database import session_scope
    from persistence.repositories import InvoiceRepository

    repo = InvoiceRepository()
    try:
        async with session_scope() as session:
            allocated = await repo.allocate_number(session, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Postgres invoice-number allocation failed for tenant %s", tenant_id
        )
        raise InvoiceNumberingUnavailable(
            f"invoice-number allocation failed for tenant {tenant_id!r}: {exc}"
        ) from exc
    if allocated is None:
        # The repository allocates or raises; a None here would mean the counter
        # silently produced nothing, which is the same defect by another route.
        raise InvoiceNumberingUnavailable(
            f"invoice-number counter returned no number for tenant {tenant_id!r}"
        )
    return allocated


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


class PaymentAlreadyExists(Exception):
    """Raised by the authoritative path when a duplicate payment is detected.

    Carries the existing payment document (projected from the Postgres row) so
    the caller can return it idempotently — matching the ES fast-path's
    "return the existing payment" behavior, but now backed by a real unique
    constraint that holds under concurrency.
    """

    def __init__(self, existing: Dict[str, Any]) -> None:
        self.existing = existing
        super().__init__("Payment already exists (idempotent re-delivery)")


async def create_payment_authoritative(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Insert a payment into Postgres FIRST, enforcing dedupe at the DB.

    Returns ``None`` when payments are NOT in authoritative mode (caller keeps
    its legacy ES-first path). When authoritative:

    - inserts the payment row (+ outbox projection event);
    - on a duplicate ``(tenant, source, external_id)`` raises
      :class:`PaymentAlreadyExists` carrying the existing projected doc so the
      caller returns it idempotently;
    - returns ``None`` if the FK parents (invoice/account) are not yet mirrored
      so the caller can fall back to the ES-only path during the soak.

    This is the correctness payoff: a re-delivered Stripe/QBO webhook can never
    create a second payment row, even under concurrency.
    """
    if not _payments_authoritative():
        return None
    from persistence.database import session_scope
    from persistence.projections import payment_to_doc
    from persistence.repositories import (
        AccountRepository,
        DuplicatePaymentError,
        InvoiceRepository,
        PaymentRepository,
    )

    payments = PaymentRepository()
    invoices = InvoiceRepository()
    accounts = AccountRepository()
    try:
        async with session_scope() as session:
            # FK parents must exist for an authoritative insert. If they are
            # not mirrored yet, signal "not handled" so the caller uses ES.
            parent_invoice = await invoices.get(session, doc["tenant_id"], doc["invoice_id"])
            parent_account = await accounts.get(session, doc["tenant_id"], doc["account_id"])
            if parent_invoice is None or parent_account is None:
                logger.warning(
                    "Payments authoritative: FK parents not mirrored for payment "
                    "%s (tenant %s) — falling back to ES path",
                    doc.get("payment_id"), doc.get("tenant_id"),
                )
                return None
            try:
                row = await payments.create_idempotent(
                    session,
                    payment_id=doc["payment_id"],
                    tenant_id=doc["tenant_id"],
                    invoice_id=doc["invoice_id"],
                    account_id=doc["account_id"],
                    amount_cents=doc["amount_cents"],
                    source=doc["source"],
                    method=doc["method"],
                    external_id=doc.get("external_id"),
                    reference=doc.get("reference"),
                    status=doc.get("status", "applied"),
                )
            except DuplicatePaymentError:
                existing = await payments.find_by_external_id(
                    session, doc["tenant_id"], doc["source"], doc["external_id"]
                )
                raise PaymentAlreadyExists(
                    payment_to_doc(existing) if existing is not None else doc
                )
            return payment_to_doc(row)
    except PaymentAlreadyExists:
        raise
    except Exception:  # noqa: BLE001
        logger.exception(
            "Payments authoritative insert failed for payment %s (tenant %s) — "
            "falling back to ES path",
            doc.get("payment_id"), doc.get("tenant_id"),
        )
        return None


async def mirror_payment_create(doc: Dict[str, Any]) -> None:
    """Best-effort: persist a created payment to Postgres + outbox (soak path).

    Used when payments are dual-written but NOT yet authoritative. Skips if the
    payment already exists (idempotent) or its FK parents are not mirrored.
    """
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import (
        AccountRepository,
        InvoiceRepository,
        PaymentRepository,
    )

    payments = PaymentRepository()
    invoices = InvoiceRepository()
    accounts = AccountRepository()
    try:
        async with session_scope() as session:
            if await payments.get(session, doc["tenant_id"], doc["payment_id"]):
                return
            parent_invoice = await invoices.get(session, doc["tenant_id"], doc["invoice_id"])
            parent_account = await accounts.get(session, doc["tenant_id"], doc["account_id"])
            if parent_invoice is None or parent_account is None:
                logger.warning(
                    "Postgres dual-write skipped payment %s: parent invoice/account "
                    "not yet mirrored (tenant %s)",
                    doc.get("payment_id"), doc.get("tenant_id"),
                )
                return
            await payments.create(
                session,
                payment_id=doc["payment_id"],
                tenant_id=doc["tenant_id"],
                invoice_id=doc["invoice_id"],
                account_id=doc["account_id"],
                amount_cents=doc["amount_cents"],
                source=doc["source"],
                method=doc["method"],
                external_id=doc.get("external_id"),
                reference=doc.get("reference"),
                status=doc.get("status", "applied"),
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-write failed for payment %s (tenant %s)",
            doc.get("payment_id"), doc.get("tenant_id"),
        )


async def mirror_payment_reverse(tenant_id: str, payment_id: str, reversed_at: str) -> None:
    """Best-effort: mirror a payment reversal (status + reversed_at) to Postgres."""
    if not _enabled() and not _payments_authoritative():
        return
    from persistence.database import session_scope
    from persistence.repositories import PaymentRepository

    repo = PaymentRepository()
    try:
        async with session_scope() as session:
            updated = await repo.set_fields(
                session, tenant_id, payment_id,
                event_type="reversed", status="reversed", reversed_at=reversed_at,
            )
            if updated is None:
                logger.warning(
                    "Postgres dual-write reverse skipped: payment %s not found in "
                    "source-of-truth (tenant %s)",
                    payment_id, tenant_id,
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-write reverse failed for payment %s (tenant %s)",
            payment_id, tenant_id,
        )


# ---------------------------------------------------------------------------
# Read-cutover helpers (Phase 4)
# ---------------------------------------------------------------------------
#
# Each helper returns the projected document(s) from Postgres when the
# read-cutover flag is on, or the sentinel ``_NOT_CUT_OVER`` when reads should
# still be served from Elasticsearch. A plain ``None`` is a meaningful value
# (record genuinely not found in Postgres), so it cannot double as "not cut
# over" — hence the distinct sentinel.

_NOT_CUT_OVER = object()


def reads_cut_over() -> bool:
    """Public predicate: are commerce reads served from Postgres?"""
    return read_from_postgres()


async def read_customer_get(tenant_id: str, customer_id: str):
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import CustomerReadRepository

    async with session_scope() as session:
        return await CustomerReadRepository().get(session, tenant_id, customer_id)


async def read_customer_list(tenant_id: str, **kwargs):
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import CustomerReadRepository

    async with session_scope() as session:
        return await CustomerReadRepository().list(session, tenant_id, **kwargs)


async def read_account_get(tenant_id: str, account_id: str):
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import AccountReadRepository

    async with session_scope() as session:
        return await AccountReadRepository().get(session, tenant_id, account_id)


async def read_account_list(tenant_id: str, **kwargs):
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import AccountReadRepository

    async with session_scope() as session:
        return await AccountReadRepository().list(session, tenant_id, **kwargs)


async def read_invoice_get(tenant_id: str, invoice_id: str):
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import InvoiceReadRepository

    async with session_scope() as session:
        return await InvoiceReadRepository().get(session, tenant_id, invoice_id)


async def read_invoice_find_by_order(tenant_id: str, order_id: str):
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import InvoiceReadRepository

    async with session_scope() as session:
        return await InvoiceReadRepository().find_by_order(session, tenant_id, order_id)


async def read_invoice_list(tenant_id: str, **kwargs):
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import InvoiceReadRepository

    async with session_scope() as session:
        return await InvoiceReadRepository().list(session, tenant_id, **kwargs)


async def read_payment_get(tenant_id: str, payment_id: str):
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import PaymentReadRepository

    async with session_scope() as session:
        return await PaymentReadRepository().get(session, tenant_id, payment_id)


async def read_payment_list(tenant_id: str, **kwargs):
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import PaymentReadRepository

    async with session_scope() as session:
        return await PaymentReadRepository().list(session, tenant_id, **kwargs)


# --- AR aging / credit / dunning / background-job reads (invoices+accounts) ---


async def read_account_get_or_none(tenant_id: str, account_id: str):
    """Tenant-scoped account doc from PG, ``None`` if missing, or _NOT_CUT_OVER."""
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import AccountReadRepository

    async with session_scope() as session:
        return await AccountReadRepository().get(session, tenant_id, account_id)


async def read_invoices_open_for_aggregation(
    tenant_id: str, *, statuses, account_id=None, require_issued_at=False,
    due_on_or_before=None, order_by_due_asc=False,
):
    """Tenant-scoped open invoices for AR-aging / dunning Python rollups."""
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import InvoiceReadRepository

    async with session_scope() as session:
        return await InvoiceReadRepository().fetch_open_for_aggregation(
            session, tenant_id, statuses=statuses, account_id=account_id,
            require_issued_at=require_issued_at,
            due_on_or_before=due_on_or_before, order_by_due_asc=order_by_due_asc,
        )


async def read_invoice_sum_remaining(tenant_id: str, account_id: str, *, statuses):
    """Sum of remaining_cents over an account's invoices, or _NOT_CUT_OVER."""
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import InvoiceReadRepository

    async with session_scope() as session:
        return await InvoiceReadRepository().sum_remaining_cents(
            session, tenant_id, account_id, statuses=statuses
        )


async def read_invoice_count_accounts_with_balance(tenant_id: str, *, statuses):
    """Distinct accounts with a positive-remaining open invoice, or _NOT_CUT_OVER."""
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import InvoiceReadRepository

    async with session_scope() as session:
        return await InvoiceReadRepository().count_accounts_with_open_balance(
            session, tenant_id, statuses=statuses
        )


async def read_invoices_due_all_tenants(*, statuses, due_on_or_before):
    """CROSS-TENANT past-due invoice sweep for the overdue job, or _NOT_CUT_OVER."""
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import InvoiceReadRepository

    async with session_scope() as session:
        return await InvoiceReadRepository().scan_due_all_tenants(
            session, statuses=statuses, due_on_or_before=due_on_or_before
        )


async def read_accounts_expired_overrides_all_tenants(*, credit_state, expires_on_or_before):
    """CROSS-TENANT expired-override account sweep for the expiry job, or _NOT_CUT_OVER."""
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import AccountReadRepository

    async with session_scope() as session:
        return await AccountReadRepository().scan_expired_overrides_all_tenants(
            session, credit_state=credit_state,
            expires_on_or_before=expires_on_or_before,
        )


async def read_price_book_get(tenant_id: str, price_book_id: str):
    """Read a price book (without rules) from Postgres, or _NOT_CUT_OVER off."""
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import PriceBookReadRepository

    async with session_scope() as session:
        return await PriceBookReadRepository().get(session, tenant_id, price_book_id)


async def read_price_book_list(tenant_id: str, **kwargs):
    """List price books from Postgres, or _NOT_CUT_OVER when ES-served."""
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import PriceBookReadRepository

    async with session_scope() as session:
        return await PriceBookReadRepository().list(session, tenant_id, **kwargs)


async def read_pricing_rules_for_book(tenant_id: str, price_book_id: str):
    """Read a book's fan-out pricing rules from Postgres, or _NOT_CUT_OVER off."""
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import PriceBookReadRepository

    async with session_scope() as session:
        return await PriceBookReadRepository().rules_for_book(
            session, tenant_id, price_book_id
        )


async def read_pricing_rules_by_product(tenant_id: str, product_code: str):
    """Read all tenant pricing rules for a product (PricingEngine candidate set).

    Returns the projected ``pricing_rules_current`` docs from Postgres, or
    ``_NOT_CUT_OVER`` when the engine should still query Elasticsearch. The
    caller applies effective-window / quantity / precedence filtering, so this
    returns the full candidate set just like the ES ``size: 1000`` term query.
    """
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import PriceBookReadRepository

    async with session_scope() as session:
        return await PriceBookReadRepository().rules_by_product(
            session, tenant_id, product_code
        )


# ---------------------------------------------------------------------------
# Rest-of-commerce mirrors (price books/rules, event ledgers, AR aging)
# ---------------------------------------------------------------------------


async def mirror_price_book_create(doc: Dict[str, Any]) -> None:
    """Best-effort: persist a created price book to Postgres + outbox."""
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import PriceBookRepository

    repo = PriceBookRepository()
    try:
        async with session_scope() as session:
            if await repo.get(session, doc["tenant_id"], doc["price_book_id"]):
                return
            await repo.create(
                session, price_book_id=doc["price_book_id"], tenant_id=doc["tenant_id"],
                name=doc["name"], description=doc.get("description"),
                status=doc.get("status", "draft"), rule_count=doc.get("rule_count", 0),
            )
    except Exception:  # noqa: BLE001
        logger.exception("Postgres dual-write failed for price_book %s (tenant %s)",
                         doc.get("price_book_id"), doc.get("tenant_id"))


async def mirror_price_book_fields(tenant_id: str, price_book_id: str,
                                   fields: Dict[str, Any]) -> None:
    """Best-effort: apply price book field changes to Postgres."""
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import PriceBookRepository

    repo = PriceBookRepository()
    try:
        async with session_scope() as session:
            await repo.set_fields(session, tenant_id, price_book_id, **fields)
    except Exception:  # noqa: BLE001
        logger.exception("Postgres dual-write update failed for price_book %s (tenant %s)",
                         price_book_id, tenant_id)


async def mirror_pricing_rules_upsert(rules: list) -> None:
    """Best-effort: upsert a batch of pricing rules into Postgres.

    Skips rules whose parent price book is not yet mirrored (FK), since the
    book create mirror runs first in normal flows.
    """
    if not _enabled() or not rules:
        return
    from persistence.database import session_scope
    from persistence.repositories import PriceBookRepository, PricingRuleRepository

    rule_repo = PricingRuleRepository()
    book_repo = PriceBookRepository()
    try:
        async with session_scope() as session:
            for rule in rules:
                parent = await book_repo.get(session, rule["tenant_id"], rule["price_book_id"])
                if parent is None:
                    logger.warning(
                        "Postgres dual-write skipped pricing_rule %s: parent price_book "
                        "%s not yet mirrored (tenant %s)",
                        rule.get("rule_id"), rule.get("price_book_id"), rule.get("tenant_id"),
                    )
                    continue
                await rule_repo.upsert(session, rule=rule)
    except Exception:  # noqa: BLE001
        logger.exception("Postgres dual-write failed for pricing rule batch")


async def mirror_pricing_rules_delete(tenant_id: str, rule_ids: list) -> None:
    """Best-effort: delete pricing rules from Postgres by id."""
    if not _enabled() or not rule_ids:
        return
    from persistence.database import session_scope
    from persistence.repositories import PricingRuleRepository

    repo = PricingRuleRepository()
    try:
        async with session_scope() as session:
            for rid in rule_ids:
                await repo.delete(session, tenant_id, rid)
    except Exception:  # noqa: BLE001
        logger.exception("Postgres dual-write delete failed for pricing rules")


async def mirror_invoice_event(doc: Dict[str, Any]) -> None:
    """Best-effort: append an invoice event to the Postgres ledger."""
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import InvoiceEventRepository

    try:
        async with session_scope() as session:
            await InvoiceEventRepository().append(session, doc=doc)
    except Exception:  # noqa: BLE001
        logger.exception("Postgres dual-write failed for invoice_event %s (tenant %s)",
                         doc.get("event_id"), doc.get("tenant_id"))


async def mirror_account_event(doc: Dict[str, Any]) -> None:
    """Best-effort: append an account event to the Postgres ledger."""
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import AccountEventRepository

    try:
        async with session_scope() as session:
            await AccountEventRepository().append(session, doc=doc)
    except Exception:  # noqa: BLE001
        logger.exception("Postgres dual-write failed for account_event %s (tenant %s)",
                         doc.get("event_id"), doc.get("tenant_id"))


async def mirror_dunning_event_create(doc: Dict[str, Any]) -> None:
    """Best-effort: persist a dunning event to Postgres + outbox."""
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import DunningEventRepository

    try:
        async with session_scope() as session:
            await DunningEventRepository().create(session, doc=doc)
    except Exception:  # noqa: BLE001
        logger.exception("Postgres dual-write failed for dunning_event %s (tenant %s)",
                         doc.get("event_id"), doc.get("tenant_id"))


async def mirror_dunning_event_fields(tenant_id: str, event_id: str,
                                      fields: Dict[str, Any]) -> None:
    """Best-effort: apply dunning event field changes (e.g. cancellation)."""
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import DunningEventRepository

    try:
        async with session_scope() as session:
            await DunningEventRepository().set_fields(session, tenant_id, event_id, **fields)
    except Exception:  # noqa: BLE001
        logger.exception("Postgres dual-write update failed for dunning_event %s (tenant %s)",
                         event_id, tenant_id)


async def mirror_ar_aging_snapshot(doc: Dict[str, Any]) -> None:
    """Best-effort: upsert an AR aging snapshot into Postgres + outbox."""
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import ArAgingSnapshotRepository

    try:
        async with session_scope() as session:
            await ArAgingSnapshotRepository().upsert(session, doc=doc)
    except Exception:  # noqa: BLE001
        logger.exception("Postgres dual-write failed for ar_aging_snapshot %s (tenant %s)",
                         doc.get("snapshot_id"), doc.get("tenant_id"))


# ---------------------------------------------------------------------------
# Compliance config mirrors (tax, contracts, sell-side pricing rules)
# ---------------------------------------------------------------------------


async def mirror_compliance_config_upsert(aggregate_type: str, doc: Dict[str, Any]) -> None:
    """Best-effort: upsert a compliance-config record into Postgres + outbox.

    ``aggregate_type`` is one of: tax_jurisdiction, tax_exemption,
    price_protection_contract, compliance_pricing_rule, supplier_contract.
    The full ES ``doc`` is stored verbatim (hybrid document table), so the ES
    projection stays byte-identical.
    """
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import ComplianceConfigRepository

    try:
        repo = ComplianceConfigRepository(aggregate_type)
        async with session_scope() as session:
            await repo.upsert(session, doc=doc)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-write failed for %s (tenant %s)",
            aggregate_type, doc.get("tenant_id"),
        )


async def mirror_compliance_config_delete(
    aggregate_type: str, tenant_id: str, doc_id: str
) -> None:
    """Best-effort: delete a compliance-config record from Postgres."""
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import ComplianceConfigRepository

    try:
        repo = ComplianceConfigRepository(aggregate_type)
        async with session_scope() as session:
            await repo.delete(session, tenant_id, doc_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-write delete failed for %s %s (tenant %s)",
            aggregate_type, doc_id, tenant_id,
        )


# ---------------------------------------------------------------------------
# Orders / jobs current-state mirrors
# ---------------------------------------------------------------------------


async def mirror_current_state_upsert(
    aggregate_type: str, doc: Dict[str, Any], *, doc_id: str | None = None
) -> None:
    """Best-effort: upsert an orders/jobs current-state record into Postgres.

    ``aggregate_type`` is one of: fuel_order, job, tenant_job_policy.
    (``shipment`` was retired with the ``shipments_current`` table, rev 0007.)
    The full ES ``doc`` is stored verbatim (hybrid document table) so the ES
    projection stays byte-identical. The stale-event guard in the repository
    discards out-of-order writes the same way the ES scripted upsert does.
    """
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import CurrentStateRepository

    try:
        repo = CurrentStateRepository(aggregate_type)
        async with session_scope() as session:
            await repo.upsert(session, doc=doc, doc_id=doc_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-write failed for %s (tenant %s)",
            aggregate_type, doc.get("tenant_id"),
        )


async def mirror_current_state_delete(
    aggregate_type: str, tenant_id: str, doc_id: str
) -> None:
    """Best-effort: delete an orders/jobs current-state row from Postgres.

    Keeps the PG source-of-truth in step with a service-level delete so a
    read-cutover deployment stops serving the deleted aggregate. Best-effort
    during the soak (ES delete is handled by the caller).
    """
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import CurrentStateRepository

    try:
        repo = CurrentStateRepository(aggregate_type)
        async with session_scope() as session:
            await repo.delete(session, tenant_id, doc_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-delete failed for %s %s (tenant %s)",
            aggregate_type, doc_id, tenant_id,
        )


async def mirror_current_state_fields(
    aggregate_type: str, tenant_id: str, doc_id: str, fields: Dict[str, Any]
) -> None:
    """Best-effort: merge a partial field update into a current-state row.

    For status-transition writes that apply an ES partial update without the
    full document on hand (e.g. the asset-certification expiry sweep marking a
    cert ``expiring_soon`` / ``expired`` / ``superseded``). Merges ``fields``
    into the verbatim ``document`` column + typed columns so the PG
    source-of-truth and the ES projection converge.
    """
    if not _enabled():
        return
    from persistence.database import session_scope
    from persistence.repositories import CurrentStateRepository

    try:
        repo = CurrentStateRepository(aggregate_type)
        async with session_scope() as session:
            await repo.set_fields(session, tenant_id, doc_id, **fields)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres dual-write fields failed for %s %s (tenant %s)",
            aggregate_type, doc_id, tenant_id,
        )


# ---------------------------------------------------------------------------
# Read-cutover helpers for hybrid aggregates (config / current-state / master)
# ---------------------------------------------------------------------------
#
# Same sentinel contract as the commerce read helpers: return _NOT_CUT_OVER
# when reads should still come from ES, the projected document (or None) when
# read-cutover is active.


def _hybrid_cut_over(aggregate_type: str) -> bool:
    """Whether hybrid reads for this aggregate should come from Postgres.

    Two conditions, not one. The flag has to be on *and* the aggregate has to
    have a Postgres table to read. Checking only the flag meant a retired
    aggregate — ``shipment``, dropped with ``shipments_current`` in rev 0007 —
    took every caller into ``HybridReadRepository.__init__`` and a ``ValueError``
    as soon as ``COMMERCE_READ_FROM_POSTGRES`` was turned on. An aggregate with
    no table is not cut over by definition, so the honest answer is to keep
    serving it from Elasticsearch rather than to fail.
    """
    if not read_from_postgres():
        return False
    from persistence.read_repositories import HybridReadRepository

    return HybridReadRepository.is_registered(aggregate_type)


async def read_hybrid_get(aggregate_type: str, tenant_id: str, doc_id: str):
    """Read one hybrid aggregate from Postgres, or _NOT_CUT_OVER when ES-served."""
    if not _hybrid_cut_over(aggregate_type):
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import HybridReadRepository

    repo = HybridReadRepository(aggregate_type)
    async with session_scope() as session:
        return await repo.get(session, tenant_id, doc_id)


async def read_hybrid_get_any(aggregate_type: str, doc_id: str):
    """Tenant-agnostic get-by-id from Postgres, or _NOT_CUT_OVER when ES-served.

    For lookups where the tenant is derived from the row (e.g. webhook channel
    resolution by globally-unique channel_id).
    """
    if not _hybrid_cut_over(aggregate_type):
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import HybridReadRepository

    repo = HybridReadRepository(aggregate_type)
    async with session_scope() as session:
        return await repo.get_any(session, doc_id)


async def read_hybrid_find_one(aggregate_type: str, tenant_id: str, *,
                               term_filters: dict | None = None):
    """First tenant-scoped doc matching term_filters, or _NOT_CUT_OVER off."""
    if not _hybrid_cut_over(aggregate_type):
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import HybridReadRepository

    repo = HybridReadRepository(aggregate_type)
    async with session_scope() as session:
        return await repo.find_one(session, tenant_id, term_filters=term_filters)


async def read_hybrid_list(aggregate_type: str, tenant_id: str, *,
                           filters: dict | None = None, cursor: str | None = None,
                           limit: int = 50):
    """List a hybrid aggregate from Postgres, or _NOT_CUT_OVER when ES-served."""
    if not _hybrid_cut_over(aggregate_type):
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import HybridReadRepository

    repo = HybridReadRepository(aggregate_type)
    async with session_scope() as session:
        return await repo.list(
            session, tenant_id, filters=filters, cursor=cursor, limit=limit
        )


async def read_hybrid_search(aggregate_type: str, tenant_id: str, *,
                             term_filters: dict | None = None,
                             in_filters: dict | None = None,
                             bool_filters: dict | None = None,
                             range_field: str | None = None,
                             range_gte: str | None = None,
                             range_lte: str | None = None,
                             range_lt: str | None = None,
                             exists_fields: list | None = None,
                             text_query: str | None = None,
                             text_fields: list | None = None,
                             sort_field: str = "created_at",
                             sort_order: str = "desc",
                             page: int = 1, size: int = 20):
    """Offset-paginated search of a hybrid aggregate from Postgres.

    Returns the ES-equivalent ``{"items", "total", "page", "size"}`` envelope,
    or ``_NOT_CUT_OVER`` when reads should still be served from Elasticsearch.
    Used for the orders/jobs list endpoints whose contract is offset + total
    (not the keyset ``list`` helper above).
    """
    if not _hybrid_cut_over(aggregate_type):
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import HybridReadRepository

    repo = HybridReadRepository(aggregate_type)
    async with session_scope() as session:
        return await repo.search(
            session, tenant_id,
            term_filters=term_filters, in_filters=in_filters,
            bool_filters=bool_filters,
            range_field=range_field, range_gte=range_gte, range_lte=range_lte,
            range_lt=range_lt, exists_fields=exists_fields,
            text_query=text_query, text_fields=text_fields,
            sort_field=sort_field, sort_order=sort_order,
            page=page, size=size,
        )


async def read_hybrid_search_all_tenants(aggregate_type: str, *,
                                         term_filters: dict | None = None,
                                         in_filters: dict | None = None,
                                         bool_filters: dict | None = None,
                                         range_field: str | None = None,
                                         range_gte: str | None = None,
                                         range_lte: str | None = None,
                                         range_lt: str | None = None,
                                         exists_fields: list | None = None,
                                         sort_field: str = "created_at",
                                         sort_order: str = "asc",
                                         size: int = 200):
    """CROSS-TENANT hybrid search for the autonomous monitor sweeps.

    Returns a list of verbatim documents matching the filters across ALL
    tenants (the agents dispatch per-tenant internally), or ``_NOT_CUT_OVER``
    when reads should still be served from Elasticsearch. System-level only —
    request-path reads must use the tenant-scoped ``read_hybrid_search``.
    """
    if not _hybrid_cut_over(aggregate_type):
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import HybridReadRepository

    repo = HybridReadRepository(aggregate_type)
    async with session_scope() as session:
        return await repo.search_all_tenants(
            session,
            term_filters=term_filters, in_filters=in_filters,
            bool_filters=bool_filters,
            range_field=range_field, range_gte=range_gte, range_lte=range_lte,
            range_lt=range_lt, exists_fields=exists_fields,
            sort_field=sort_field, sort_order=sort_order, size=size,
        )


async def read_hybrid_fetch_for_aggregation(
    aggregate_type: str, tenant_id: str, *,
    term_filters: dict | None = None,
    in_filters: dict | None = None,
    bool_filters: dict | None = None,
    exists_fields: list | None = None,
    range_field: str | None = None,
    range_gte: str | None = None,
    range_lte: str | None = None,
):
    """Fetch all matching hybrid documents for in-Python aggregation.

    Returns a list of verbatim documents, or ``_NOT_CUT_OVER`` when reads
    should still be served from Elasticsearch. Powers the analytics/metrics
    endpoints (job counts, completion, asset utilization, delays) and the
    tax-engine FIPS/exemption lookups,
    which compute their rollups / filtering in Python — so the Postgres path
    reuses the identical post-processing and stays byte-identical to the ES
    query output.
    """
    if not _hybrid_cut_over(aggregate_type):
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import HybridReadRepository

    repo = HybridReadRepository(aggregate_type)
    async with session_scope() as session:
        return await repo.fetch_for_aggregation(
            session, tenant_id,
            term_filters=term_filters, in_filters=in_filters,
            bool_filters=bool_filters, exists_fields=exists_fields,
            range_field=range_field, range_gte=range_gte, range_lte=range_lte,
        )


async def read_hybrid_list_sorted(
    aggregate_type: str, tenant_id: str, *,
    term_filters: dict | None = None,
    sort_doc_field: str = "created_at",
    sort_order: str = "asc",
    cursor: str | None = None,
    limit: int = 50,
):
    """Keyset list sorted by a document field then pk, from Postgres.

    Returns ``{items, next_cursor, limit}``, or ``_NOT_CUT_OVER`` when reads
    should still be served from Elasticsearch. Used by aggregates whose list
    order is a document field rather than the typed ``created_at`` mirror
    column (e.g. asset_certifications sorted by ``expiry_date asc, cert_id asc``).
    """
    if not _hybrid_cut_over(aggregate_type):
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import HybridReadRepository

    repo = HybridReadRepository(aggregate_type)
    async with session_scope() as session:
        return await repo.list_sorted(
            session, tenant_id,
            term_filters=term_filters,
            sort_doc_field=sort_doc_field, sort_order=sort_order,
            cursor=cursor, limit=limit,
        )
