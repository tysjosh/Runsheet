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

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from persistence.models import AccountORM, CustomerORM, InvoiceORM, PaymentORM
from persistence.projections import (
    account_to_doc,
    customer_to_doc,
    invoice_to_doc,
    payment_to_doc,
)

logger = logging.getLogger(__name__)

_DEFAULT_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 200


def _clamp(limit: int) -> int:
    if limit < 1:
        return _DEFAULT_PAGE_LIMIT
    return min(limit, _MAX_PAGE_LIMIT)


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
                   status: Optional[str] = None, cursor: Optional[str] = None,
                   limit: int = _DEFAULT_PAGE_LIMIT) -> Dict[str, Any]:
        limit = _clamp(limit)
        filters = [CustomerORM.status == status] if status else []
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
        "shipment": ("ShipmentCurrentORM", "shipment_id", False),
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
    }

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
