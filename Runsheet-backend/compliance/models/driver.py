"""Driver Pydantic model.

Defines the canonical Driver entity backing the ``drivers`` Elasticsearch
index (see ``compliance/services/compliance_es_mappings.py::DRIVERS_MAPPING``).

A ``Driver`` record represents a driver's qualification file including CDL,
medical card, HAZMAT endorsement, tanker endorsement, drug testing, and MVR
records. The ``Driver_Qualification_Service`` uses this model to evaluate
dispatch eligibility and generate expiry alerts (design §5, tasks 6.1–6.11).

Validates: Requirements 5.1
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Dict, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.time_utils import utcnow


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid CDL classes per FMCSA regulations.
CDLClass = Literal["A", "B", "C"]

# Driver status values — mirrors the ES mapping comment.
DriverStatus = Literal["active", "suspended", "expired"]

# Regex for 2-letter uppercase US state code.
_STATE_CODE_RE = re.compile(r"^[A-Z]{2}$")


# ---------------------------------------------------------------------------
# ID generator
# ---------------------------------------------------------------------------


def _generate_driver_id() -> str:
    """Generate a driver_id of shape ``driver_<uuid4>``."""
    return f"driver_{uuid4()}"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Driver(BaseModel):
    """Driver qualification file record stored in ``drivers`` index.

    Validators enforce (Req 5.1):
    - ``cdl_number`` is non-empty.
    - ``cdl_state`` is exactly 2 uppercase letters (US state abbreviation).
    - ``full_name`` is non-empty.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------------------------------------------------
    # Identity / scoping
    # ------------------------------------------------------------------
    driver_id: str = Field(
        default_factory=_generate_driver_id,
        description="Server-assigned identifier of shape driver_<uuid4>",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )

    # ------------------------------------------------------------------
    # Personal / CDL information
    # ------------------------------------------------------------------
    full_name: str = Field(
        ...,
        description="Driver's full legal name as it appears on CDL",
    )
    cdl_number: str = Field(
        ...,
        description="Commercial Driver's License number",
    )
    cdl_state: str = Field(
        ...,
        description="2-letter US state code where CDL was issued",
    )
    cdl_class: CDLClass = Field(
        ...,
        description="CDL class: A, B, or C",
    )

    # ------------------------------------------------------------------
    # Qualification expiry dates
    # ------------------------------------------------------------------
    cdl_expiry_date: date = Field(
        ...,
        description="CDL expiration date",
    )
    medical_card_expiry_date: date = Field(
        ...,
        description="DOT medical card expiration date",
    )
    hazmat_endorsement_expiry_date: Optional[date] = Field(
        default=None,
        description="HAZMAT endorsement expiration date (None if not endorsed)",
    )
    tanker_endorsement_expiry_date: Optional[date] = Field(
        default=None,
        description="Tanker endorsement expiration date (None if not endorsed)",
    )

    # ------------------------------------------------------------------
    # Testing / review dates
    # ------------------------------------------------------------------
    last_drug_test_date: Optional[date] = Field(
        default=None,
        description="Date of most recent drug/alcohol test",
    )
    last_mvr_date: Optional[date] = Field(
        default=None,
        description="Date of most recent Motor Vehicle Record review",
    )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    status: DriverStatus = Field(
        default="active",
        description="Driver qualification status: active, suspended, or expired",
    )
    suspension_reason: Optional[str] = Field(
        default=None,
        description="Reason for suspension (populated when status is suspended)",
    )

    # ------------------------------------------------------------------
    # External references
    # ------------------------------------------------------------------
    external_refs: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "External system references (e.g. geotab_driver_id, "
            "payroll_id, tms_driver_id)"
        ),
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

    @field_validator("full_name")
    @classmethod
    def full_name_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only full_name."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("full_name must not be empty or whitespace")
        return stripped

    @field_validator("cdl_number")
    @classmethod
    def cdl_number_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only cdl_number."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("cdl_number must not be empty or whitespace")
        return stripped

    @field_validator("cdl_state")
    @classmethod
    def cdl_state_must_be_two_uppercase_letters(cls, v: str) -> str:
        """Validate cdl_state is exactly 2 uppercase letters (US state code)."""
        stripped = v.strip()
        if not _STATE_CODE_RE.match(stripped):
            raise ValueError(
                "cdl_state must be exactly 2 uppercase letters "
                f"(US state abbreviation), got {v!r}"
            )
        return stripped

    @field_validator("suspension_reason")
    @classmethod
    def strip_suspension_reason(cls, v: Optional[str]) -> Optional[str]:
        """Strip whitespace from suspension_reason; collapse empty to None."""
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None
