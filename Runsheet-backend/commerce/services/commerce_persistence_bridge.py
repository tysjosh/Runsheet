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


async def allocate_invoice_number(tenant_id: str) -> Optional[int]:
    """Allocate the next monotonic invoice number from the Postgres counter.

    Returns the allocated integer, or ``None`` when the persistence layer is
    dormant / dual-write is off (caller keeps its legacy behavior — today that
    means ``invoice_number`` stays ``None``, exactly as before).
    """
    if not _enabled():
        return None
    from persistence.database import session_scope
    from persistence.repositories import InvoiceRepository

    repo = InvoiceRepository()
    try:
        async with session_scope() as session:
            return await repo.allocate_number(session, tenant_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Postgres invoice-number allocation failed for tenant %s", tenant_id
        )
        return None


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

    ``aggregate_type`` is one of: fuel_order, job, shipment, tenant_job_policy.
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


# ---------------------------------------------------------------------------
# Read-cutover helpers for hybrid aggregates (config / current-state / master)
# ---------------------------------------------------------------------------
#
# Same sentinel contract as the commerce read helpers: return _NOT_CUT_OVER
# when reads should still come from ES, the projected document (or None) when
# read-cutover is active.


async def read_hybrid_get(aggregate_type: str, tenant_id: str, doc_id: str):
    """Read one hybrid aggregate from Postgres, or _NOT_CUT_OVER when ES-served."""
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import HybridReadRepository

    repo = HybridReadRepository(aggregate_type)
    async with session_scope() as session:
        return await repo.get(session, tenant_id, doc_id)


async def read_hybrid_list(aggregate_type: str, tenant_id: str, *,
                           filters: dict | None = None, cursor: str | None = None,
                           limit: int = 50):
    """List a hybrid aggregate from Postgres, or _NOT_CUT_OVER when ES-served."""
    if not read_from_postgres():
        return _NOT_CUT_OVER
    from persistence.database import session_scope
    from persistence.read_repositories import HybridReadRepository

    repo = HybridReadRepository(aggregate_type)
    async with session_scope() as session:
        return await repo.list(
            session, tenant_id, filters=filters, cursor=cursor, limit=limit
        )
