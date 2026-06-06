"""Meter Pydantic models.

Defines the canonical ``MeterRegistration`` and ``MeterAuditEntry`` entities
backing the ``meter_registry`` and ``meter_audit_trail`` Elasticsearch indices
(see ``compliance/services/compliance_es_mappings.py``).

A ``MeterRegistration`` record represents a physical flow meter installed on a
delivery truck, including its calibration certificate and certifying authority.
A ``MeterAuditEntry`` records the immutable association between a meter ticket,
delivery, and invoice for weights-and-measures traceability.

The ``Meter_Audit_Service`` uses these models to maintain the meter registry,
link tickets to invoices, and generate calibration alerts (design §8,
tasks 10.1–10.8).

Validates: Requirements 8.3
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.time_utils import utcnow


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Meter status values.
MeterStatus = Literal["active", "expired_calibration"]


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------


def _generate_meter_id() -> str:
    """Generate a meter_id of shape ``meter_<uuid4>``."""
    return f"meter_{uuid4()}"


def _generate_audit_id() -> str:
    """Generate an audit_id of shape ``maudit_<uuid4>``."""
    return f"maudit_{uuid4()}"


# ---------------------------------------------------------------------------
# MeterRegistration Model
# ---------------------------------------------------------------------------


class MeterRegistration(BaseModel):
    """Meter registry record stored in ``meter_registry`` index.

    Represents a physical flow meter installed on a delivery truck with its
    calibration certificate details and certifying authority.

    Validators enforce (Req 8.3):
    - ``meter_number`` is non-empty.
    - ``truck_id`` is non-empty.
    - ``tenant_id`` is non-empty.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------------------------------------------------
    # Identity / scoping
    # ------------------------------------------------------------------
    meter_id: str = Field(
        default_factory=_generate_meter_id,
        description="Server-assigned identifier of shape meter_<uuid4>",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )

    # ------------------------------------------------------------------
    # Meter details
    # ------------------------------------------------------------------
    meter_number: str = Field(
        ...,
        description="Physical meter serial/identification number",
    )
    truck_id: str = Field(
        ...,
        description="Identifier of the truck this meter is installed on",
    )

    # ------------------------------------------------------------------
    # Calibration details
    # ------------------------------------------------------------------
    calibration_certificate_number: str = Field(
        ...,
        description="Official calibration certificate number",
    )
    calibration_date: date = Field(
        ...,
        description="Date the calibration was performed",
    )
    calibration_expiry_date: date = Field(
        ...,
        description="Date the calibration expires",
    )
    weights_measures_authority: str = Field(
        ...,
        description="The certifying weights and measures authority",
    )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    status: MeterStatus = Field(
        default="active",
        description="Meter status: active or expired_calibration",
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

    @field_validator("meter_number")
    @classmethod
    def meter_number_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only meter_number."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("meter_number must not be empty or whitespace")
        return stripped

    @field_validator("truck_id")
    @classmethod
    def truck_id_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only truck_id."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("truck_id must not be empty or whitespace")
        return stripped

    @field_validator("tenant_id")
    @classmethod
    def tenant_id_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only tenant_id."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("tenant_id must not be empty or whitespace")
        return stripped

    # ------------------------------------------------------------------
    # Uniform cross-module subject reference (cross-module-entity-linkage
    # task 10, Req 11.1). A meter is installed on a truck, which is a fleet
    # **asset**; ``truck_id == asset_id`` (design §Data Models), so the
    # uniform ``subject_ref`` is a view over ``truck_id``.
    # ------------------------------------------------------------------
    @property
    def subject_ref(self) -> "SubjectRef":
        """The asset (truck) this meter is installed on, as a ``SubjectRef``."""
        from compliance.services.compliance_subject_ref import SubjectRef

        return SubjectRef(subject_type="asset", subject_id=self.truck_id)


# ---------------------------------------------------------------------------
# MeterAuditEntry Model
# ---------------------------------------------------------------------------


class MeterAuditEntry(BaseModel):
    """Per-meter audit trail record stored in ``meter_audit_trail`` index.

    Records the immutable association between a meter ticket, delivery, and
    invoice for weights-and-measures traceability (Req 8.2, 8.6).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------------------------------------------------
    # Identity / scoping
    # ------------------------------------------------------------------
    audit_id: str = Field(
        default_factory=_generate_audit_id,
        description="Server-assigned identifier of shape maudit_<uuid4>",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )

    # ------------------------------------------------------------------
    # References
    # ------------------------------------------------------------------
    meter_id: str = Field(
        ...,
        description="Reference to the meter in the meter_registry",
    )
    meter_ticket_id: str = Field(
        ...,
        description="Reference to the OCR-processed meter ticket",
    )
    delivery_id: str = Field(
        ...,
        description="Reference to the delivery record",
    )
    invoice_id: Optional[str] = Field(
        default=None,
        description="Reference to the linked invoice (None until invoice is generated)",
    )

    # ------------------------------------------------------------------
    # Measurement data
    # ------------------------------------------------------------------
    gross_gallons: float = Field(
        ...,
        description="Gross gallons recorded on the meter ticket",
    )

    # ------------------------------------------------------------------
    # Timestamps
    # ------------------------------------------------------------------
    timestamp: datetime = Field(
        ...,
        description="Timestamp of the meter reading/delivery event",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp when the audit record was created (UTC)",
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

    @field_validator("meter_id")
    @classmethod
    def meter_id_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only meter_id."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("meter_id must not be empty or whitespace")
        return stripped

    @field_validator("meter_ticket_id")
    @classmethod
    def meter_ticket_id_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only meter_ticket_id."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("meter_ticket_id must not be empty or whitespace")
        return stripped

    @field_validator("delivery_id")
    @classmethod
    def delivery_id_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only delivery_id."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("delivery_id must not be empty or whitespace")
        return stripped
