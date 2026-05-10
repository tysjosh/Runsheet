"""PricingRule Pydantic model.

Defines the sell-side pricing rule entity evaluated by the
``Sales_Pricing_Engine`` (design §11) and backing the ``pricing_rules``
Elasticsearch index (see
``compliance/services/compliance_es_mappings.py::PRICING_RULES_MAPPING``).

A ``PricingRule`` describes how to resolve a customer-facing unit price
for a given (customer, account, product) tuple using one of four
strategies (Req 11.1):

- ``posted_price``      — fixed ¢/gallon stored on the rule
- ``rack_plus_margin``  — current OPIS rack price + ``margin_cents``;
                          rack price is looked up at resolution time for
                          the configured ``terminal_id``
- ``tiered_volume``     — price breaks across gallon thresholds
                          (``tier_thresholds`` submodel)
- ``cost_plus``         — rack price + freight (``freight_rate_cents_per_mile``
                          × miles) + ``margin_cents``

Resolution order (Req 11.2) is customer-specific → account-tier →
product-default. ``customer_id is None`` identifies a product-default
rule, and ``account_id`` scopes a rule to a specific billing account.

Validates: Requirements 11.1
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.time_utils import utcnow


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Allowed ``strategy`` values (Req 11.1) — mirrors the ES mapping comment.
PricingStrategy = Literal[
    "posted_price",
    "rack_plus_margin",
    "tiered_volume",
    "cost_plus",
]

# Allowed lifecycle ``status`` values — mirrors the ES mapping comment.
#   active   → rule is evaluated by the Sales_Pricing_Engine
#   inactive → rule is retained for history but skipped at resolution time
PricingRuleStatus = Literal["active", "inactive"]


# ---------------------------------------------------------------------------
# ID generator
# ---------------------------------------------------------------------------


def _generate_rule_id() -> str:
    """Generate a rule_id of shape ``rule_<uuid4>``.

    Matches the ``commerce.models.price_book._generate_rule_id`` convention
    so PriceBook and PricingRule identifiers share the same namespace and
    never collide with the ``pb_<uuid4>`` / ``contract_<uuid4>`` prefixes
    used elsewhere in the commerce domain.
    """
    return f"rule_{uuid4()}"


# ---------------------------------------------------------------------------
# Submodels
# ---------------------------------------------------------------------------


class TierBreak(BaseModel):
    """A single tier in a ``tiered_volume`` pricing rule.

    Tiers describe a half-open gallon range ``[min_gallons, max_gallons)``
    mapped to a ``unit_price_cents`` price break. ``max_gallons`` is
    optional so an unbounded top tier can be expressed as ``None``.

    Validators enforce:
    - ``min_gallons`` >= 0 — a tier cannot start below zero gallons.
    - ``max_gallons`` > ``min_gallons`` when provided — a tier must cover
      at least some gallon span to be meaningful.
    - ``unit_price_cents`` >= 0 (Constraint C1: money is integer cents).
    """

    model_config = ConfigDict(extra="forbid")

    min_gallons: float = Field(
        ...,
        description=(
            "Inclusive lower gallon threshold for this tier. Must be >= 0."
        ),
    )
    max_gallons: Optional[float] = Field(
        default=None,
        description=(
            "Exclusive upper gallon threshold for this tier. None means "
            "the tier is unbounded (top tier). When provided, must be "
            "strictly greater than min_gallons."
        ),
    )
    unit_price_cents: int = Field(
        ...,
        description=(
            "Unit price in integer cents per gallon for this tier "
            "(Constraint C1). Must be >= 0."
        ),
    )

    @field_validator("min_gallons")
    @classmethod
    def min_gallons_must_be_non_negative(cls, v: float) -> float:
        """Reject negative ``min_gallons`` values."""
        if v < 0:
            raise ValueError(f"min_gallons must be >= 0, got {v}")
        return v

    @field_validator("unit_price_cents")
    @classmethod
    def unit_price_cents_must_be_non_negative(cls, v: int) -> int:
        """Reject negative ``unit_price_cents`` (Constraint C1)."""
        if v < 0:
            raise ValueError(f"unit_price_cents must be >= 0, got {v}")
        return v

    @model_validator(mode="after")
    def _check_max_greater_than_min(self) -> "TierBreak":
        """Ensure ``max_gallons > min_gallons`` when provided.

        An inverted or zero-width tier would make the resolver either
        skip the tier entirely or double-apply the neighboring price.
        """
        if (
            self.max_gallons is not None
            and self.max_gallons <= self.min_gallons
        ):
            raise ValueError(
                "max_gallons must be strictly greater than min_gallons "
                f"when provided (got min_gallons={self.min_gallons}, "
                f"max_gallons={self.max_gallons})"
            )
        return self


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class PricingRule(BaseModel):
    """Sell-side pricing rule stored in ``pricing_rules``.

    Validators enforce (Req 11.1):
    - Strategy-specific required fields:
      * ``posted_price``     → ``posted_price_cents`` required
      * ``rack_plus_margin`` → ``margin_cents`` and ``terminal_id`` required
      * ``tiered_volume``    → non-empty ``tier_thresholds`` required
      * ``cost_plus``        → ``margin_cents`` and
                               ``freight_rate_cents_per_mile`` required
    - ``expiry_date >= effective_date`` when an ``expiry_date`` is supplied.
    - All cents-denominated fields are non-negative integer cents
      (Constraint C1: money is integer cents).
    - ``product_code`` is non-empty.
    - Tier thresholds are sorted and non-overlapping when ``tiered_volume``
      is used.

    The model is deliberately permissive about fields that do not belong
    to the active ``strategy`` — e.g. a ``posted_price`` rule may still
    record a ``margin_cents`` hint for analyst context — but the
    ``Sales_Pricing_Engine`` only consults the strategy-relevant fields
    at resolution time. What the validators guarantee is that every rule
    has **at least** the inputs its strategy needs.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------------------------------------------------
    # Identity / scoping
    # ------------------------------------------------------------------
    rule_id: str = Field(
        default_factory=_generate_rule_id,
        description="Server-assigned identifier of shape rule_<uuid4>",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )
    customer_id: Optional[str] = Field(
        default=None,
        description=(
            "Customer identifier the rule applies to. None indicates a "
            "product-default rule (lowest priority in the Req 11.2 "
            "resolution order)."
        ),
    )
    account_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional billing account identifier scoping the rule to a "
            "specific account (account-tier rule). Takes priority over "
            "product-default but below customer-specific."
        ),
    )
    product_code: str = Field(
        ...,
        description=(
            "Canonicalized product code (e.g. 'HEATING_OIL', 'DIESEL_LSD') "
            "the rule covers. Resolution matches this exact code."
        ),
    )

    # ------------------------------------------------------------------
    # Strategy + parameters (Req 11.1)
    # ------------------------------------------------------------------
    strategy: PricingStrategy = Field(
        ...,
        description=(
            "One of posted_price, rack_plus_margin, tiered_volume, or "
            "cost_plus (Req 11.1)."
        ),
    )
    posted_price_cents: Optional[int] = Field(
        default=None,
        description=(
            "Fixed unit price in integer cents per gallon. Required for "
            "posted_price; optional for other strategies."
        ),
    )
    margin_cents: Optional[int] = Field(
        default=None,
        description=(
            "Margin in integer cents per gallon added on top of the rack "
            "price. Required for rack_plus_margin and cost_plus."
        ),
    )
    freight_rate_cents_per_mile: Optional[int] = Field(
        default=None,
        description=(
            "Freight surcharge in integer cents per mile. Required for "
            "cost_plus; multiplied by route miles at resolution time "
            "(Req 11.5)."
        ),
    )
    terminal_id: Optional[str] = Field(
        default=None,
        description=(
            "Terminal identifier used to look up the OPIS rack price. "
            "Required for rack_plus_margin."
        ),
    )
    tier_thresholds: Optional[List[TierBreak]] = Field(
        default=None,
        description=(
            "Ordered list of tier price breaks. Required (non-empty) for "
            "tiered_volume (Req 11.4). When provided, tiers must be "
            "sorted by min_gallons and must not overlap."
        ),
    )

    # ------------------------------------------------------------------
    # Priority + effective window
    # ------------------------------------------------------------------
    priority: int = Field(
        default=0,
        description=(
            "Lower value = higher priority when multiple rules match "
            "(Req 11.2). Rules are sorted ascending by priority before "
            "scope-based resolution."
        ),
    )
    effective_date: date = Field(
        ...,
        description=(
            "First date (inclusive) on which this rule applies. "
            "Rule changes apply prospectively."
        ),
    )
    expiry_date: Optional[date] = Field(
        default=None,
        description=(
            "Last date (inclusive) on which this rule applies. "
            "None means the rule is active indefinitely."
        ),
    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    status: PricingRuleStatus = Field(
        default="active",
        description=(
            "Lifecycle status: active (default, evaluated by the engine) "
            "or inactive (retained for audit, skipped at resolution)."
        ),
    )

    # ------------------------------------------------------------------
    # Audit timestamps
    # ------------------------------------------------------------------
    created_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp when the record was created (UTC)",
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp of last modification (UTC)",
    )

    # ------------------------------------------------------------------
    # Field validators
    # ------------------------------------------------------------------

    @field_validator("product_code")
    @classmethod
    def product_code_must_be_non_empty(cls, v: str) -> str:
        """Reject empty / whitespace-only ``product_code`` values."""
        if not isinstance(v, str):
            raise ValueError("product_code must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError("product_code must not be empty or whitespace")
        return stripped

    @field_validator(
        "posted_price_cents",
        "margin_cents",
        "freight_rate_cents_per_mile",
    )
    @classmethod
    def cents_fields_must_be_non_negative(
        cls, v: Optional[int]
    ) -> Optional[int]:
        """Reject negative integer-cents price fields (Constraint C1).

        ``None`` passes through — strategy-level validators decide whether
        the field is required for the active strategy.
        """
        if v is None:
            return None
        if v < 0:
            raise ValueError(
                f"cents fields must be >= 0 when provided, got {v}"
            )
        return v

    @field_validator("customer_id", "account_id", "terminal_id")
    @classmethod
    def optional_keyword_strip_or_none(
        cls, v: Optional[str]
    ) -> Optional[str]:
        """Strip whitespace; all-whitespace collapses to ``None``.

        Keeps ES keyword lookups consistent — ``""`` and ``"  "`` are
        equivalent to "no scope" and must be stored as ``None``.
        """
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    # ------------------------------------------------------------------
    # Model-level validators
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _check_effective_window(self) -> "PricingRule":
        """Ensure ``expiry_date >= effective_date`` when ``expiry_date`` is set.

        An inverted window would make the rule permanently unreachable
        and silently fall through to the next priority tier — easier to
        catch at write time than to debug in production.
        """
        if (
            self.expiry_date is not None
            and self.expiry_date < self.effective_date
        ):
            raise ValueError(
                "expiry_date must be >= effective_date when provided "
                f"(got effective_date={self.effective_date}, "
                f"expiry_date={self.expiry_date})"
            )
        return self

    @model_validator(mode="after")
    def _check_strategy_requirements(self) -> "PricingRule":
        """Enforce strategy-specific required fields (Req 11.1).

        - ``posted_price``     → ``posted_price_cents`` required
        - ``rack_plus_margin`` → ``margin_cents`` and ``terminal_id`` required
        - ``tiered_volume``    → non-empty ``tier_thresholds`` required
        - ``cost_plus``        → ``margin_cents`` and
                                 ``freight_rate_cents_per_mile`` required
        """
        if self.strategy == "posted_price":
            if self.posted_price_cents is None:
                raise ValueError(
                    "posted_price strategy requires posted_price_cents"
                )

        elif self.strategy == "rack_plus_margin":
            if self.margin_cents is None:
                raise ValueError(
                    "rack_plus_margin strategy requires margin_cents"
                )
            if self.terminal_id is None:
                raise ValueError(
                    "rack_plus_margin strategy requires terminal_id"
                )

        elif self.strategy == "tiered_volume":
            if not self.tier_thresholds:
                raise ValueError(
                    "tiered_volume strategy requires a non-empty "
                    "tier_thresholds list"
                )

        else:  # cost_plus
            if self.margin_cents is None:
                raise ValueError(
                    "cost_plus strategy requires margin_cents"
                )
            if self.freight_rate_cents_per_mile is None:
                raise ValueError(
                    "cost_plus strategy requires freight_rate_cents_per_mile"
                )

        return self

    @model_validator(mode="after")
    def _check_tier_thresholds_sorted_and_non_overlapping(
        self,
    ) -> "PricingRule":
        """Ensure tier breaks are sorted by ``min_gallons`` and do not overlap.

        Only enforced when ``tier_thresholds`` is provided (possible on any
        strategy — the resolver ignores it outside ``tiered_volume``, but a
        malformed list should still be caught at write time).

        Overlap rule: for adjacent tiers ``prev`` and ``curr``, we require
        ``curr.min_gallons >= prev.max_gallons``. ``prev.max_gallons`` may
        be ``None`` only on the final tier, so an earlier unbounded tier
        is rejected.
        """
        tiers = self.tier_thresholds
        if not tiers:
            return self

        for idx in range(1, len(tiers)):
            prev = tiers[idx - 1]
            curr = tiers[idx]

            if prev.max_gallons is None:
                raise ValueError(
                    "only the final tier may have max_gallons=None "
                    f"(tier at index {idx - 1} is unbounded but tier at "
                    f"index {idx} follows it)"
                )
            if curr.min_gallons < prev.max_gallons:
                raise ValueError(
                    "tier_thresholds must be sorted and non-overlapping "
                    f"(tier at index {idx} has min_gallons="
                    f"{curr.min_gallons} < previous tier max_gallons="
                    f"{prev.max_gallons})"
                )

        return self
