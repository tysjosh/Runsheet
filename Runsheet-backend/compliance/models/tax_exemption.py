"""TaxExemption Pydantic model.

Defines the canonical customer tax-exemption certificate entity backing the
``tax_exemptions`` Elasticsearch index (see
``compliance/services/compliance_es_mappings.py::TAX_EXEMPTIONS_MAPPING``).

A ``TaxExemption`` row represents a customer-held exemption certificate
recognized by the ``Tax_Engine`` when computing per-invoice tax
breakdowns. Certificate shape varies by ``exemption_type``:

- ``dyed_diesel`` / ``off_road``: excludes federal + state road-use excise
  for dyed or off-road diesel deliveries (Req 1.7, 6.1).
- ``farm``: applies the reduced agricultural rate from the jurisdictional
  tax table instead of the standard rate (Req 1.8).
- ``637M``: IRS Form 637 letter M registration for dyed-diesel blenders
  and exempt buyers (Req 1.6). ``letter_suffix`` carries the specific IRS
  letter designation (e.g. "M").
- ``government`` / ``resale``: jurisdiction-specific blanket exemptions.

Validates: Requirements 1.6 (637M registration), 1.7 (dyed/off-road
exemption), 1.8 (farm exemption).
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

# Allowed ``exemption_type`` values — mirrors the ES mapping comment.
ExemptionType = Literal[
    "dyed_diesel",
    "off_road",
    "farm",
    "637M",
    "government",
    "resale",
]

# Allowed ``status`` values — mirrors the ES mapping comment.
ExemptionStatus = Literal["valid", "expired", "revoked"]


# ---------------------------------------------------------------------------
# ID generator
# ---------------------------------------------------------------------------


def _generate_exemption_id() -> str:
    """Generate an exemption_id of shape ``exempt_<uuid4>``."""
    return f"exempt_{uuid4()}"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TaxExemption(BaseModel):
    """Customer tax-exemption certificate stored in ``tax_exemptions``.

    Validators enforce:
    - ``certificate_number`` is a non-empty string (leading/trailing
      whitespace is stripped).
    - ``expiry_date >= issued_date`` when both are provided. Certificates
      without an ``issued_date`` are accepted as legacy imports.
    - Optional text fields (``letter_suffix``, ``issuing_authority``,
      ``jurisdiction_fips``, ``document_ref``) are stripped; all-whitespace
      values collapse to ``None`` so ES queries by keyword are consistent.
    - Optional ``product_codes`` entries are stripped; an empty / ``None``
      list means the exemption applies to all products for the configured
      ``exemption_type`` (e.g. a blanket farm exemption).

    The :meth:`is_expired_as_of` helper gives the ``Tax_Engine`` a single
    source of truth for "can this certificate be honored today?" — the
    certificate is treated as expired when the reference date is past
    ``expiry_date`` or when the status has already been transitioned to
    ``expired`` or ``revoked`` (Req 6.6).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------------------------------------------------
    # Identity / scoping
    # ------------------------------------------------------------------
    exemption_id: str = Field(
        default_factory=_generate_exemption_id,
        description="Server-assigned identifier of shape exempt_<uuid4>",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )
    customer_id: str = Field(
        ...,
        description="Customer who holds the exemption certificate",
    )
    account_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional account identifier when the exemption is scoped to "
            "a specific billing account rather than the whole customer"
        ),
    )

    # ------------------------------------------------------------------
    # Certificate descriptor
    # ------------------------------------------------------------------
    exemption_type: ExemptionType = Field(
        ...,
        description=(
            "Exemption category: dyed_diesel, off_road, farm, 637M, "
            "government, or resale"
        ),
    )
    certificate_number: str = Field(
        ...,
        description=(
            "Certificate number as issued by the authority. "
            "Stripped of surrounding whitespace; must be non-empty."
        ),
    )
    letter_suffix: Optional[str] = Field(
        default=None,
        description=(
            "IRS 637 letter suffix (e.g. 'M' for dyed-diesel blenders). "
            "Populated for 637M-type registrations."
        ),
    )
    issuing_authority: Optional[str] = Field(
        default=None,
        description=(
            "Authority that issued the certificate (e.g. 'IRS', "
            "'CA_CDTFA', state revenue department)"
        ),
    )

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------
    product_codes: Optional[List[str]] = Field(
        default=None,
        description=(
            "Canonicalized product codes this exemption applies to. "
            "An empty list or None means the exemption applies to all "
            "products allowed for the configured exemption_type "
            "(e.g. a blanket farm exemption)."
        ),
    )
    jurisdiction_fips: Optional[str] = Field(
        default=None,
        description=(
            "Optional FIPS code scoping the exemption to a specific "
            "state/county/city. None means the exemption is valid in "
            "every jurisdiction where the Tax_Engine honors the type."
        ),
    )

    # ------------------------------------------------------------------
    # Effective window
    # ------------------------------------------------------------------
    issued_date: Optional[date] = Field(
        default=None,
        description=(
            "Date the certificate was issued by the authority. "
            "Optional to permit legacy imports without paperwork."
        ),
    )
    expiry_date: date = Field(
        ...,
        description=(
            "Last date on which the certificate is honored. "
            "The Tax_Engine rejects dyed-diesel / exemption claims once "
            "this date has passed (Req 6.6)."
        ),
    )
    status: ExemptionStatus = Field(
        default="valid",
        description=(
            "Lifecycle status: valid (default), expired (automatically "
            "transitioned past expiry_date), or revoked (operator-driven)"
        ),
    )

    # ------------------------------------------------------------------
    # Audit / provenance
    # ------------------------------------------------------------------
    document_ref: Optional[str] = Field(
        default=None,
        description=(
            "Reference to the scanned certificate document (e.g. S3 key). "
            "Stored so auditors can retrieve the original paperwork."
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

    @field_validator("certificate_number")
    @classmethod
    def certificate_number_must_be_non_empty(cls, v: str) -> str:
        """Reject empty / whitespace-only certificate numbers.

        A missing certificate number would allow the Tax_Engine to honor
        an unidentifiable exemption — unacceptable for IRS audit readiness
        (Req 6.7).
        """
        if not isinstance(v, str):
            raise ValueError("certificate_number must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError("certificate_number must not be empty")
        return stripped

    @field_validator("letter_suffix", "issuing_authority", "document_ref")
    @classmethod
    def strip_optional_text(cls, v: Optional[str]) -> Optional[str]:
        """Strip whitespace from optional text fields; ``None`` passthrough."""
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @field_validator("jurisdiction_fips")
    @classmethod
    def jurisdiction_fips_must_be_digits(cls, v: Optional[str]) -> Optional[str]:
        """Reject non-digit FIPS codes when provided; ``None`` passthrough.

        FIPS code shape matches :class:`JurisdictionRate`: 2 digits for
        federal/state, 5 for county, 7 for city.
        """
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        if not stripped.isdigit():
            raise ValueError(
                f"jurisdiction_fips must contain only digits, got {v!r}"
            )
        if len(stripped) not in (2, 5, 7):
            raise ValueError(
                "jurisdiction_fips must be 2 digits (federal/state), "
                "5 digits (county), or 7 digits (city), "
                f"got length {len(stripped)}"
            )
        return stripped

    @field_validator("product_codes")
    @classmethod
    def product_codes_entries_must_be_non_empty(
        cls, v: Optional[List[str]]
    ) -> Optional[List[str]]:
        """Strip entries; reject empty / whitespace-only codes.

        An empty top-level list is coerced to ``None`` so the Tax_Engine
        can rely on a single "applies to all products" signal.
        """
        if v is None:
            return None
        cleaned: List[str] = []
        for code in v:
            if not isinstance(code, str):
                raise ValueError(
                    "product_codes entries must be strings"
                )
            stripped = code.strip()
            if not stripped:
                raise ValueError(
                    "product_codes entries must not be empty or whitespace"
                )
            cleaned.append(stripped)
        return cleaned or None

    # ------------------------------------------------------------------
    # Model-level validators
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _check_expiry_not_before_issued(self) -> "TaxExemption":
        """Ensure ``expiry_date >= issued_date`` when both are provided.

        Skipped when ``issued_date`` is ``None`` (legacy import) so historic
        paperwork without a recorded issue date still loads cleanly.
        """
        if (
            self.issued_date is not None
            and self.expiry_date < self.issued_date
        ):
            raise ValueError(
                "expiry_date must be >= issued_date when both are "
                f"provided (got issued_date={self.issued_date}, "
                f"expiry_date={self.expiry_date})"
            )
        return self

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_expired_as_of(self, reference_date: date) -> bool:
        """Return True if this certificate cannot be honored on ``reference_date``.

        A certificate is considered expired when:
        - ``reference_date`` is strictly greater than ``expiry_date``, or
        - ``status`` has already been transitioned to ``expired`` or
          ``revoked`` (operator-driven state that persists regardless of
          the date).

        The Tax_Engine consults this method when resolving whether to
        apply a dyed-diesel / farm / off-road exemption to an invoice
        (Req 1.7, 1.8, 6.6).
        """
        if self.status in ("expired", "revoked"):
            return True
        return reference_date > self.expiry_date

    # ------------------------------------------------------------------
    # Uniform cross-module subject reference (cross-module-entity-linkage
    # task 10, Req 11.1). An exemption is about a **customer**, or the more
    # specific **account** when it is account-scoped (``account_id`` set).
    # The uniform ``subject_ref`` is a view over the existing
    # ``customer_id`` / ``account_id`` — no second copy.
    # ------------------------------------------------------------------
    @property
    def subject_ref(self) -> "SubjectRef":
        """The customer/account this exemption applies to, as a ``SubjectRef``."""
        from compliance.services.compliance_subject_ref import subject_ref_for_kind

        ref = subject_ref_for_kind(
            "exemption", subject_id=self.customer_id, account_id=self.account_id
        )
        # customer_id is required on the model, so a ref is always derivable.
        assert ref is not None  # noqa: S101 - invariant guard, not a runtime check
        return ref
