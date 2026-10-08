"""Portal invoices: list, detail, PDF and CSV (design §5, R5, D8).

Reads go only through :class:`~portal.services.scoped_readers.PortalInvoiceReader`
(both ids, ``statuses`` without ``draft``); every response is built by
:func:`~portal.services.projection.project_invoice`.

Configured from ``bootstrap/core.py`` with :func:`wire_portal_invoices` when
the invoice service exists. Unconfigured, the invoice routes answer 404
``INVOICING_DISABLED``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, List, Mapping, Optional, Tuple

from fastapi import Request
from fastapi.responses import StreamingResponse

from errors.codes import ErrorCode
from errors.exceptions import AppException
from portal.models import PortalInvoice
from portal.services.invoice_pdf import render_invoice_pdf
from portal.services.portal_payment_service import latest_payment_attempt, portal_connector
from portal.services.projection import (
    account_display_names,
    invoice_total_gallons,
    project_invoice,
    project_payment_attempt,
)
from portal.services.scoped_readers import PortalInvoiceReader
from services.csv_export import (
    EXPORT_PAGE_SIZE,
    ExportColumn,
    KeysetPage,
    KeysetSource,
    stream_csv_export,
)
from services.date_range import parse_date_range

logger = logging.getLogger(__name__)

PDF_ERROR_MESSAGE = "Invoice PDF could not be generated"
_CENT = Decimal("0.01")


def _dollars(cents: Optional[int]) -> Optional[Decimal]:
    if cents is None:
        return None
    return (Decimal(int(cents)) / 100).quantize(_CENT)


def _col(header: str, value) -> ExportColumn:
    return ExportColumn(header, value)


#: Portal CSV columns (design §5). Money is decimal dollars.
PORTAL_INVOICE_EXPORT_COLUMNS: Tuple[ExportColumn, ...] = (
    _col("invoice_number", lambda r: r.invoice_number or r.invoice_id),
    _col("issued_at", lambda r: r.issued_at),
    _col("due_date", lambda r: r.due_date),
    _col("status", lambda r: r.status_label),
    _col("account", lambda r: r.account_display_name),
    _col("total_gallons", invoice_total_gallons),
    _col("subtotal", lambda r: _dollars(r.subtotal_cents)),
    _col("tax", lambda r: _dollars(r.tax_cents)),
    _col("total", lambda r: _dollars(r.total_cents)),
    _col("paid", lambda r: _dollars(r.amount_paid_cents)),
    _col("remaining", lambda r: _dollars(r.remaining_cents)),
)


def pdf_filename(invoice: PortalInvoice) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", invoice.invoice_number or invoice.invoice_id)[:64]
    return f"invoice_{safe}.pdf"


@dataclass
class InvoiceFilters:
    status: Optional[str]
    start_date: Optional[str]
    end_date: Optional[str]

    def service_kwargs(self) -> dict:
        """Date bounds on invoice ``created_at`` (D8, DV9)."""
        rng = parse_date_range(self.start_date, self.end_date)
        return {
            "status": self.status,
            "created_from": rng.gte,
            "created_before": rng.lt,
            "created_until": rng.lte,
        }


class PortalInvoiceService:
    def __init__(
        self,
        reader: PortalInvoiceReader,
        *,
        account_service: Any = None,
        customer_service: Any = None,
    ) -> None:
        self._reader = reader
        self._accounts = account_service
        self._customers = customer_service

    async def _payments_available(self, scope: Any) -> bool:
        return await portal_connector(scope.tenant_id) is not None

    async def _projector(self, scope: Any):
        names = await account_display_names(self._accounts, scope)
        payments = await self._payments_available(scope)

        def project(doc: Mapping[str, Any]) -> PortalInvoice:
            return project_invoice(doc, account_names=names, payments_available=payments)

        return project

    async def list(
        self, scope: Any, filters: InvoiceFilters, *, limit: int, cursor: Optional[str]
    ) -> Tuple[List[PortalInvoice], Optional[str]]:
        kwargs = filters.service_kwargs()
        page = await self._reader.list(scope, limit=limit, cursor=cursor, **kwargs)
        project = await self._projector(scope)
        return [project(doc) for doc in page.items], page.next_cursor

    @property
    def reader(self) -> PortalInvoiceReader:
        """The scoped invoice reader (payment create reads the invoice fresh)."""
        return self._reader

    async def detail(self, scope: Any, invoice_id: str) -> PortalInvoice:
        doc = await self._reader.get(scope, invoice_id)
        names = await account_display_names(self._accounts, scope)
        payments = await self._payments_available(scope)
        # The newest attempt from PortalPaymentAttemptStore (§5, R6.13).
        attempt = await latest_payment_attempt(scope, str(doc.get("invoice_id") or invoice_id))
        return project_invoice(
            doc,
            account_names=names,
            payments_available=payments,
            payment_attempt=project_payment_attempt(attempt) if attempt else None,
        )

    async def _customer_display_name(self, scope: Any) -> str:
        if self._customers is None:
            return ""
        try:
            customer = await self._customers.get(scope.tenant_id, scope.customer_id)
        except Exception as exc:  # noqa: BLE001 — cosmetic on the PDF
            logger.warning(
                "portal invoice pdf: customer read failed for tenant=%s: %s",
                scope.tenant_id,
                type(exc).__name__,
            )
            return ""
        return str((customer or {}).get("display_name") or "")

    async def pdf(self, scope: Any, invoice_id: str) -> Tuple[bytes, str]:
        """``(pdf_bytes, filename)``; a render error is 500 and not retried."""
        invoice = await self.detail(scope, invoice_id)
        customer_name = await self._customer_display_name(scope)
        try:
            content = render_invoice_pdf(
                invoice,
                # No tenant display name yet: the BOL precedent (B1, DV1).
                supplier_name=scope.tenant_id,
                customer_display_name=customer_name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "portal invoice pdf render failed: %s",
                type(exc).__name__,
                extra={"extra_data": {"invoice_id": invoice_id, "tenant_id": scope.tenant_id}},
            )
            raise AppException(ErrorCode.INTERNAL_ERROR, PDF_ERROR_MESSAGE) from None
        return content, pdf_filename(invoice)

    def _export_source(self, scope: Any, kwargs: dict) -> KeysetSource:
        reader = self._reader
        state: dict = {}

        async def fetch(after, page_size, with_total):
            if "project" not in state:
                state["project"] = await self._projector(scope)
            page = await reader.list(
                scope, limit=page_size, cursor=after[1] if after else None, **kwargs
            )
            rows = [state["project"](doc) for doc in page.items]
            return KeysetPage(
                rows=rows,
                raw_count=page_size if page.next_cursor else len(page.items),
                total=None,
                last_key=("", page.next_cursor) if page.next_cursor else None,
            )

        async def count() -> int:
            return await reader.count(scope, **kwargs)

        return KeysetSource(fetch, page_size=EXPORT_PAGE_SIZE, count=count)

    async def export(
        self, request: Request, scope: Any, filters: InvoiceFilters
    ) -> StreamingResponse:
        kwargs = filters.service_kwargs()
        return await stream_csv_export(
            request=request,
            tenant=scope.tenant,
            export_type="portal_invoices",
            columns=PORTAL_INVOICE_EXPORT_COLUMNS,
            source=self._export_source(scope, kwargs),
            filters={
                "status": filters.status,
                "start_date": filters.start_date,
                "end_date": filters.end_date,
            },
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_service: Optional[PortalInvoiceService] = None


def configure_portal_invoices(service: Optional[PortalInvoiceService]) -> None:
    global _service
    _service = service


def get_configured_invoice_service() -> Optional[PortalInvoiceService]:
    return _service


def wire_portal_invoices(
    *, invoice_service: Any, account_service: Any = None, customer_service: Any = None
) -> PortalInvoiceService:
    """Build the reader and service over the commerce services and register it."""
    service = PortalInvoiceService(
        PortalInvoiceReader(invoice_service),
        account_service=account_service,
        customer_service=customer_service,
    )
    configure_portal_invoices(service)
    return service


__all__ = [
    "InvoiceFilters",
    "PDF_ERROR_MESSAGE",
    "PORTAL_INVOICE_EXPORT_COLUMNS",
    "PortalInvoiceService",
    "configure_portal_invoices",
    "get_configured_invoice_service",
    "pdf_filename",
    "wire_portal_invoices",
]
