"""Portal response models (design §2.1 E10, §3).

Every model is ``extra="forbid"`` and is built only from explicit projection
code, so a response can never carry a field that isn't listed here.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _PortalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortalMeasurementUnits(_PortalModel):
    volume: str
    distance: str


class PortalMe(_PortalModel):
    """``GET /api/portal/me`` (design §3.1)."""

    email: str
    customer_display_name: str
    #: Falls back to the tenant id until tenants have a display name (B1, DV1).
    supplier_name: str
    ordering_available: bool
    invoices_available: bool
    payments_available: bool
    measurement_units: PortalMeasurementUnits


class PortalMeEnvelope(_PortalModel):
    data: PortalMe
    request_id: str


# ---------------------------------------------------------------------------
# Staff portal-user administration (design §1.7)
# ---------------------------------------------------------------------------


class PortalUserInviteRequest(_PortalModel):
    """``POST /api/commerce/customers/{customer_id}/portal-users``.

    Same minimal shape check as ``AppAccessGrantRequest`` (no ``EmailStr``, so
    the optional ``email-validator`` dependency is never needed).
    """

    email: str = Field(..., min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        candidate = (value or "").strip()
        local, _, domain = candidate.partition("@")
        if not local or not domain or "." not in domain or " " in candidate:
            raise ValueError("email must be a valid address")
        return candidate


PortalUserStatus = Literal["invited", "active", "revoked"]


class PortalUserGrant(_PortalModel):
    grant_id: str
    email: str
    status: PortalUserStatus
    created_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class PortalUserListResponse(_PortalModel):
    data: List[PortalUserGrant]


class PortalUserLinkResponse(_PortalModel):
    """``POST .../{grant_id}/resend``; also the delivery part of an invite."""

    password_set_link: Optional[str] = None
    link_error: bool
    email_sent: bool


class PortalUserInviteResponse(_PortalModel):
    """201 for a new invite; 200 with ``already_invited: true`` otherwise."""

    grant_id: str
    email: str
    status: PortalUserStatus
    password_set_link: Optional[str] = None
    link_error: bool
    email_sent: bool
    already_invited: bool = False


class PortalUserRevokeResponse(_PortalModel):
    grant_id: str
    status: Literal["revoked"]


__all__ = [
    "PortalMe",
    "PortalMeEnvelope",
    "PortalMeasurementUnits",
    "PortalUserGrant",
    "PortalUserInviteRequest",
    "PortalUserInviteResponse",
    "PortalUserLinkResponse",
    "PortalUserListResponse",
    "PortalUserRevokeResponse",
]
