"""InvoiceEvent, AccountEvent, InvoiceEventType, AccountEventType.

Defines the append-only event models for the Commerce Backbone event-sourcing
layer. Every state transition on Invoice and Account goes through these events
before the corresponding ``_current`` projection is updated.

Fields align with the ``invoice_events`` (design §3.4) and ``account_events``
(design §3.6) ES mappings.

Every event carries ``tenant_id``, ``occurred_at``, ``actor``, and
``sequence_number`` for idempotent projection replay (Constraint C7).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict
from uuid import uuid4

from pydantic import BaseModel, Field

from services.time_utils import utcnow


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InvoiceEventType(str, Enum):
    """Allowed event types for the ``invoice_events`` append-only log.

    Maps to the ``event_type`` keyword field in design §3.4.
    """

    CREATED = "created"
    FINALIZED = "finalized"
    PAYMENT_APPLIED = "payment_applied"
    VOIDED = "voided"
    OVERDUE_MARKED = "overdue_marked"
    PAYMENT_REVERSED = "payment_reversed"


class AccountEventType(str, Enum):
    """Allowed event types for the ``account_events`` append-only log.

    Maps to the ``event_type`` keyword field in design §3.6.
    """

    CREATED = "created"
    CREDIT_STATE_CHANGED = "credit_state_changed"
    OVERRIDE_APPLIED = "override_applied"
    OVERRIDE_EXPIRED = "override_expired"
    CREDIT_BALANCE_APPLIED = "credit_balance_applied"


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------


def _generate_invoice_event_id() -> str:
    """Generate an event_id of shape ``ievt_<uuid4>`` for invoice events."""
    return f"ievt_{uuid4()}"


def _generate_account_event_id() -> str:
    """Generate an event_id of shape ``aevt_<uuid4>`` for account events."""
    return f"aevt_{uuid4()}"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class InvoiceEvent(BaseModel):
    """Append-only event record stored in ``invoice_events``.

    Every Invoice state transition is first appended as an InvoiceEvent
    before the ``invoices_current`` projection is updated (Constraint C7).

    The projection update is idempotent via ``sequence_number`` — replayed
    events never double-apply.

    Timestamps use ``utcnow()`` from ``services.time_utils`` (Constraint C2).
    """

    event_id: str = Field(
        default_factory=_generate_invoice_event_id,
        description="Server-assigned identifier of shape ievt_<uuid4>",
    )
    invoice_id: str = Field(
        ..., description="Invoice this event belongs to"
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )
    event_type: InvoiceEventType = Field(
        ...,
        description="Event type: created|finalized|payment_applied|voided|overdue_marked|payment_reversed",
    )
    payload: Dict[str, Any] = Field(
        default_factory=dict,
        description="Event-specific payload data (e.g. payment amount, void reason)",
    )
    occurred_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp when the event occurred (UTC, Constraint C2)",
    )
    actor: str = Field(
        ...,
        description="Who triggered the event: user_id, 'system', 'qbo', or 'stripe'",
    )
    sequence_number: int = Field(
        ...,
        description="Monotonically increasing sequence for idempotent projection replay",
    )


class AccountEvent(BaseModel):
    """Append-only event record stored in ``account_events``.

    Every Account state-changing operation is recorded as an AccountEvent
    for audit and idempotent projection replay.

    Timestamps use ``utcnow()`` from ``services.time_utils`` (Constraint C2).
    """

    event_id: str = Field(
        default_factory=_generate_account_event_id,
        description="Server-assigned identifier of shape aevt_<uuid4>",
    )
    account_id: str = Field(
        ..., description="Account this event belongs to"
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )
    event_type: AccountEventType = Field(
        ...,
        description="Event type: created|credit_state_changed|override_applied|override_expired|credit_balance_applied",
    )
    payload: Dict[str, Any] = Field(
        default_factory=dict,
        description="Event-specific payload data (e.g. old/new credit state, override details)",
    )
    occurred_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp when the event occurred (UTC, Constraint C2)",
    )
    actor: str = Field(
        ...,
        description="Who triggered the event: user_id, 'system', 'qbo', or 'stripe'",
    )
    sequence_number: int = Field(
        ...,
        description="Monotonically increasing sequence for idempotent projection replay",
    )
