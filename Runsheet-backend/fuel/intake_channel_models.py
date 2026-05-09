"""
Intake Channel domain model — IntakeChannel.

This module defines the Pydantic model that backs the ``intake_channels``
Elasticsearch index. The model uses ``ConfigDict(extra="forbid")`` so unknown
fields are rejected at construction time, matching the strict ES mapping.

Key responsibilities:

* Expose :class:`IntakeChannel` — the per-tenant configuration record that
  binds a ``channel_id`` to a ``channel_type``, an HMAC secret reference,
  supported schema versions, and optional rate-limit override.
* Expose :data:`RegistrableChannelType` — the 6 channel types that can be
  registered via the admin surface (excludes ``legacy`` which is only valid
  on the order's ``intake_channel`` field during migration).
* Enforce business rules via validators:
  - ``channel_id`` matches ``^[a-z0-9][a-z0-9\\-]{1,62}[a-z0-9]$``.
  - ``channel_type`` is one of the 6 registrable types.
  - ``supported_schema_versions`` is non-empty.
  - ``rate_limit_per_minute > 0`` when set.

Validates: Requirement 2.1.2.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Type Aliases
# ---------------------------------------------------------------------------

RegistrableChannelType = Literal[
    "voice", "web_portal", "dispatcher", "csv", "edi", "api_partner",
]

# Regex for valid channel_id: starts and ends with lowercase alphanumeric,
# middle allows lowercase alphanumeric and hyphens, total length 3–64.
_CHANNEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}[a-z0-9]$")


# ---------------------------------------------------------------------------
# IntakeChannel
# ---------------------------------------------------------------------------


class IntakeChannel(BaseModel):
    """Per-tenant intake channel configuration.

    Persisted in the ``intake_channels`` ES index. Each channel binds a
    ``channel_id`` to a ``channel_type``, an HMAC secret reference stored
    in the :class:`TenantCredentialsVault`, a list of supported schema
    versions, and an optional rate-limit override.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(..., min_length=3, max_length=64)
    tenant_id: str = Field(..., min_length=1)
    channel_type: RegistrableChannelType
    display_name: str = Field(..., min_length=1)
    hmac_secret_ref: str = Field(..., min_length=1)
    supported_schema_versions: List[str] = Field(..., min_length=1)
    rate_limit_per_minute: Optional[int] = Field(default=None)
    secret_version: int = Field(default=1, ge=1)
    enabled: bool = True
    created_at: datetime
    updated_at: datetime

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("channel_id")
    @classmethod
    def _validate_channel_id(cls, value: str) -> str:
        """Validate channel_id matches the required pattern.

        Must start and end with a lowercase alphanumeric character, with
        lowercase alphanumeric and hyphens allowed in between. Total
        length between 3 and 64 characters.
        """
        if not _CHANNEL_ID_PATTERN.match(value):
            raise ValueError(
                "channel_id must match ^[a-z0-9][a-z0-9\\-]{1,62}[a-z0-9]$"
            )
        return value

    @field_validator("supported_schema_versions")
    @classmethod
    def _validate_supported_schema_versions(cls, value: List[str]) -> List[str]:
        """Ensure supported_schema_versions is non-empty."""
        if not value:
            raise ValueError("supported_schema_versions must not be empty")
        return value

    @field_validator("rate_limit_per_minute")
    @classmethod
    def _validate_rate_limit(cls, value: Optional[int]) -> Optional[int]:
        """Ensure rate_limit_per_minute > 0 when set."""
        if value is not None and value <= 0:
            raise ValueError("rate_limit_per_minute must be greater than 0")
        return value


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "RegistrableChannelType",
    "IntakeChannel",
]
