"""Customer Pydantic model.

Defines the canonical Customer entity for the Commerce Backbone.
Fields align with the ``customers_current`` ES mapping (design §3.1).
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from services.time_utils import utcnow


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CustomerStatus(str, Enum):
    """Allowed lifecycle states for a Customer record."""

    ACTIVE = "active"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TAX_ID_PATTERN = re.compile(r"^[A-Z0-9-]{1,64}$")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def _generate_customer_id() -> str:
    """Generate a customer_id of shape ``cust_<uuid4>``."""
    return f"cust_{uuid4()}"


class Customer(BaseModel):
    """Canonical Customer record stored in ``customers_current``.

    Validators enforce:
    - ``display_name`` is non-empty, non-whitespace, and <= 255 chars.
    - ``tax_id``, when present, matches ``^[A-Z0-9-]{1,64}$``.
    - ``status`` is one of the ``CustomerStatus`` enum values.
    """

    customer_id: str = Field(
        default_factory=_generate_customer_id,
        description="Server-assigned identifier of shape cust_<uuid4>",
    )
    tenant_id: str = Field(..., description="Tenant identifier for data isolation")
    display_name: str = Field(..., description="Customer display name")
    legal_name: Optional[str] = Field(default=None, description="Legal business name")
    primary_email: Optional[str] = Field(default=None, description="Primary contact email")
    tax_id: Optional[str] = Field(default=None, description="Tax identification number")
    status: CustomerStatus = Field(
        default=CustomerStatus.ACTIVE,
        description="Lifecycle status: active or archived",
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
        description="External system references (e.g. {qbo: 'cust:123', stripe: 'cus_...'})",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary tenant-defined metadata (not indexed)",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("display_name")
    @classmethod
    def display_name_must_be_non_empty(cls, v: str) -> str:
        """Reject empty, whitespace-only, or overly long display names."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("display_name must not be empty or whitespace-only")
        if len(stripped) > 255:
            raise ValueError("display_name must not exceed 255 characters")
        return stripped

    @field_validator("tax_id")
    @classmethod
    def tax_id_must_match_pattern(cls, v: Optional[str]) -> Optional[str]:
        """Validate tax_id against ``^[A-Z0-9-]{1,64}$`` when present."""
        if v is None:
            return v
        if not _TAX_ID_PATTERN.match(v):
            raise ValueError(
                "tax_id must match ^[A-Z0-9-]{1,64}$ (uppercase alphanumeric and hyphens, 1-64 chars)"
            )
        return v
