"""AssetCertification Pydantic model.

Defines the canonical AssetCertification entity backing the
``asset_certifications`` Elasticsearch index.

An ``AssetCertification`` record represents a single certification for a
vehicle or trailer — DOT cargo tank inspections (V/K/I/P/UT), meter seal
certifications, or fire extinguisher recertification. The
``Asset_Certification_Service`` uses this model to evaluate dispatch
eligibility and generate expiry alerts (design §13, tasks 8.1–8.12).

Validates: Requirements 13.1
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

# Valid certification types per DOT/FMCSA regulations for cargo tanks,
# plus meter seal and fire extinguisher.
CertificationType = Literal[
    "V_test",
    "K_test",
    "I_test",
    "P_test",
    "UT_test",
    "meter_seal",
    "fire_extinguisher",
]

# Certification status values.
CertificationStatus = Literal["valid", "expiring_soon", "expired"]


# ---------------------------------------------------------------------------
# ID generator
# ---------------------------------------------------------------------------


def _generate_cert_id() -> str:
    """Generate a cert_id of shape ``cert_<uuid4>``."""
    return f"cert_{uuid4()}"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class AssetCertification(BaseModel):
    """Vehicle/trailer certification record stored in ``asset_certifications`` index.

    Validators enforce (Req 13.1):
    - ``certification_type`` must be one of the valid DOT/operational types.
    - ``status`` must be one of: valid, expiring_soon, expired.
    - ``inspector_name`` is non-empty.
    - ``certificate_number`` is non-empty.
    - ``asset_id`` is non-empty.
    - ``tenant_id`` is non-empty.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------------------------------------------------
    # Identity / scoping
    # ------------------------------------------------------------------
    cert_id: str = Field(
        default_factory=_generate_cert_id,
        description="Server-assigned identifier of shape cert_<uuid4>",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )

    # ------------------------------------------------------------------
    # Asset reference
    # ------------------------------------------------------------------
    asset_id: str = Field(
        ...,
        description="Identifier of the vehicle or trailer this certification belongs to",
    )

    # ------------------------------------------------------------------
    # Certification details
    # ------------------------------------------------------------------
    certification_type: CertificationType = Field(
        ...,
        description=(
            "Type of certification: V_test, K_test, I_test, P_test, "
            "UT_test, meter_seal, or fire_extinguisher"
        ),
    )
    certification_date: date = Field(
        ...,
        description="Date the certification was issued/performed",
    )
    expiry_date: date = Field(
        ...,
        description="Date the certification expires",
    )
    inspector_name: str = Field(
        ...,
        description="Name of the inspector who performed the certification",
    )
    certificate_number: str = Field(
        ...,
        description="Official certificate/document number",
    )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    status: CertificationStatus = Field(
        default="valid",
        description="Certification status: valid, expiring_soon, or expired",
    )

    # ------------------------------------------------------------------
    # Optional provenance (present in the ES mapping; populated by the
    # admin surface / imports). Kept optional so older records without them
    # still validate.
    # ------------------------------------------------------------------
    issuing_authority: Optional[str] = Field(
        default=None,
        description="Authority that issued / performed the certification",
    )
    retest_due_date: Optional[date] = Field(
        default=None,
        description=(
            "Date the asset is next due for retest (e.g. the 3-year horizon "
            "for annual cargo-tank tests); None when not applicable"
        ),
    )
    document_ref: Optional[str] = Field(
        default=None,
        description="Reference to the scanned certificate document (e.g. S3 key)",
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

    @field_validator("asset_id")
    @classmethod
    def asset_id_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only asset_id."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("asset_id must not be empty or whitespace")
        return stripped

    @field_validator("tenant_id")
    @classmethod
    def tenant_id_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only tenant_id."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("tenant_id must not be empty or whitespace")
        return stripped

    @field_validator("inspector_name")
    @classmethod
    def inspector_name_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only inspector_name."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("inspector_name must not be empty or whitespace")
        return stripped

    @field_validator("certificate_number")
    @classmethod
    def certificate_number_must_be_non_empty(cls, v: str) -> str:
        """Reject empty or whitespace-only certificate_number."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("certificate_number must not be empty or whitespace")
        return stripped

    # ------------------------------------------------------------------
    # Uniform cross-module subject reference (cross-module-entity-linkage
    # task 10, Req 11.1). A certification is about a fleet **asset**; the
    # uniform ``subject_ref`` is a view over the existing ``asset_id`` (no
    # second copy) so the reference can be rendered/validated/resolved the
    # same way as every other compliance record.
    # ------------------------------------------------------------------
    @property
    def subject_ref(self) -> "SubjectRef":
        """The asset this certification is about, as a uniform ``SubjectRef``."""
        from compliance.services.compliance_subject_ref import SubjectRef

        return SubjectRef(subject_type="asset", subject_id=self.asset_id)
