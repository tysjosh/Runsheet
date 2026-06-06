"""TerminalBOL Pydantic model.

Defines the canonical ``TerminalBOL`` entity backing the ``terminal_bols``
Elasticsearch index.

A ``TerminalBOL`` record represents a terminal-issued Bill of Lading captured
at the rack — either via EDI (ANSI X12 856 or pipe-delimited) or manual
upload. It stores the loaded product details, gross/net gallons, temperature,
API gravity, supplier, terminal, and driver information for chain-of-custody
traceability and reconciliation against delivery records.

The ``Terminal_BOL_Ingestion_Service`` uses this model to persist parsed BOL
data and link it to load plans (design §10, tasks 11.1–11.13).

Validates: Requirements 10.1
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.time_utils import utcnow


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# BOL processing status values.
BOLStatus = Literal["ingested", "linked", "verified", "pending_confirmation"]


# ---------------------------------------------------------------------------
# ID generator
# ---------------------------------------------------------------------------


def _generate_bol_id() -> str:
    """Generate a bol_id of shape ``bol_<uuid4>``."""
    return f"bol_{uuid4()}"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TerminalBOL(BaseModel):
    """Terminal Bill of Lading record stored in ``terminal_bols`` index.

    Represents a single BOL issued at a fuel terminal (rack), capturing the
    product loaded, volumes, temperature/gravity measurements, and driver
    assignment.

    Validators enforce (Req 10.1):
    - ``load_number`` is non-empty.
    - ``product_code`` is non-empty.
    - ``tenant_id`` is non-empty.
    - ``gross_gallons`` is positive.
    - ``net_gallons`` is positive.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------------------------------------------------
    # Identity / scoping
    # ------------------------------------------------------------------
    bol_id: str = Field(
        default_factory=_generate_bol_id,
        description="Server-assigned identifier of shape bol_<uuid4>",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )

    # ------------------------------------------------------------------
    # BOL core fields (parsed from EDI/upload)
    # ------------------------------------------------------------------
    load_number: str = Field(
        ...,
        description="Unique load identifier assigned by the terminal",
    )
    product_code: str = Field(
        ...,
        description="Fuel product code (e.g., UNL87, ULSD, PROP)",
    )
    gross_gallons: float = Field(
        ...,
        description="Gross gallons loaded at observed temperature",
    )
    net_gallons: float = Field(
        ...,
        description="Net gallons corrected to 60°F via VCF",
    )
    observed_temperature_f: float = Field(
        ...,
        description="Observed temperature at loading in degrees Fahrenheit",
    )
    api_gravity: float = Field(
        ...,
        description="API gravity of the product at loading",
    )

    # ------------------------------------------------------------------
    # Origin / supplier details
    # ------------------------------------------------------------------
    supplier_name: str = Field(
        ...,
        description="Name of the fuel supplier at the terminal",
    )
    terminal_name: str = Field(
        ...,
        description="Name of the loading terminal (rack)",
    )
    terminal_id: Optional[str] = Field(
        default=None,
        description=(
            "Canonical reference to the loading terminal record "
            "(``fuel.terminal_models.Terminal``), resolvable via the shared "
            "RefResolver's ``terminal`` loader. Nullable/additive so existing "
            "BOLs remain valid without backfill; when set it supersedes the "
            "free-text ``terminal_name`` snapshot as the source of truth for "
            "the terminal identity (cross-module-entity-linkage Req 9.2, 6.1)."
        ),
    )

    # ------------------------------------------------------------------
    # Driver reference
    # ------------------------------------------------------------------
    driver_id: str = Field(
        ...,
        description="Identifier of the driver who loaded the product",
    )

    # ------------------------------------------------------------------
    # Timestamps
    # ------------------------------------------------------------------
    timestamp: datetime = Field(
        ...,
        description="Timestamp when the BOL was issued at the terminal",
    )

    # ------------------------------------------------------------------
    # Document storage
    # ------------------------------------------------------------------
    raw_document_ref: Optional[str] = Field(
        default=None,
        description="S3 key/reference to the raw EDI payload or uploaded document",
    )

    # ------------------------------------------------------------------
    # Linkage
    # ------------------------------------------------------------------
    load_plan_id: Optional[str] = Field(
        default=None,
        description="Linked load plan ID (set after ingestion when matched)",
    )

    # ------------------------------------------------------------------
    # Status / flags
    # ------------------------------------------------------------------
    status: BOLStatus = Field(
        default="ingested",
        description="Processing status: ingested, linked, or verified",
    )
    vcf_discrepancy_flag: Optional[bool] = Field(
        default=None,
        description="Set to True if VCF cross-check fails (net_gallons mismatch > ±0.1%)",
    )
    needs_operator_confirmation: bool = Field(
        default=False,
        description="True when BOL was ingested via manual upload and OCR results need operator confirmation",
    )

    # ------------------------------------------------------------------
    # Audit / timestamps
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

    @field_validator("tenant_id")
    @classmethod
    def tenant_id_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only tenant_id."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("tenant_id must not be empty or whitespace")
        return stripped

    @field_validator("load_number")
    @classmethod
    def load_number_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only load_number."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("load_number must not be empty or whitespace")
        return stripped

    @field_validator("product_code")
    @classmethod
    def product_code_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only product_code."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("product_code must not be empty or whitespace")
        return stripped

    @field_validator("gross_gallons")
    @classmethod
    def gross_gallons_must_be_positive(cls, v: float) -> float:
        """Reject zero or negative gross_gallons."""
        if v <= 0:
            raise ValueError("gross_gallons must be positive")
        return v

    @field_validator("net_gallons")
    @classmethod
    def net_gallons_must_be_positive(cls, v: float) -> float:
        """Reject zero or negative net_gallons."""
        if v <= 0:
            raise ValueError("net_gallons must be positive")
        return v

    # ------------------------------------------------------------------
    # Uniform cross-module subject reference (cross-module-entity-linkage
    # task 10, Req 11.1). A terminal BOL's compliance subject is the
    # **driver** who loaded the product; the uniform ``subject_ref`` is a
    # view over the existing ``driver_id``.
    # ------------------------------------------------------------------
    @property
    def subject_ref(self) -> "SubjectRef":
        """The driver who loaded this BOL, as a uniform ``SubjectRef``."""
        from compliance.services.compliance_subject_ref import SubjectRef

        return SubjectRef(subject_type="driver", subject_id=self.driver_id)

    @field_validator("terminal_id")
    @classmethod
    def terminal_id_strip_optional(cls, v: Optional[str]) -> Optional[str]:
        """Normalize a blank ``terminal_id`` to ``None``.

        An empty / whitespace-only id is treated as "no canonical reference"
        rather than a dangling blank, so the shared RefResolver surfaces it as
        ``empty`` (reference absent) instead of ``unresolved``.
        """
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None
