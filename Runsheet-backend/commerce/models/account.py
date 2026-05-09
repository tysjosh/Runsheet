"""Account Pydantic model.

Defines the canonical Account (billing unit) entity for the Commerce Backbone.
Fields align with the ``accounts_current`` ES mapping (design §3.2).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from services.time_utils import utcnow


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AccountStatus(str, Enum):
    """Allowed lifecycle states for an Account record."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class CreditState(str, Enum):
    """Credit state machine values (design §4.2)."""

    OK = "ok"
    HOLD = "hold"
    OVERRIDE = "override"


class AccountTier(str, Enum):
    """Pricing tier assigned to an Account."""

    PLATINUM = "platinum"
    GOLD = "gold"
    SILVER = "silver"
    BRONZE = "bronze"
    DEFAULT = "default"


class PaymentMethodPreference(str, Enum):
    """Preferred payment method for the Account."""

    INVOICE = "invoice"
    ACH = "ach"
    CARD = "card"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_NET_TERMS_DAYS = {0, 7, 15, 30, 45, 60, 90}
_MAX_CREDIT_LIMIT_CENTS = 999_999_999_999  # hundred-billion-dollar ceiling


# ---------------------------------------------------------------------------
# Submodels
# ---------------------------------------------------------------------------


class BillingAddress(BaseModel):
    """Billing address submodel for an Account."""

    line1: str = Field(..., description="Street address line 1")
    line2: Optional[str] = Field(default=None, description="Street address line 2")
    city: str = Field(..., description="City")
    state: Optional[str] = Field(default=None, description="State or province")
    postal_code: str = Field(..., description="Postal / ZIP code")
    country: str = Field(default="US", description="ISO 3166-1 alpha-2 country code")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def _generate_account_id() -> str:
    """Generate an account_id of shape ``acct_<uuid4>``."""
    return f"acct_{uuid4()}"


class Account(BaseModel):
    """Canonical Account (billing unit) record stored in ``accounts_current``.

    Validators enforce:
    - ``net_terms_days`` is in {0, 7, 15, 30, 45, 60, 90}.
    - ``credit_limit_cents`` is >= 0 and <= 999,999,999,999.
    - All ``_cents`` fields are typed as ``int``.
    """

    account_id: str = Field(
        default_factory=_generate_account_id,
        description="Server-assigned identifier of shape acct_<uuid4>",
    )
    tenant_id: str = Field(..., description="Tenant identifier for data isolation")
    customer_id: str = Field(..., description="Parent customer identifier")
    display_name: str = Field(..., description="Account display name")
    status: AccountStatus = Field(
        default=AccountStatus.ACTIVE,
        description="Lifecycle status: active, suspended, or closed",
    )
    credit_limit_cents: int = Field(
        default=0,
        description="Credit limit in cents. 0 means cash on delivery only.",
    )
    open_balance_cents: int = Field(
        default=0,
        description="Sum of outstanding invoice amounts in cents",
    )
    available_credit_cents: int = Field(
        default=0,
        description="credit_limit_cents minus open_balance_cents",
    )
    credit_balance_cents: int = Field(
        default=0,
        description="Overpayment surplus accrued on the account in cents",
    )
    credit_state: CreditState = Field(
        default=CreditState.OK,
        description="Current credit state: ok, hold, or override",
    )
    credit_override_expires_at: Optional[datetime] = Field(
        default=None,
        description="Expiry timestamp for an active credit override",
    )
    net_terms_days: int = Field(
        default=30,
        description="Payment terms in days. Must be in {0, 7, 15, 30, 45, 60, 90}.",
    )
    tier: AccountTier = Field(
        default=AccountTier.DEFAULT,
        description="Pricing tier: platinum, gold, silver, bronze, or default",
    )
    billing_address: Optional[BillingAddress] = Field(
        default=None,
        description="Billing address for the account",
    )
    payment_method_preference: PaymentMethodPreference = Field(
        default=PaymentMethodPreference.INVOICE,
        description="Preferred payment method: invoice, ach, or card",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp when the record was created (UTC)",
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp of last modification (UTC)",
    )
    external_refs: Dict[str, Any] = Field(
        default_factory=dict,
        description="External system references (e.g. {qbo: 'acct:456'})",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("net_terms_days")
    @classmethod
    def net_terms_days_must_be_valid(cls, v: int) -> int:
        """Reject net_terms_days values not in the allowed set."""
        if v not in _VALID_NET_TERMS_DAYS:
            raise ValueError(
                f"net_terms_days must be one of {sorted(_VALID_NET_TERMS_DAYS)}, got {v}"
            )
        return v

    @field_validator("credit_limit_cents")
    @classmethod
    def credit_limit_cents_must_be_in_range(cls, v: int) -> int:
        """Reject credit_limit_cents outside [0, 999_999_999_999]."""
        if v < 0:
            raise ValueError("credit_limit_cents must be >= 0")
        if v > _MAX_CREDIT_LIMIT_CENTS:
            raise ValueError(
                f"credit_limit_cents must be <= {_MAX_CREDIT_LIMIT_CENTS}"
            )
        return v
