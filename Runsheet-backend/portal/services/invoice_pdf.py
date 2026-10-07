"""Customer-portal invoice PDF (design §5, PD12).

Rendered with the low-level reportlab canvas API, as
``services/bol_service.BOLService._render_pdf`` does. The input is the
:class:`~portal.models.PortalInvoice` projection, so a field that isn't
projected can't be rendered.
"""
from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any, List, Optional

from portal.models import PortalInvoice, PortalInvoiceLineItem

#: Line items per page.
LINES_PER_PAGE = 30


def money(cents: Optional[int]) -> str:
    """``$1,234.56`` (``-$1.00`` for a negative amount); blank when unknown."""
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    whole, rem = divmod(abs(int(cents)), 100)
    return f"{sign}${whole:,}.{rem:02d}"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC") if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _gallons(value: Optional[float]) -> str:
    return "" if value is None else f"{value:,.2f}"


def _pages(lines: List[PortalInvoiceLineItem]) -> List[List[PortalInvoiceLineItem]]:
    if not lines:
        return [[]]
    return [lines[i:i + LINES_PER_PAGE] for i in range(0, len(lines), LINES_PER_PAGE)]


def render_invoice_pdf(
    invoice: PortalInvoice,
    *,
    supplier_name: str,
    customer_display_name: str,
) -> bytes:
    """Render the invoice as a letter-size PDF and return its bytes."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as rl_canvas

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=letter)
    c.setTitle(f"Invoice {invoice.invoice_number or invoice.invoice_id}")
    _width, height = letter
    pages = _pages(list(invoice.line_items))

    for page_no, page_lines in enumerate(pages, start=1):
        y = height - 60
        c.setFont("Helvetica-Bold", 16)
        c.drawString(40, y, "INVOICE")
        c.setFont("Helvetica", 9)
        c.drawRightString(572, y, f"Page {page_no} of {len(pages)}")
        y -= 20
        c.setFont("Helvetica", 10)

        def kv(label: str, value: Any) -> None:
            nonlocal y
            c.setFont("Helvetica-Bold", 10)
            c.drawString(40, y, f"{label}:")
            c.setFont("Helvetica", 10)
            c.drawString(160, y, _text(value))
            y -= 14

        kv("From", supplier_name)
        kv("Bill to", customer_display_name)
        kv("Account", invoice.account_display_name)
        kv("Invoice number", invoice.invoice_number or invoice.invoice_id)
        kv("Status", invoice.status_label)
        kv("Issued", invoice.issued_at)
        kv("Due", invoice.due_date)
        if invoice.delivery is not None:
            kv("Delivered", invoice.delivery.delivered_at)
            kv("Delivered gallons", _gallons(invoice.delivery.actual_gallons))
            kv("Ticket number", invoice.delivery.ticket_number)

        # Line items.
        y -= 10
        c.setFont("Helvetica-Bold", 10)
        c.drawString(40, y, "Product")
        c.drawRightString(330, y, "Gallons")
        c.drawRightString(450, y, "Unit price")
        c.drawRightString(572, y, "Subtotal")
        y -= 4
        c.line(40, y, 572, y)
        y -= 12
        c.setFont("Helvetica", 10)
        for line in page_lines:
            c.drawString(40, y, _text(line.product_code))
            c.drawRightString(330, y, _gallons(line.quantity_gallons))
            c.drawRightString(450, y, money(line.unit_price_cents))
            c.drawRightString(572, y, money(line.subtotal_cents))
            y -= 14

        if page_no == len(pages):
            y -= 6
            c.line(330, y, 572, y)
            y -= 14
            for label, cents in (
                ("Subtotal", invoice.subtotal_cents),
                ("Tax", invoice.tax_cents),
                ("Total", invoice.total_cents),
                ("Paid", invoice.amount_paid_cents),
                ("Remaining", invoice.remaining_cents),
            ):
                c.setFont("Helvetica-Bold", 10)
                c.drawRightString(450, y, label)
                c.setFont("Helvetica", 10)
                c.drawRightString(572, y, money(cents))
                y -= 14
        c.showPage()

    c.save()
    return buf.getvalue()


__all__ = ["LINES_PER_PAGE", "money", "render_invoice_pdf"]
