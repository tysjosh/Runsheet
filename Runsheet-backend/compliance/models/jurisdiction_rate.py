"""JurisdictionRate Pydantic model.

Defines the canonical jurisdictional fuel-tax rate entity backing the
``tax_jurisdictions`` Elasticsearch index (see
``compliance/services/compliance_es_mappings.py::TAX_JURISDICTIONS_MAPPING``).

A ``JurisdictionRate`` row represents a single fuel-tax rate (federal,
state, county, city, UST, SPCC, environmental, etc.) that applies to a
set of product codes over the window [effective_date, expiry_date]. The
``Tax_Engine`` resolves rates by ``fips_code`` + ``tax_type`` +
effective date (design §1, tasks 3.3 and 3.4).

Validates: Requirements 1.5
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

# Allowed ``jurisdiction_level`` values — mirrors the ES mapping comment.
JurisdictionLevel = Literal["federal", "state", "county", "city"]

# Allowed ``tax_type`` values — mirrors the ES mapping comment. UST and SPCC
# are environmental surcharges; ``environmental`` is the catch-all for
# jurisdiction-specific environmental fees (e.g. state cleanup funds).
TaxType = Literal["excise", "ust", "spcc", "environmental"]

# FIPS code width by jurisdiction level.
# - Federal rows use a 2-digit sentinel (commonly "00") so a single query
#   shape ``len(fips_code) == 2`` serves both federal and state lookups.
# - State: 2-digit FIPS (01-56 for US states and territories).
# - County: 5-digit FIPS (state 2 + county 3).
# - City:  7-digit FIPS place code (state 2 + place 5).
_FIPS_LENGTH_BY_LEVEL: dict[str, set[int]] = {
    "federal": {2},
    "state":   {2},
    "county":  {5},
    "city":    {7},
}


# ---------------------------------------------------------------------------
# ID generator
# ---------------------------------------------------------------------------


def _generate_jurisdiction_id() -> str:
    """Generate a jurisdiction_id of shape ``juris_<uuid4>``."""
    return f"juris_{uuid4()}"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class JurisdictionRate(BaseModel):
    """Jurisdictional fuel-tax rate record stored in ``tax_jurisdictions``.

    Validators enforce (Req 1.5):
    - ``fips_code`` contains digits only with a length matching the
      ``jurisdiction_level`` (2 for federal/state, 5 for county, 7 for city).
    - ``rate_cents_per_gallon`` is non-negative integer cents
      (Constraint C1: money is integer cents — see Commerce Backbone).
    - ``expiry_date`` is greater than or equal to ``effective_date`` when
      an ``expiry_date`` is supplied; rows without ``expiry_date`` apply
      indefinitely into the future.
    - ``product_codes`` is non-empty (a rate with no product scope cannot
      be resolved by the Tax_Engine).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------------------------------------------------
    # Identity / scoping
    # ------------------------------------------------------------------
    jurisdiction_id: str = Field(
        default_factory=_generate_jurisdiction_id,
        description="Server-assigned identifier of shape juris_<uuid4>",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )

    # ------------------------------------------------------------------
    # Jurisdiction descriptor
    # ------------------------------------------------------------------
    fips_code: str = Field(
        ...,
        description=(
            "FIPS code identifying the jurisdiction: 2-digit state "
            "(or federal sentinel), 5-digit county, or 7-digit city/place"
        ),
    )
    jurisdiction_level: JurisdictionLevel = Field(
        ...,
        description="Jurisdictional level: federal, state, county, or city",
    )
    jurisdiction_name: Optional[str] = Field(
        default=None,
        description="Human-readable jurisdiction name for display / reporting",
    )

    # ------------------------------------------------------------------
    # Tax semantics
    # ------------------------------------------------------------------
    tax_type: TaxType = Field(
        ...,
        description="Tax category: excise, ust, spcc, or environmental",
    )
    product_codes: List[str] = Field(
        ...,
        description=(
            "Canonicalized product codes this rate applies to. "
            "Non-empty — a rate with no product scope cannot be resolved."
        ),
    )
    rate_cents_per_gallon: int = Field(
        ...,
        description=(
            "Rate in integer cents per gallon (Constraint C1). "
            "Must be >= 0 to avoid negative tax line items."
        ),
    )

    # ------------------------------------------------------------------
    # Effective window (Req 1.5)
    # ------------------------------------------------------------------
    effective_date: date = Field(
        ...,
        description=(
            "First date (inclusive) on which this rate applies. "
            "Rate changes apply prospectively — historical invoices "
            "continue to reference the row active at their invoice date."
        ),
    )
    expiry_date: Optional[date] = Field(
        default=None,
        description=(
            "Last date (inclusive) on which this rate applies. "
            "None means the row is active indefinitely into the future."
        ),
    )

    # ------------------------------------------------------------------
    # Audit / provenance
    # ------------------------------------------------------------------
    source: Optional[str] = Field(
        default=None,
        description=(
            "Source identifier for the rate (e.g. 'irs_form_720', "
            "'avalara_2026q1', 'manual_csv_import')"
        ),
    )
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

    @field_validator("fips_code")
    @classmethod
    def fips_code_must_be_valid(cls, v: str) -> str:
        """Reject non-digit or wrong-length FIPS codes.

        Level-specific length alignment is enforced in
        :meth:`_check_fips_length_matches_level` because ``field_validator``
        cannot cross-reference ``jurisdiction_level``.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError("fips_code must not be empty")
        if not stripped.isdigit():
            raise ValueError(
                f"fips_code must contain only digits, got {v!r}"
            )
        if len(stripped) not in (2, 5, 7):
            raise ValueError(
                "fips_code must be 2 digits (federal/state), "
                "5 digits (county), or 7 digits (city), "
                f"got length {len(stripped)}"
            )
        return stripped

    @field_validator("product_codes")
    @classmethod
    def product_codes_must_be_non_empty(cls, v: List[str]) -> List[str]:
        """Reject empty ``product_codes`` lists and empty entries."""
        if not v:
            raise ValueError(
                "product_codes must contain at least one product code"
            )
        cleaned: List[str] = []
        for code in v:
            stripped = code.strip()
            if not stripped:
                raise ValueError(
                    "product_codes entries must not be empty or whitespace"
                )
            cleaned.append(stripped)
        return cleaned

    @field_validator("rate_cents_per_gallon")
    @classmethod
    def rate_must_be_non_negative(cls, v: int) -> int:
        """Reject negative rates (Constraint C1).

        Negative rates would produce negative tax line items and break
        Form 720 reconciliation.
        """
        if v < 0:
            raise ValueError(
                f"rate_cents_per_gallon must be >= 0, got {v}"
            )
        return v

    @field_validator("jurisdiction_name", "source")
    @classmethod
    def strip_optional_text(cls, v: Optional[str]) -> Optional[str]:
        """Strip whitespace from optional text fields; ``None`` passthrough."""
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    # ------------------------------------------------------------------
    # Model-level validators
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _check_fips_length_matches_level(self) -> "JurisdictionRate":
        """Ensure ``fips_code`` length aligns with ``jurisdiction_level``.

        Cross-field check because :meth:`fips_code_must_be_valid` runs in
        isolation. Federal rows accept the 2-digit sentinel so a single
        lookup shape serves both federal and state resolutions.
        """
        expected_lengths = _FIPS_LENGTH_BY_LEVEL[self.jurisdiction_level]
        actual = len(self.fips_code)
        if actual not in expected_lengths:
            raise ValueError(
                f"fips_code length {actual} does not match "
                f"jurisdiction_level {self.jurisdiction_level!r} "
                f"(expected one of {sorted(expected_lengths)})"
            )
        return self

    @model_validator(mode="after")
    def _check_expiry_not_before_effective(self) -> "JurisdictionRate":
        """Ensure ``expiry_date`` is not earlier than ``effective_date``.

        Prevents impossible effective windows that would silently exclude
        every invoice from the rate table.
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
