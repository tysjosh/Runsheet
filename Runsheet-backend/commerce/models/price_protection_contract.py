"""PriceProtectionContract Pydantic model.

Defines the sell-side price-protection contract entity backing the
``price_protection_contracts`` Elasticsearch index (see
``compliance/services/compliance_es_mappings.py::PRICE_PROTECTION_CONTRACTS_MAPPING``).

A ``PriceProtectionContract`` record represents a guaranteed-price
commitment between the distributor and a customer/account for a
specific product over the window [start_date, end_date]. The
``Price_Protection_Service`` resolves an effective sell price per
delivery by dispatching on ``contract_type`` (design §3, tasks 4.2 and
4.3) and decrements ``remaining_gallons`` with an optimistic-concurrency
guard keyed on ``version``.

Validates: Requirements 3.1, 3.2
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.time_utils import utcnow


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Allowed ``contract_type`` values (Req 3.1) — mirrors the ES mapping comment.
ContractType = Literal["fixed_price", "cap_price", "collar"]

# Allowed lifecycle ``status`` values — mirrors the ES mapping comment.
#   active    → contract is in force and eligible for price resolution
#   exhausted → contracted_gallons fully consumed (transition on Req 3.6)
#   expired   → end_date has passed (transition on Req 3.6)
#   cancelled → operator-initiated termination before end_date
ContractStatus = Literal["active", "exhausted", "expired", "cancelled"]


# ---------------------------------------------------------------------------
# ID generator
# ---------------------------------------------------------------------------


def _generate_contract_id() -> str:
    """Generate a contract_id of shape ``contract_<uuid4>``."""
    return f"contract_{uuid4()}"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class PriceProtectionContract(BaseModel):
    """Sell-side price-protection contract stored in ``price_protection_contracts``.

    Validators enforce (Req 3.1, 3.2):
    - ``contracted_gallons`` is strictly positive; ``remaining_gallons`` is
      non-negative and never exceeds ``contracted_gallons``.
    - ``end_date`` is greater than or equal to ``start_date``.
    - ``fixed_price`` contracts require ``fixed_price_cents`` and reject
      ``price_cap_cents`` / ``price_floor_cents`` so the resolver never
      sees ambiguous inputs.
    - ``cap_price`` contracts require ``price_cap_cents``; ``price_floor_cents``
      is rejected (collar semantics would otherwise apply silently).
    - ``collar`` contracts require both ``price_cap_cents`` and
      ``price_floor_cents`` with ``price_floor_cents <= price_cap_cents``.
    - All cents-denominated fields are non-negative integer cents
      (Constraint C1: money is integer cents).
    - ``remaining_gallons`` defaults to ``contracted_gallons`` when not
      explicitly provided, so a freshly-created contract starts with its
      full allotment available.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------------------------------------------------
    # Identity / scoping
    # ------------------------------------------------------------------
    contract_id: str = Field(
        default_factory=_generate_contract_id,
        description="Server-assigned identifier of shape contract_<uuid4>",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )
    customer_id: str = Field(
        ..., description="Customer identifier the contract is written for"
    )
    account_id: str = Field(
        ..., description="Billing account identifier the contract applies to"
    )
    product_code: str = Field(
        ...,
        description=(
            "Canonical product code (e.g. 'HEATING_OIL', 'DIESEL_LSD') the "
            "contract covers. Resolution matches this exact code."
        ),
    )

    # ------------------------------------------------------------------
    # Contract semantics (Req 3.1, 3.2)
    # ------------------------------------------------------------------
    contract_type: ContractType = Field(
        ...,
        description="One of fixed_price, cap_price, or collar (Req 3.1)",
    )
    start_date: date = Field(
        ..., description="First date (inclusive) the contract is in force"
    )
    end_date: date = Field(
        ..., description="Last date (inclusive) the contract is in force"
    )

    # ------------------------------------------------------------------
    # Volume tracking (Req 3.2, 3.4)
    # ------------------------------------------------------------------
    contracted_gallons: float = Field(
        ...,
        description=(
            "Total gallons reserved under the contract. Must be > 0 — a "
            "zero-gallon contract has no coverage window to decrement."
        ),
    )
    remaining_gallons: Optional[float] = Field(
        default=None,
        description=(
            "Gallons still available under the contract. Defaults to "
            "contracted_gallons at creation time. Must be >= 0 and "
            "<= contracted_gallons."
        ),
    )

    # ------------------------------------------------------------------
    # Pricing parameters (Req 3.2, 3.3)
    # ------------------------------------------------------------------
    price_cap_cents: Optional[int] = Field(
        default=None,
        description=(
            "Maximum sell price in integer cents per gallon. Required for "
            "cap_price and collar contracts; rejected for fixed_price."
        ),
    )
    price_floor_cents: Optional[int] = Field(
        default=None,
        description=(
            "Minimum sell price in integer cents per gallon. Required for "
            "collar contracts; rejected for fixed_price and cap_price."
        ),
    )
    fixed_price_cents: Optional[int] = Field(
        default=None,
        description=(
            "Locked sell price in integer cents per gallon. Required for "
            "fixed_price contracts; rejected for cap_price and collar."
        ),
    )

    # ------------------------------------------------------------------
    # Lifecycle (Req 3.6)
    # ------------------------------------------------------------------
    status: ContractStatus = Field(
        default="active",
        description="Lifecycle status: active, exhausted, expired, or cancelled",
    )
    version: int = Field(
        default=0,
        description=(
            "Optimistic-concurrency counter incremented by decrement_gallons "
            "(Task 4.3). Must be >= 0."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description="Free-form operator notes for contract context",
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

    @field_validator("contracted_gallons")
    @classmethod
    def contracted_gallons_must_be_positive(cls, v: float) -> float:
        """Reject non-positive ``contracted_gallons``.

        A zero-gallon contract has no coverage window to decrement, and a
        negative contract would corrupt the remaining-gallons invariant.
        """
        if v <= 0:
            raise ValueError(
                f"contracted_gallons must be > 0, got {v}"
            )
        return v

    @field_validator(
        "price_cap_cents",
        "price_floor_cents",
        "fixed_price_cents",
    )
    @classmethod
    def cents_fields_must_be_non_negative(cls, v: Optional[int]) -> Optional[int]:
        """Reject negative integer-cents price fields (Constraint C1)."""
        if v is None:
            return None
        if v < 0:
            raise ValueError(
                f"price fields must be >= 0 cents when provided, got {v}"
            )
        return v

    @field_validator("version")
    @classmethod
    def version_must_be_non_negative(cls, v: int) -> int:
        """Reject negative OCC ``version`` counters."""
        if v < 0:
            raise ValueError(f"version must be >= 0, got {v}")
        return v

    @field_validator("notes")
    @classmethod
    def notes_strip_or_none(cls, v: Optional[str]) -> Optional[str]:
        """Strip whitespace; all-whitespace collapses to ``None``."""
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    # ------------------------------------------------------------------
    # Model-level validators
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _check_end_date_not_before_start(self) -> "PriceProtectionContract":
        """Ensure ``end_date >= start_date``.

        An inverted window would never resolve a price and would produce a
        contract that is simultaneously active and unreachable.
        """
        if self.end_date < self.start_date:
            raise ValueError(
                "end_date must be >= start_date "
                f"(got start_date={self.start_date}, end_date={self.end_date})"
            )
        return self

    @model_validator(mode="after")
    def _default_and_check_remaining_gallons(self) -> "PriceProtectionContract":
        """Default ``remaining_gallons`` to ``contracted_gallons`` and validate.

        Runs after field validation so both values are known. Enforces:
        - non-negative ``remaining_gallons``
        - ``remaining_gallons <= contracted_gallons``
        """
        if self.remaining_gallons is None:
            # Bypass validate_assignment re-entry by writing to __dict__;
            # field_validator for contracted_gallons has already checked
            # positivity so the default is safe.
            object.__setattr__(self, "remaining_gallons", self.contracted_gallons)
            return self

        if self.remaining_gallons < 0:
            raise ValueError(
                f"remaining_gallons must be >= 0, got {self.remaining_gallons}"
            )
        if self.remaining_gallons > self.contracted_gallons:
            raise ValueError(
                "remaining_gallons must be <= contracted_gallons "
                f"(got remaining_gallons={self.remaining_gallons}, "
                f"contracted_gallons={self.contracted_gallons})"
            )
        return self

    @model_validator(mode="after")
    def _check_pricing_parameters_match_contract_type(
        self,
    ) -> "PriceProtectionContract":
        """Ensure pricing parameters align with ``contract_type`` (Req 3.1, 3.2).

        ``fixed_price`` → requires ``fixed_price_cents``; rejects cap/floor.
        ``cap_price``   → requires ``price_cap_cents``; rejects fixed/floor.
        ``collar``      → requires cap and floor; floor <= cap; rejects fixed.
        """
        if self.contract_type == "fixed_price":
            if self.fixed_price_cents is None:
                raise ValueError(
                    "fixed_price contracts require fixed_price_cents"
                )
            if self.price_cap_cents is not None:
                raise ValueError(
                    "fixed_price contracts must not set price_cap_cents"
                )
            if self.price_floor_cents is not None:
                raise ValueError(
                    "fixed_price contracts must not set price_floor_cents"
                )

        elif self.contract_type == "cap_price":
            if self.price_cap_cents is None:
                raise ValueError(
                    "cap_price contracts require price_cap_cents"
                )
            if self.fixed_price_cents is not None:
                raise ValueError(
                    "cap_price contracts must not set fixed_price_cents"
                )
            if self.price_floor_cents is not None:
                raise ValueError(
                    "cap_price contracts must not set price_floor_cents "
                    "(use collar for floor+cap semantics)"
                )

        else:  # collar
            if self.price_cap_cents is None:
                raise ValueError(
                    "collar contracts require price_cap_cents"
                )
            if self.price_floor_cents is None:
                raise ValueError(
                    "collar contracts require price_floor_cents"
                )
            if self.fixed_price_cents is not None:
                raise ValueError(
                    "collar contracts must not set fixed_price_cents"
                )
            if self.price_floor_cents > self.price_cap_cents:
                raise ValueError(
                    "collar contracts require price_floor_cents <= "
                    "price_cap_cents "
                    f"(got price_floor_cents={self.price_floor_cents}, "
                    f"price_cap_cents={self.price_cap_cents})"
                )

        return self
