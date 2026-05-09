"""Invoice, InvoiceLineItem, InvoiceStatus, and QBOPushState Pydantic models.

Defines the canonical Invoice entity for the Commerce Backbone.
Fields align with the ``invoices_current`` ES mapping (design §3.4).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from services.time_utils import utcnow


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InvoiceStatus(str, Enum):
    """Allowed lifecycle states for an Invoice (Req 5.2–5.5).

    State machine:
        draft → open → partial → paid
                open → overdue → partial → paid
                any non-terminal → void (terminal)
    """

    DRAFT = "draft"
    OPEN = "open"
    PARTIAL = "partial"
    PAID = "paid"
    OVERDUE = "overdue"
    VOID = "void"


class QBOPushState(str, Enum):
    """QBO push lifecycle states (Req 5.6b).

    Tracks the transport state of an Invoice toward QuickBooks Online.
    """

    PENDING = "pending"
    PUSHED = "pushed"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------


def _generate_invoice_id() -> str:
    """Generate an invoice_id of shape ``inv_<uuid4>`` (Req 5.1)."""
    return f"inv_{uuid4()}"


def _generate_line_id() -> str:
    """Generate a line_id of shape ``line_<uuid4>``."""
    return f"line_{uuid4()}"


# ---------------------------------------------------------------------------
# Submodels
# ---------------------------------------------------------------------------


class InvoiceLineItem(BaseModel):
    """A single line item on an Invoice.

    Nested within ``invoices_current.line_items`` (design §3.4).
    All monetary fields are integer cents (Constraint C1).
    """

    line_id: str = Field(
        default_factory=_generate_line_id,
        description="Server-assigned identifier of shape line_<uuid4>",
    )
    product_code: str = Field(
        ..., description="Canonicalized product code for the line item"
    )
    quantity_gallons: float = Field(
        ..., description="Quantity delivered in gallons"
    )
    unit_price_cents: int = Field(
        ..., description="Unit price in integer cents (Constraint C1)"
    )
    subtotal_cents: int = Field(
        ..., description="Line subtotal in integer cents (quantity × unit_price_cents)"
    )


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Invoice(BaseModel):
    """Canonical Invoice record stored in ``invoices_current``.

    Event-sourced: every state transition is first appended to
    ``invoice_events`` before this projection is updated (Constraint C7).

    All monetary fields are typed as ``int`` (Constraint C1).
    Timestamps use ``utcnow()`` from ``services.time_utils`` (Constraint C2).
    Includes ``external_refs`` for QBO / Stripe correlation.
    """

    invoice_id: str = Field(
        default_factory=_generate_invoice_id,
        description="Server-assigned identifier of shape inv_<uuid4> (Req 5.1)",
    )
    tenant_id: str = Field(..., description="Tenant identifier for data isolation")
    customer_id: str = Field(..., description="Parent customer identifier")
    account_id: str = Field(..., description="Billing account identifier")
    order_id: Optional[str] = Field(
        default=None,
        description="Source fuel order identifier (null for QBO-backfilled invoices)",
    )
    invoice_number: Optional[str] = Field(
        default=None,
        description="Human-readable per-tenant sequential invoice number",
    )
    status: InvoiceStatus = Field(
        default=InvoiceStatus.DRAFT,
        description="Invoice lifecycle status (Req 5.2): draft|open|partial|paid|overdue|void",
    )
    total_cents: int = Field(
        default=0,
        description="Total invoice amount in integer cents",
    )
    amount_paid_cents: int = Field(
        default=0,
        description="Total amount paid against this invoice in integer cents",
    )
    remaining_cents: int = Field(
        default=0,
        description="Remaining balance in integer cents (total_cents - amount_paid_cents)",
    )
    tax_cents: int = Field(
        default=0,
        description="Tax amount in integer cents",
    )
    subtotal_cents: int = Field(
        default=0,
        description="Subtotal before tax in integer cents",
    )
    line_items: List[InvoiceLineItem] = Field(
        default_factory=list,
        description="Nested line items for this invoice",
    )
    issued_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when the invoice was issued/finalized (UTC)",
    )
    due_date: Optional[date] = Field(
        default=None,
        description="Payment due date",
    )
    finalized_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when the invoice transitioned from draft to open (UTC)",
    )
    voided_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when the invoice was voided (UTC)",
    )
    void_reason: Optional[str] = Field(
        default=None,
        description="Reason provided when voiding the invoice",
    )
    qbo_push_state: QBOPushState = Field(
        default=QBOPushState.PENDING,
        description="QBO push lifecycle state (Req 5.6b): pending|pushed|retry|dead_letter",
    )
    qbo_push_attempts: int = Field(
        default=0,
        description="Number of QBO push attempts made",
    )
    qbo_push_last_error: Optional[str] = Field(
        default=None,
        description="Last error message from a failed QBO push attempt",
    )
    external_refs: Dict[str, Any] = Field(
        default_factory=dict,
        description="External system references for QBO/Stripe correlation (e.g. {qbo: 'inv:45', stripe: 'ch_...'})",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp when the record was created (UTC)",
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp of last modification (UTC)",
    )
