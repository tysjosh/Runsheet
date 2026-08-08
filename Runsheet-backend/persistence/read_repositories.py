"""Read-side queries against the Postgres source-of-truth.

These power the read-cutover (Phase 4): when ``commerce_read_from_postgres`` is
on, the commerce services serve ``get`` / ``list`` from here instead of
Elasticsearch. Every method returns the SAME document shape the ES path
returned — produced by :mod:`persistence.projections` — so callers and the UI
see no difference after the switch.

Pagination matches each service's keyset contract exactly:

    customers / accounts / invoices : sort (created_at DESC, <id> ASC), cursor=<id>
    payments                        : sort (applied_at DESC, payment_id ASC), cursor=payment_id

The cursor is the trailing row's id (the services use the id as the opaque
cursor and pass it as both ``search_after`` values). To reproduce that keyset
semantics deterministically we resolve the cursor row's sort key and page with
a ``(sort_field, id) < (cursor_sort, cursor_id)`` tuple comparison.

All queries are tenant-scoped; no method exposes a cross-tenant read.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from persistence.models import AccountORM, CustomerORM, InvoiceORM, PaymentORM, PriceBookORM, PricingRuleORM
from persistence.projections import (
    account_to_doc,
    customer_to_doc,
    invoice_to_doc,
    payment_to_doc,
    price_book_to_doc,
    pricing_rule_to_doc,
)

logger = logging.getLogger(__name__)

_DEFAULT_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 200


def _clamp(limit: int) -> int:
    if limit < 1:
        return _DEFAULT_PAGE_LIMIT
    return min(limit, _MAX_PAGE_LIMIT)


def _ilike_contains(column, query: str):
    """Build a case-insensitive ``column ILIKE %query%`` with LIKE metacharacters
    in ``query`` escaped so ``%`` / ``_`` match literally.

    Returns ``None`` when ``query`` is blank. Portable across Postgres (native
    ILIKE) and SQLite (``lower() LIKE lower()`` via SQLAlchemy's ``ilike``).
    """
    if not query or not query.strip():
        return None
    needle = query.strip()
    escaped = (
        needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return column.ilike(f"%{escaped}%", escape="\\")


def _page_result(items: List[Dict[str, Any]], limit: int, id_key: str) -> Dict[str, Any]:
    """Shape a list response identically to the ES-backed services."""
    next_cursor: Optional[str] = None
    if len(items) == limit and items:
        next_cursor = items[-1][id_key]
    return {"items": items, "next_cursor": next_cursor, "limit": limit}


async def _keyset_page(
    session: AsyncSession,
    model,
    *,
    tenant_id: str,
    filters: list,
    sort_col,
    id_col,
    cursor: Optional[str],
    limit: int,
    options: Optional[list] = None,
) -> list:
    """Run a (sort_col DESC, id_col ASC) keyset page, resolving the cursor row.

    The services use the trailing row's id as the cursor. We look up that row's
    ``sort_col`` to build the strict ``(sort, id)`` boundary so pages are
    contiguous and stable even when many rows share a ``created_at``.
    """
    where = [model.tenant_id == tenant_id, *filters]

    if cursor:
        cur = (
            await session.execute(
                select(sort_col, id_col).where(
                    model.tenant_id == tenant_id, id_col == cursor
                )
            )
        ).first()
        if cur is not None:
            cur_sort, cur_id = cur
            # Next page: rows ordered after the cursor in (sort DESC, id ASC).
            where.append(
                or_(
                    sort_col < cur_sort,
                    and_(sort_col == cur_sort, id_col > cur_id),
                )
            )

    stmt = (
        select(model)
        .where(*where)
        .order_by(sort_col.desc(), id_col.asc())
        .limit(limit)
    )
    if options:
        stmt = stmt.options(*options)
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


class CustomerReadRepository:
    async def get(self, session: AsyncSession, tenant_id: str,
                  customer_id: str) -> Optional[Dict[str, Any]]:
        row = (
            await session.execute(
                select(CustomerORM).where(
                    CustomerORM.tenant_id == tenant_id,
                    CustomerORM.customer_id == customer_id,
                )
            )
        ).scalar_one_or_none()
        return customer_to_doc(row) if row is not None else None

    async def list(self, session: AsyncSession, tenant_id: str, *,
                   status: Optional[str] = None, search: Optional[str] = None,
                   cursor: Optional[str] = None,
                   limit: int = _DEFAULT_PAGE_LIMIT) -> Dict[str, Any]:
        limit = _clamp(limit)
        filters = [CustomerORM.status == status] if status else []
        if search and search.strip():
            clauses = [
                _ilike_contains(col, search)
                for col in (
                    CustomerORM.display_name,
                    CustomerORM.legal_name,
                    CustomerORM.primary_email,
                    CustomerORM.customer_id,
                )
            ]
            filters.append(or_(*[c for c in clauses if c is not None]))
        rows = await _keyset_page(
            session, CustomerORM, tenant_id=tenant_id, filters=filters,
            sort_col=CustomerORM.created_at, id_col=CustomerORM.customer_id,
            cursor=cursor, limit=limit,
        )
        return _page_result([customer_to_doc(r) for r in rows], limit, "customer_id")


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


class AccountReadRepository:
    async def get(self, session: AsyncSession, tenant_id: str,
                  account_id: str) -> Optional[Dict[str, Any]]:
        row = (
            await session.execute(
                select(AccountORM).where(
                    AccountORM.tenant_id == tenant_id,
                    AccountORM.account_id == account_id,
                )
            )
        ).scalar_one_or_none()
        return account_to_doc(row) if row is not None else None

    async def list(self, session: AsyncSession, tenant_id: str, *,
                   customer_id: Optional[str] = None, status: Optional[str] = None,
                   cursor: Optional[str] = None,
                   limit: int = _DEFAULT_PAGE_LIMIT) -> Dict[str, Any]:
        limit = _clamp(limit)
        filters = []
        if customer_id:
            filters.append(AccountORM.customer_id == customer_id)
        if status:
            filters.append(AccountORM.status == status)
        rows = await _keyset_page(
            session, AccountORM, tenant_id=tenant_id, filters=filters,
            sort_col=AccountORM.created_at, id_col=AccountORM.account_id,
            cursor=cursor, limit=limit,
        )
        return _page_result([account_to_doc(r) for r in rows], limit, "account_id")

    async def scan_expired_overrides_all_tenants(
        self, session: AsyncSession, *, credit_state: str, expires_on_or_before,
        cap: int = 1_000,
    ) -> List[Dict[str, Any]]:
        """CROSS-TENANT scan for accounts whose credit override has expired.

        Powers the credit-override-expiry background job — a system-level
        sweep (no tenant filter); each ``expire_override`` call is
        tenant-scoped internally. Matches the ES filter ``credit_state ==
        override AND credit_override_expires_at <= now``. Returns the verbatim
        ``accounts_current`` projection.
        """
        rows = (
            await session.execute(
                select(AccountORM)
                .where(
                    AccountORM.credit_state == credit_state,
                    AccountORM.credit_override_expires_at.is_not(None),
                    AccountORM.credit_override_expires_at <= expires_on_or_before,
                )
                .limit(cap)
            )
        ).scalars().all()
        return [account_to_doc(r) for r in rows]


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------


class InvoiceReadRepository:
    async def get(self, session: AsyncSession, tenant_id: str,
                  invoice_id: str) -> Optional[Dict[str, Any]]:
        row = (
            await session.execute(
                select(InvoiceORM)
                .where(
                    InvoiceORM.tenant_id == tenant_id,
                    InvoiceORM.invoice_id == invoice_id,
                )
                .options(selectinload(InvoiceORM.line_items))
            )
        ).scalar_one_or_none()
        return invoice_to_doc(row) if row is not None else None

    async def find_by_order(self, session: AsyncSession, tenant_id: str,
                            order_id: str) -> Optional[Dict[str, Any]]:
        row = (
            await session.execute(
                select(InvoiceORM)
                .where(
                    InvoiceORM.tenant_id == tenant_id,
                    InvoiceORM.order_id == order_id,
                )
                .options(selectinload(InvoiceORM.line_items))
                .limit(1)
            )
        ).scalar_one_or_none()
        return invoice_to_doc(row) if row is not None else None

    async def list(self, session: AsyncSession, tenant_id: str, *,
                   status: Optional[str] = None, customer_id: Optional[str] = None,
                   account_id: Optional[str] = None, order_id: Optional[str] = None,
                   cursor: Optional[str] = None,
                   limit: int = _DEFAULT_PAGE_LIMIT) -> Dict[str, Any]:
        limit = _clamp(limit)
        filters = []
        if status:
            filters.append(InvoiceORM.status == status)
        if customer_id:
            filters.append(InvoiceORM.customer_id == customer_id)
        if account_id:
            filters.append(InvoiceORM.account_id == account_id)
        if order_id:
            filters.append(InvoiceORM.order_id == order_id)
        rows = await _keyset_page(
            session, InvoiceORM, tenant_id=tenant_id, filters=filters,
            sort_col=InvoiceORM.created_at, id_col=InvoiceORM.invoice_id,
            cursor=cursor, limit=limit,
            options=[selectinload(InvoiceORM.line_items)],
        )
        return _page_result([invoice_to_doc(r) for r in rows], limit, "invoice_id")

    # --- Aggregation / sweep reads (AR aging, credit, dunning, overdue job) ---

    async def fetch_open_for_aggregation(
        self, session: AsyncSession, tenant_id: str, *,
        statuses: List[str],
        account_id: Optional[str] = None,
        require_issued_at: bool = False,
        due_on_or_before=None,
        order_by_due_asc: bool = False,
        cap: int = 10_000,
    ) -> List[Dict[str, Any]]:
        """Tenant-scoped open-invoice fetch for in-Python rollups.

        Powers AR aging (bucket math), the dunning overdue scan, and the
        credit open-balance sum. Returns the verbatim ``invoices_current``
        projection so the callers' existing Python aggregation is unchanged.
        ``statuses`` is the ES ``terms`` set; ``require_issued_at`` mirrors the
        ES ``exists: issued_at`` filter; ``due_on_or_before`` applies the
        ``due_date <= X`` range (date-compared, matching the date-only column).
        """
        filters = [
            InvoiceORM.tenant_id == tenant_id,
            InvoiceORM.status.in_(statuses),
        ]
        if account_id:
            filters.append(InvoiceORM.account_id == account_id)
        if require_issued_at:
            filters.append(InvoiceORM.issued_at.is_not(None))
        if due_on_or_before is not None:
            filters.append(InvoiceORM.due_date <= due_on_or_before)
        stmt = (
            select(InvoiceORM)
            .where(*filters)
            .options(selectinload(InvoiceORM.line_items))
            .limit(cap)
        )
        if order_by_due_asc:
            stmt = stmt.order_by(InvoiceORM.due_date.asc())
        rows = (await session.execute(stmt)).scalars().all()
        return [invoice_to_doc(r) for r in rows]

    async def sum_remaining_cents(
        self, session: AsyncSession, tenant_id: str, account_id: str, *,
        statuses: List[str],
    ) -> int:
        """Sum ``remaining_cents`` over an account's invoices in ``statuses``.

        Mirrors the credit-service ES ``sum`` aggregation (Constraint C1 —
        integer cents). Pushed into SQL since no per-row data is needed.
        """
        total = (
            await session.execute(
                select(func.coalesce(func.sum(InvoiceORM.remaining_cents), 0)).where(
                    InvoiceORM.tenant_id == tenant_id,
                    InvoiceORM.account_id == account_id,
                    InvoiceORM.status.in_(statuses),
                )
            )
        ).scalar_one()
        return int(total or 0)

    async def count_accounts_with_open_balance(
        self, session: AsyncSession, tenant_id: str, *, statuses: List[str],
    ) -> int:
        """Distinct accounts with a positive-remaining open invoice.

        Mirrors the AR-aging ``cardinality(account_id)`` aggregation filtered
        to ``status in statuses`` and ``remaining_cents > 0``.
        """
        count = (
            await session.execute(
                select(func.count(func.distinct(InvoiceORM.account_id))).where(
                    InvoiceORM.tenant_id == tenant_id,
                    InvoiceORM.status.in_(statuses),
                    InvoiceORM.remaining_cents > 0,
                )
            )
        ).scalar_one()
        return int(count or 0)

    async def scan_due_all_tenants(
        self, session: AsyncSession, *, statuses: List[str], due_on_or_before,
        cap: int = 10_000,
    ) -> List[Dict[str, Any]]:
        """CROSS-TENANT past-due invoice scan for the overdue background job.

        The job is a system-level sweep (no tenant filter); each downstream
        ``mark_overdue`` call is tenant-scoped internally. Returns the verbatim
        projection so the job's per-tenant grouping is unchanged.
        """
        rows = (
            await session.execute(
                select(InvoiceORM)
                .where(
                    InvoiceORM.status.in_(statuses),
                    InvoiceORM.due_date.is_not(None),
                    InvoiceORM.due_date <= due_on_or_before,
                )
                .options(selectinload(InvoiceORM.line_items))
                .limit(cap)
            )
        ).scalars().all()
        return [invoice_to_doc(r) for r in rows]


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


class PaymentReadRepository:
    async def get(self, session: AsyncSession, tenant_id: str,
                  payment_id: str) -> Optional[Dict[str, Any]]:
        row = (
            await session.execute(
                select(PaymentORM).where(
                    PaymentORM.tenant_id == tenant_id,
                    PaymentORM.payment_id == payment_id,
                )
            )
        ).scalar_one_or_none()
        return payment_to_doc(row) if row is not None else None

    async def list(self, session: AsyncSession, tenant_id: str, *,
                   invoice_id: Optional[str] = None, account_id: Optional[str] = None,
                   cursor: Optional[str] = None,
                   limit: int = _DEFAULT_PAGE_LIMIT) -> Dict[str, Any]:
        limit = _clamp(limit)
        filters = []
        if invoice_id:
            filters.append(PaymentORM.invoice_id == invoice_id)
        if account_id:
            filters.append(PaymentORM.account_id == account_id)
        rows = await _keyset_page(
            session, PaymentORM, tenant_id=tenant_id, filters=filters,
            sort_col=PaymentORM.applied_at, id_col=PaymentORM.payment_id,
            cursor=cursor, limit=limit,
        )
        return _page_result([payment_to_doc(r) for r in rows], limit, "payment_id")


# ---------------------------------------------------------------------------
# Price book / pricing rule (commerce, typed-column models)
# ---------------------------------------------------------------------------


class PriceBookReadRepository:
    """Read-side for commerce price books + their fan-out pricing rules.

    Unlike the hybrid aggregates these are typed-column models, so reads
    project through ``price_book_to_doc`` / ``pricing_rule_to_doc`` to return
    the byte-identical ``price_books_current`` / ``pricing_rules_current``
    document shapes the ES path returned.
    """

    async def get(self, session: AsyncSession, tenant_id: str,
                  price_book_id: str) -> Optional[Dict[str, Any]]:
        row = (
            await session.execute(
                select(PriceBookORM).where(
                    PriceBookORM.tenant_id == tenant_id,
                    PriceBookORM.price_book_id == price_book_id,
                )
            )
        ).scalar_one_or_none()
        return price_book_to_doc(row) if row is not None else None

    async def list(self, session: AsyncSession, tenant_id: str, *,
                   status: Optional[str] = None, cursor: Optional[str] = None,
                   limit: int = _DEFAULT_PAGE_LIMIT) -> Dict[str, Any]:
        limit = _clamp(limit)
        filters = [PriceBookORM.status == status] if status else []
        rows = await _keyset_page(
            session, PriceBookORM, tenant_id=tenant_id, filters=filters,
            sort_col=PriceBookORM.created_at, id_col=PriceBookORM.price_book_id,
            cursor=cursor, limit=limit,
        )
        return _page_result(
            [price_book_to_doc(r) for r in rows], limit, "price_book_id"
        )

    async def rules_for_book(self, session: AsyncSession, tenant_id: str,
                             price_book_id: str) -> List[Dict[str, Any]]:
        """All pricing rules for a book, ordered ``created_at ASC`` (ES contract)."""
        rows = list(
            (
                await session.execute(
                    select(PricingRuleORM)
                    .where(
                        PricingRuleORM.tenant_id == tenant_id,
                        PricingRuleORM.price_book_id == price_book_id,
                    )
                    .order_by(
                        PricingRuleORM.created_at.asc(),
                        PricingRuleORM.rule_id.asc(),
                    )
                )
            ).scalars().all()
        )
        return [pricing_rule_to_doc(r) for r in rows]

    async def rules_by_product(self, session: AsyncSession, tenant_id: str,
                               product_code: str) -> List[Dict[str, Any]]:
        """Every tenant rule for a product (PricingEngine candidate set).

        Mirrors the ES ``pricing_rules_current`` term query on
        ``product_code`` (size 1000); the engine applies effective-window /
        quantity / precedence filtering in Python afterward, so we return the
        full candidate set projected to the ES doc shape. Backed by the
        ``ix_pricing_rule_tenant_product`` composite index.
        """
        rows = list(
            (
                await session.execute(
                    select(PricingRuleORM).where(
                        PricingRuleORM.tenant_id == tenant_id,
                        PricingRuleORM.product_code == product_code,
                    )
                )
            ).scalars().all()
        )
        return [pricing_rule_to_doc(r) for r in rows]


# ---------------------------------------------------------------------------
# Hybrid document tables (compliance config, orders/jobs current-state,
# master data). The stored ``document`` column IS the ES projection, so reads
# return it verbatim. One read repo serves every hybrid aggregate via a spec.
# ---------------------------------------------------------------------------


class HybridReadRepository:
    """Read-side queries for the hybrid ``document`` tables.

    Bound to one aggregate type; returns the verbatim stored ES document so the
    read path is byte-identical to what ES returned. Supports get-by-id,
    optional-tenant get, and filtered list with a stable ``(created_at DESC,
    <pk> ASC)`` keyset page matching the existing list contracts.
    """

    # aggregate_type -> (ORM model, pk attr, tenant-optional?)
    _SPECS = {
        # compliance config
        "tax_jurisdiction": ("TaxJurisdictionORM", "jurisdiction_id", False),
        "tax_exemption": ("TaxExemptionORM", "exemption_id", False),
        "price_protection_contract": ("PriceProtectionContractORM", "contract_id", False),
        "compliance_pricing_rule": ("CompliancePricingRuleORM", "rule_id", False),
        "supplier_contract": ("SupplierContractORM", "contract_id", False),
        # orders / jobs current-state
        "fuel_order": ("FuelOrderCurrentORM", "order_id", False),
        "job": ("JobCurrentORM", "job_id", False),
        # ``shipment`` was retired with the ``shipments_current`` table (rev 0007).
        "tenant_job_policy": ("TenantJobPolicyORM", "policy_id", False),
        # master data
        "driver": ("DriverMasterORM", "driver_id", False),
        "depot": ("DepotORM", "depot_id", False),
        "terminal": ("TerminalORM", "terminal_id", False),
        "asset_certification": ("AssetCertificationORM", "cert_id", False),
        "intake_channel": ("IntakeChannelORM", "channel_id", False),
        # legacy generic-ES indices may carry no tenant_id
        "truck": ("TruckORM", "truck_id", True),
        "location": ("LocationORM", "location_id", True),
        # fuel assets (previously Elasticsearch-only)
        "customer_tank": ("CustomerTankORM", "customer_tank_id", False),
        "truck_compartment": ("TruckCompartmentORM", "compartment_key", False),
        "fuel_station": ("FuelStationORM", "station_key", False),
    }

    @classmethod
    def is_registered(cls, aggregate_type: str) -> bool:
        """Whether this aggregate has a Postgres table to read from.

        An aggregate absent from :attr:`_SPECS` has no table, so it cannot be
        read-cut-over no matter what ``COMMERCE_READ_FROM_POSTGRES`` says. The
        hybrid read helpers consult this and fall back to Elasticsearch instead
        of constructing a repository that would raise.

        Exists because retiring an aggregate leaves callers behind. ``shipment``
        was removed here when ``shipments_current`` was dropped (rev 0007), but
        seven ``ops`` endpoints still ask for it; with the flag on they raised
        ``ValueError`` from the constructor below. They are behind a default-off
        feature flag, so nobody hit it — which is exactly why a guard is better
        placed here than in seven call sites that can be added faster than they
        are found.
        """
        return aggregate_type in cls._SPECS

    def __init__(self, aggregate_type: str) -> None:
        if aggregate_type not in self._SPECS:
            raise ValueError(f"Unknown hybrid aggregate_type: {aggregate_type!r}")
        import persistence.models as _models

        self.aggregate_type = aggregate_type
        model_name, pk_attr, tenant_optional = self._SPECS[aggregate_type]
        self.model = getattr(_models, model_name)
        self.pk_attr = pk_attr
        self.tenant_optional = tenant_optional

    async def get(self, session: AsyncSession, tenant_id: str,
                  doc_id: str) -> Optional[Dict[str, Any]]:
        row = await session.get(self.model, doc_id)
        if row is None:
            return None
        # Tenant isolation (unless this aggregate may legitimately lack one).
        if not self.tenant_optional and row.tenant_id != tenant_id:
            return None
        return dict(row.document or {})

    async def get_any(self, session: AsyncSession,
                      doc_id: str) -> Optional[Dict[str, Any]]:
        """Get a document by primary key WITHOUT a tenant filter.

        For globally-unique-id lookups where the tenant is derived *from* the
        row (e.g. webhook channel resolution: the caller has not yet
        established tenant identity). Returns the verbatim document or None.
        """
        row = await session.get(self.model, doc_id)
        return dict(row.document or {}) if row is not None else None

    async def find_one(self, session: AsyncSession, tenant_id: str, *,
                       term_filters: Optional[Dict[str, Any]] = None,
                       ) -> Optional[Dict[str, Any]]:
        """Return the first tenant-scoped document matching ``term_filters``.

        Mirrors the ES "first hit" lookups (e.g. the tenant's single
        dispatcher channel: ``channel_type == 'dispatcher'``). Document-field
        equality only; tenant isolation enforced via the typed column.
        """
        where = []
        if not self.tenant_optional:
            where.append(self.model.tenant_id == tenant_id)
        for key, value in (term_filters or {}).items():
            if value is None:
                continue
            where.append(self._doc_field(key) == value)
        row = (
            await session.execute(select(self.model).where(*where).limit(1))
        ).scalars().first()
        if row is None:
            return None
        if not self.tenant_optional and row.tenant_id != tenant_id:
            return None
        return dict(row.document or {})

    def _doc_field(self, name: str):
        """A comparable/orderable handle to a *document* field.

        Always reads from the verbatim JSON ``document`` column (never the typed
        mirror columns) so search ordering/filtering matches Elasticsearch,
        which operates on the document. Notably the typed ``created_at`` is the
        mirror-insert time, whereas ES sorts on the document's business
        ``created_at`` / ``scheduled_time`` — so we must go through the JSON
        accessor here. Compiles to ``JSON_EXTRACT`` on SQLite and ``->>`` on
        PostgreSQL, so the same code runs in tests and against the container.
        """
        return self.model.document[name].as_string()

    async def search(self, session: AsyncSession, tenant_id: str, *,
                     term_filters: Optional[Dict[str, Any]] = None,
                     in_filters: Optional[Dict[str, list]] = None,
                     bool_filters: Optional[Dict[str, bool]] = None,
                     range_field: Optional[str] = None,
                     range_gte: Optional[str] = None,
                     range_lte: Optional[str] = None,
                     range_lt: Optional[str] = None,
                     exists_fields: Optional[List[str]] = None,
                     text_query: Optional[str] = None,
                     text_fields: Optional[List[str]] = None,
                     sort_field: str = "created_at",
                     sort_order: str = "desc",
                     page: int = 1,
                     size: int = _DEFAULT_PAGE_LIMIT) -> Dict[str, Any]:
        """Offset-paginated search over the document, matching the ES contract.

        Returns ``{"items": [...verbatim docs...], "total": int, "page": int,
        "size": int}``. ``term_filters`` are exact-match on document fields;
        ``in_filters`` match a field against a set of values (ES ``terms``);
        ``bool_filters`` match a JSON boolean field; ``range_field`` applies an
        inclusive ``>= range_gte`` / ``<= range_lte`` (or exclusive ``<
        range_lt``) string comparison (ISO-8601 timestamps sort lexically ==
        chronologically); ``exists_fields`` require the document field to be
        present and non-null (ES ``exists``). ``text_query`` + ``text_fields``
        apply a case-insensitive substring (``ILIKE %q%``) match ORed across the
        named document fields — the Postgres analogue of the ES ``wildcard``
        free-text search, giving the same "contains" semantics on both read
        paths. Cross-tenant rows are excluded via the typed, indexed
        ``tenant_id``.
        """
        if page < 1:
            page = 1
        if size <= 0:
            size = _DEFAULT_PAGE_LIMIT
        size = _clamp(size)

        where = []
        if not self.tenant_optional:
            where.append(self.model.tenant_id == tenant_id)
        for key, value in (term_filters or {}).items():
            if value is None:
                continue
            where.append(self._doc_field(key) == value)
        for key, values in (in_filters or {}).items():
            if not values:
                continue
            where.append(self._doc_field(key).in_(list(values)))
        for key, value in (bool_filters or {}).items():
            if value is None:
                continue
            where.append(self.model.document[key].as_boolean() == bool(value))
        for field in (exists_fields or []):
            where.append(self._doc_field(field).is_not(None))
        if range_field and range_gte is not None:
            where.append(self._doc_field(range_field) >= range_gte)
        if range_field and range_lte is not None:
            where.append(self._doc_field(range_field) <= range_lte)
        if range_field and range_lt is not None:
            where.append(self._doc_field(range_field) < range_lt)

        # Free-text "contains" across the named document fields. Escape the
        # LIKE metacharacters so a user typing % or _ matches them literally.
        if text_query and text_query.strip() and text_fields:
            clauses = [
                _ilike_contains(self._doc_field(field), text_query)
                for field in text_fields
            ]
            where.append(or_(*[c for c in clauses if c is not None]))

        total = (
            await session.execute(
                select(func.count()).select_from(self.model).where(*where)
            )
        ).scalar_one()

        order_expr = self._doc_field(sort_field)
        order_expr = order_expr.desc() if sort_order == "desc" else order_expr.asc()
        stmt = (
            select(self.model)
            .where(*where)
            .order_by(order_expr, getattr(self.model, self.pk_attr).asc())
            .offset((page - 1) * size)
            .limit(size)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        return {
            "items": [dict(r.document or {}) for r in rows],
            "total": int(total),
            "page": page,
            "size": size,
        }

    async def search_all_tenants(
        self, session: AsyncSession, *,
        term_filters: Optional[Dict[str, Any]] = None,
        in_filters: Optional[Dict[str, list]] = None,
        bool_filters: Optional[Dict[str, bool]] = None,
        range_field: Optional[str] = None,
        range_gte: Optional[str] = None,
        range_lte: Optional[str] = None,
        range_lt: Optional[str] = None,
        exists_fields: Optional[List[str]] = None,
        sort_field: str = "created_at",
        sort_order: str = "asc",
        size: int = _DEFAULT_PAGE_LIMIT,
    ) -> List[Dict[str, Any]]:
        """CROSS-TENANT document search for system-level background sweeps.

        The autonomous monitor agents (job SLA, delay response, SLA guardian,
        truck fuel, …) run a single system-wide ES query with no tenant filter
        and then dispatch per-tenant internally. This reproduces that exact
        access pattern over the migrated current-state aggregates: same
        ``term`` / ``terms`` / ``bool`` / ``range`` / ``exists`` filter set and
        document-field sort as :meth:`search`, but WITHOUT the tenant clause.
        Returns the matching verbatim documents (capped at ``size``), ordered
        by ``sort_field``. NOT for request-path reads — those must stay
        tenant-scoped.
        """
        if size <= 0:
            size = _DEFAULT_PAGE_LIMIT
        size = _clamp(size)

        where = []
        for key, value in (term_filters or {}).items():
            if value is None:
                continue
            where.append(self._doc_field(key) == value)
        for key, values in (in_filters or {}).items():
            if not values:
                continue
            where.append(self._doc_field(key).in_(list(values)))
        for key, value in (bool_filters or {}).items():
            if value is None:
                continue
            where.append(self.model.document[key].as_boolean() == bool(value))
        for field in (exists_fields or []):
            where.append(self._doc_field(field).is_not(None))
        if range_field and range_gte is not None:
            where.append(self._doc_field(range_field) >= range_gte)
        if range_field and range_lte is not None:
            where.append(self._doc_field(range_field) <= range_lte)
        if range_field and range_lt is not None:
            where.append(self._doc_field(range_field) < range_lt)

        order_expr = self._doc_field(sort_field)
        order_expr = order_expr.desc() if sort_order == "desc" else order_expr.asc()
        stmt = (
            select(self.model)
            .where(*where)
            .order_by(order_expr, getattr(self.model, self.pk_attr).asc())
            .limit(size)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        return [dict(r.document or {}) for r in rows]

    async def list(self, session: AsyncSession, tenant_id: str, *,
                   filters: Optional[Dict[str, Any]] = None,
                   cursor: Optional[str] = None,
                   limit: int = _DEFAULT_PAGE_LIMIT) -> Dict[str, Any]:
        limit = _clamp(limit)
        pk_col = getattr(self.model, self.pk_attr)
        sort_col = self.model.created_at

        where = []
        if not self.tenant_optional:
            where.append(self.model.tenant_id == tenant_id)
        # Filters map to typed columns when present on the model.
        for key, value in (filters or {}).items():
            if value is None:
                continue
            col = getattr(self.model, key, None)
            if col is not None:
                where.append(col == value)

        # Resolve the cursor row's sort key for a stable (created_at DESC,
        # pk ASC) keyset page — identical contract to the commerce read repos.
        if cursor:
            cur = (
                await session.execute(
                    select(sort_col, pk_col).where(pk_col == cursor)
                )
            ).first()
            if cur is not None:
                cur_sort, cur_id = cur
                where.append(
                    or_(
                        sort_col < cur_sort,
                        and_(sort_col == cur_sort, pk_col > cur_id),
                    )
                )

        stmt = (
            select(self.model)
            .where(*where)
            .order_by(sort_col.desc(), pk_col.asc())
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())

        items = [dict(r.document or {}) for r in rows]
        next_cursor = None
        if len(rows) == limit and rows:
            next_cursor = getattr(rows[-1], self.pk_attr)
        return {"items": items, "next_cursor": next_cursor, "limit": limit}

    async def fetch_for_aggregation(
        self, session: AsyncSession, tenant_id: str, *,
        term_filters: Optional[Dict[str, Any]] = None,
        in_filters: Optional[Dict[str, list]] = None,
        bool_filters: Optional[Dict[str, bool]] = None,
        exists_fields: Optional[List[str]] = None,
        range_field: Optional[str] = None,
        range_gte: Optional[str] = None,
        range_lte: Optional[str] = None,
        cap: int = 50_000,
    ) -> List[Dict[str, Any]]:
        """Return every matching verbatim document for in-Python aggregation.

        The metrics/analytics endpoints aggregate over the migrated
        ``jobs_current`` / ``shipments_current`` rows. Rather than pushing
        GROUP BY into portable SQL (which would diverge from the ES
        ``date_histogram`` calendar bucketing and the Python duration math
        already used), we pull the matching documents and reuse the exact same
        post-processing the ES path runs — guaranteeing byte-identical output.
        Tenant-scoped; capped to ``cap`` rows as a safety bound (per-tenant
        counts are modest).
        """
        where = []
        if not self.tenant_optional:
            where.append(self.model.tenant_id == tenant_id)
        for key, value in (term_filters or {}).items():
            if value is None:
                continue
            where.append(self._doc_field(key) == value)
        for key, values in (in_filters or {}).items():
            if not values:
                continue
            where.append(self._doc_field(key).in_(list(values)))
        for key, value in (bool_filters or {}).items():
            if value is None:
                continue
            where.append(self.model.document[key].as_boolean() == bool(value))
        for field in (exists_fields or []):
            where.append(self._doc_field(field).is_not(None))
        if range_field and range_gte is not None:
            where.append(self._doc_field(range_field) >= range_gte)
        if range_field and range_lte is not None:
            where.append(self._doc_field(range_field) <= range_lte)

        stmt = select(self.model).where(*where).limit(cap)
        rows = (await session.execute(stmt)).scalars().all()
        return [dict(r.document or {}) for r in rows]

    async def list_sorted(
        self, session: AsyncSession, tenant_id: str, *,
        term_filters: Optional[Dict[str, Any]] = None,
        sort_doc_field: str = "created_at",
        sort_order: str = "asc",
        cursor: Optional[str] = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> Dict[str, Any]:
        """Keyset page sorted by a *document* field then pk, both directions.

        Reproduces the ES ``sort: [{<doc_field>: <order>}, {<pk>: asc}]`` +
        ``search_after=[cursor, cursor]`` contract used by aggregates whose
        list order is a document field (e.g. asset_certifications sorted by
        ``expiry_date asc, cert_id asc``). The cursor is the trailing row's pk;
        we resolve its sort value to build a strict ``(sort, pk)`` boundary so
        pages stay contiguous. Returns ``{items, next_cursor, limit}`` — the
        same envelope those services emit.
        """
        limit = _clamp(limit)
        pk_col = getattr(self.model, self.pk_attr)
        sort_expr = self._doc_field(sort_doc_field)
        ascending = sort_order != "desc"

        where = []
        if not self.tenant_optional:
            where.append(self.model.tenant_id == tenant_id)
        for key, value in (term_filters or {}).items():
            if value is None:
                continue
            where.append(self._doc_field(key) == value)

        if cursor:
            cur = (
                await session.execute(
                    select(sort_expr, pk_col).where(pk_col == cursor)
                )
            ).first()
            if cur is not None:
                cur_sort, cur_id = cur
                if ascending:
                    where.append(
                        or_(sort_expr > cur_sort,
                            and_(sort_expr == cur_sort, pk_col > cur_id))
                    )
                else:
                    where.append(
                        or_(sort_expr < cur_sort,
                            and_(sort_expr == cur_sort, pk_col > cur_id))
                    )

        order_sort = sort_expr.asc() if ascending else sort_expr.desc()
        stmt = (
            select(self.model)
            .where(*where)
            .order_by(order_sort, pk_col.asc())
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        items = [dict(r.document or {}) for r in rows]
        next_cursor = None
        if len(rows) == limit and rows:
            next_cursor = getattr(rows[-1], self.pk_attr)
        return {"items": items, "next_cursor": next_cursor, "limit": limit}
