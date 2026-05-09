"""Payment, PaymentSource, PaymentMethod, PaymentStatus Pydantic models.

Defines the canonical Payment entity for the Commerce Backbone.
Fields align with the ``payments_current`` ES mapping (design §3.5).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from services.time_utils import utcnow


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PaymentSource(str, Enum):
    """System-of-record identifier for a Payment (Req 6.1–6.4, 5.5).

    - stripe: Stripe auto-charge (Req 6.1)
    - qbo: QuickBooks Online sync_pull (Req 6.2)
    - manual: Tenant admin manual entry (Req 6.3)
    - account_credit: Synthetic payment from overpayment surplus (Req 6.4)
    - void_cascade: Auto-reversal on forced void (Req 5.5)
    """

    STRIPE = "stripe"
    QBO = "qbo"
    MANUAL = "manual"
    ACCOUNT_CREDIT = "account_credit"
    VOID_CASCADE = "void_cascade"


class PaymentMethod(str, Enum):
    """Payment instrument type (Req 6.1–6.4).

    - card: Stripe card charge (Req 6.1)
    - ach: ACH transfer (Req 6.1, 6.2)
    - wire: Wire transfer (Req 6.2)
    - check: Check payment (Req 6.2)
    - credit_balance: Synthetic from overpayment surplus (Req 6.4)
    - other: Catch-all for unclassified methods (Req 6.2)
    """

    CARD = "card"
    ACH = "ach"
    WIRE = "wire"
    CHECK = "check"
    CREDIT_BALANCE = "credit_balance"
    OTHER = "other"


class PaymentStatus(str, Enum):
    """Lifecycle status for a Payment (Req 6.6).

    - applied: Payment is active and applied to an invoice.
    - reversed: Payment has been reversed (Stripe refund, QBO delete, void cascade).
    """

    APPLIED = "applied"
    REVERSED = "reversed"


# ---------------------------------------------------------------------------
# ID generator
# ---------------------------------------------------------------------------


def _generate_payment_id() -> str:
    """Generate a payment_id of shape ``pay_<uuid4>`` (Req 6.1)."""
    return f"pay_{uuid4()}"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Payment(BaseModel):
    """Canonical Payment record stored in ``payments_current``.

    Represents a single inbound settlement (Stripe charge, ACH, check, wire,
    or synthetic credit-balance application) applied to one invoice.

    All monetary fields are typed as ``int`` (Constraint C1).
    Timestamps use ``utcnow()`` from ``services.time_utils`` (Constraint C2).
    """

    payment_id: str = Field(
        default_factory=_generate_payment_id,
        description="Server-assigned identifier of shape pay_<uuid4> (Req 6.1)",
    )
    tenant_id: str = Field(..., description="Tenant identifier for data isolation")
    invoice_id: str = Field(..., description="Invoice this payment is applied to")
    account_id: str = Field(..., description="Billing account identifier")
    amount_cents: int = Field(
        ..., description="Payment amount in integer cents (Constraint C1)"
    )
    source: PaymentSource = Field(
        ..., description="System-of-record: stripe|qbo|manual|account_credit|void_cascade",
    )
    method: PaymentMethod = Field(
        ..., description="Payment instrument: card|ach|wire|check|credit_balance|other",
    )
    external_id: Optional[str] = Field(
        default=None,
        description="External system identifier (e.g. Stripe charge ID, QBO payment ID)",
    )
    reference: Optional[str] = Field(
        default=None,
        description="Free-text reference (check number, wire memo, etc.)",
    )
    status: PaymentStatus = Field(
        default=PaymentStatus.APPLIED,
        description="Payment lifecycle status: applied|reversed (Req 6.6)",
    )
    received_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when the payment was received externally (UTC)",
    )
    applied_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp when the payment was applied to the invoice (UTC)",
    )
    reversed_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when the payment was reversed (UTC, null if not reversed)",
    )
