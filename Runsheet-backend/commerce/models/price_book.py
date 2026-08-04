"""PriceBook, PricingRule, PricingScope, and PricingResult Pydantic models.

Defines the canonical pricing entities for the Commerce Backbone.
Fields align with the ``price_books_current`` and ``pricing_rules_current``
ES mappings (design §3.3).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from services.time_utils import utcnow


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PriceBookStatus(str, Enum):
    """Allowed lifecycle states for a PriceBook."""

    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class PricingScopeType(str, Enum):
    """Scope type for a pricing rule's applicability."""

    ACCOUNT = "account"
    TIER = "tier"
    DEFAULT = "default"


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------


def _generate_price_book_id() -> str:
    """Generate a price_book_id of shape ``pb_<uuid4>``."""
    return f"pb_{uuid4()}"


def _generate_rule_id() -> str:
    """Generate a rule_id of shape ``rule_<uuid4>``."""
    return f"rule_{uuid4()}"


# ---------------------------------------------------------------------------
# Submodels
# ---------------------------------------------------------------------------


class PricingScope(BaseModel):
    """Scope definition for a PricingRule.

    Determines whether the rule applies to a specific account, a tier,
    or as a default fallback.
    """

    scope_type: PricingScopeType = Field(
        ..., description="Type of scope: account, tier, or default"
    )
    scope_value: str = Field(
        ...,
        description=(
            "Scope value: account_id for account scope, "
            "tier name for tier scope, 'default' for default scope"
        ),
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PricingRule(BaseModel):
    """A single pricing rule within a PriceBook.

    Stored denormalized in ``pricing_rules_current`` for fast resolution.

    Validators enforce:
    - ``unit_price_cents`` >= 0 (Constraint C1: money is integer cents).
    - ``effective_from`` < ``effective_to`` when both are present.
    - ``product_code`` is non-empty.
    """

    rule_id: str = Field(
        default_factory=_generate_rule_id,
        description="Server-assigned identifier of shape rule_<uuid4>",
    )
    price_book_id: str = Field(
        ..., description="Parent price book identifier"
    )
    tenant_id: str = Field(..., description="Tenant identifier for data isolation")
    product_code: str = Field(
        ..., description="Canonicalized product code for the line item"
    )
    scope_type: PricingScopeType = Field(
        ..., description="Scope type: account, tier, or default"
    )
    scope_value: str = Field(
        ...,
        description="Scope value: account_id, tier name, or 'default'",
    )
    effective_from: datetime = Field(
        ..., description="Start of the rule's effective window (inclusive)"
    )
    effective_to: Optional[datetime] = Field(
        default=None,
        description="End of the rule's effective window (exclusive). null means active indefinitely.",
    )
    min_quantity_gallons: Optional[float] = Field(
        default=None,
        description="Minimum quantity in gallons for this rule to apply (quantity break)",
    )
    unit_price_cents: int = Field(
        ..., description="Rounded whole-cent compatibility unit price"
    )
    unit_price_micros: Optional[int] = Field(
        default=None,
        ge=0,
        description="Exact price per gallon in integer micro-dollars",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp when the rule was created (UTC)",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("product_code")
    @classmethod
    def product_code_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only product codes."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("product_code must not be empty or whitespace-only")
        return stripped

    @field_validator("unit_price_cents")
    @classmethod
    def unit_price_cents_must_be_non_negative(cls, v: int) -> int:
        """Reject negative unit_price_cents values."""
        if v < 0:
            raise ValueError("unit_price_cents must be >= 0")
        return v

    @model_validator(mode="after")
    def effective_window_must_be_coherent(self) -> "PricingRule":
        """Ensure effective_to > effective_from when both are present."""
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError(
                "effective_to must be after effective_from when both are present"
            )
        return self


class PriceBook(BaseModel):
    """A price book containing a collection of pricing rules.

    Stored in ``price_books_current``. Rules are fanned out into
    ``pricing_rules_current`` for fast resolution by the PricingEngine.
    """

    price_book_id: str = Field(
        default_factory=_generate_price_book_id,
        description="Server-assigned identifier of shape pb_<uuid4>",
    )
    tenant_id: str = Field(..., description="Tenant identifier for data isolation")
    name: str = Field(..., description="Price book name")
    description: Optional[str] = Field(
        default=None, description="Optional description of the price book"
    )
    status: PriceBookStatus = Field(
        default=PriceBookStatus.DRAFT,
        description="Lifecycle status: draft, active, or archived",
    )
    rule_count: int = Field(
        default=0, description="Number of rules in this price book"
    )
    rules: List[PricingRule] = Field(
        default_factory=list,
        description="Embedded list of pricing rules (admin surface)",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp when the price book was created (UTC)",
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp of last modification (UTC)",
    )


class PricingResult(BaseModel):
    """Result returned by ``PricingEngine.resolve()``.

    Contains the resolved pricing information for a single line item.
    """

    unit_price_cents: int = Field(
        ..., description="Rounded whole-cent compatibility unit price"
    )
    unit_price_micros: Optional[int] = Field(
        default=None,
        ge=0,
        description="Exact resolved unit price in integer micro-dollars",
    )
    rule_id: str = Field(
        ..., description="Identifier of the matched pricing rule"
    )
    scope_type: PricingScopeType = Field(
        ..., description="Scope type of the matched rule"
    )
    matched_from_cache: bool = Field(
        ..., description="Whether the result was served from the Redis cache"
    )

    @field_validator("unit_price_cents")
    @classmethod
    def unit_price_cents_must_be_non_negative(cls, v: int) -> int:
        """Reject negative unit_price_cents values."""
        if v < 0:
            raise ValueError("unit_price_cents must be >= 0")
        return v
