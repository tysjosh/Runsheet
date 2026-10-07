"""Portal invoice routes (design §3, §5; R5). Read-only.

* ``GET /api/portal/invoices`` — the customer's non-draft invoices, newest first.
* ``GET /api/portal/invoices/export`` — the same list as CSV (download limit).
  Declared before ``/{invoice_id}``, as in the commerce router.
* ``GET /api/portal/invoices/{invoice_id}`` — one invoice.
* ``GET /api/portal/invoices/{invoice_id}/pdf`` — its PDF (download limit).

Every route depends on :func:`require_portal_invoicing`. Data comes only
through ``portal.services``; this module imports no store.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from config.settings import get_settings
from errors.codes import ErrorCode
from errors.exceptions import AppException
from middleware.rate_limiter import limiter
from portal.api._authz import (
    PORTAL_READ_LIMIT,
    PORTAL_READ_SCOPE,
    PortalScope,
    portal_rate_key,
    reject_scope_params,
    require_portal_customer,
)
from portal.models import PortalInvoiceEnvelope, PortalInvoiceListEnvelope, is_path_id
from portal.services.portal_invoice_service import (
    InvoiceFilters,
    PortalInvoiceService,
    get_configured_invoice_service,
)
from portal.services.scoped_readers import invoice_not_found
from services.csv_export import EXPORT_RATE_LIMIT

router = APIRouter(
    prefix="/api/portal",
    tags=["portal"],
    dependencies=[Depends(reject_scope_params)],
)


class PortalInvoiceStatus(str, Enum):
    """The list filter: every invoice status except ``draft`` (§3.2)."""

    open = "open"
    partial = "partial"
    paid = "paid"
    overdue = "overdue"
    void = "void"


async def require_portal_invoicing(
    scope: PortalScope = Depends(require_portal_customer),
) -> PortalScope:
    """404 ``INVOICING_DISABLED`` when the commerce backbone or invoicing is
    off, or the invoice service isn't configured (R5.6)."""
    settings = get_settings()
    if (
        not settings.commerce_backbone_enabled
        or not settings.commerce_invoicing_enabled
        or get_configured_invoice_service() is None
    ):
        raise AppException(ErrorCode.INVOICING_DISABLED, "Invoices are not available")
    return scope


def _service() -> PortalInvoiceService:
    service = get_configured_invoice_service()
    if service is None:  # pragma: no cover - require_portal_invoicing ran first
        raise AppException(ErrorCode.INVOICING_DISABLED, "Invoices are not available")
    return service


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def _invoice_id(invoice_id: str) -> str:
    """A malformed id answers exactly like an unknown one (§3.2)."""
    if not is_path_id(invoice_id):
        raise invoice_not_found(invoice_id)
    return invoice_id


def _filters(
    status: Optional[PortalInvoiceStatus], start_date: Optional[str], end_date: Optional[str]
) -> InvoiceFilters:
    return InvoiceFilters(
        status=status.value if status else None, start_date=start_date, end_date=end_date
    )


@router.get("/invoices", response_model=PortalInvoiceListEnvelope)
@limiter.shared_limit(PORTAL_READ_LIMIT, scope=PORTAL_READ_SCOPE, key_func=portal_rate_key)
async def list_portal_invoices(
    request: Request,
    status: Optional[PortalInvoiceStatus] = Query(default=None),
    start_date: Optional[str] = Query(default=None, max_length=64),
    end_date: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=25, ge=1, le=50),
    cursor: Optional[str] = Query(default=None, max_length=256),
    scope: PortalScope = Depends(require_portal_invoicing),
) -> PortalInvoiceListEnvelope:
    """The customer's invoices across all its accounts, newest first (R5.1)."""
    data, next_cursor = await _service().list(
        scope, _filters(status, start_date, end_date), limit=limit, cursor=cursor
    )
    return PortalInvoiceListEnvelope(
        data=data, next_cursor=next_cursor, limit=limit, request_id=_request_id(request)
    )


@router.get("/invoices/export", response_model=None)
@limiter.limit(EXPORT_RATE_LIMIT, key_func=portal_rate_key)
async def export_portal_invoices(
    request: Request,
    status: Optional[PortalInvoiceStatus] = Query(default=None),
    start_date: Optional[str] = Query(default=None, max_length=64),
    end_date: Optional[str] = Query(default=None, max_length=64),
    scope: PortalScope = Depends(require_portal_invoicing),
):
    """CSV of the list with the same filters (R5.4)."""
    return await _service().export(request, scope, _filters(status, start_date, end_date))


@router.get("/invoices/{invoice_id}", response_model=PortalInvoiceEnvelope)
@limiter.shared_limit(PORTAL_READ_LIMIT, scope=PORTAL_READ_SCOPE, key_func=portal_rate_key)
async def get_portal_invoice(
    request: Request,
    invoice_id: str,
    scope: PortalScope = Depends(require_portal_invoicing),
) -> PortalInvoiceEnvelope:
    """One of the customer's invoices; drafts and anyone else's are 404 (R5.2)."""
    invoice = await _service().detail(scope, _invoice_id(invoice_id))
    return PortalInvoiceEnvelope(data=invoice, request_id=_request_id(request))


@router.get("/invoices/{invoice_id}/pdf", response_model=None)
@limiter.limit(EXPORT_RATE_LIMIT, key_func=portal_rate_key)
async def get_portal_invoice_pdf(
    request: Request,
    invoice_id: str,
    scope: PortalScope = Depends(require_portal_invoicing),
) -> Response:
    """The invoice as a PDF attachment (R5.3)."""
    content, filename = await _service().pdf(scope, _invoice_id(invoice_id))
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["require_portal_invoicing", "router"]
