"""Portal response models (design §2.1 E10, §3).

Every model is ``extra="forbid"`` and is built only from explicit projection
code, so a response can never carry a field that isn't listed here.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, List, Literal, Optional, Union

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


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


# ---------------------------------------------------------------------------
# Order requests (design §4.1, §4.5, PD9, FREEZE F6)
# ---------------------------------------------------------------------------

#: Path-id rule (design §3.2). A malformed id answers like an unknown one.
PATH_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"
_PATH_ID_RE = re.compile(PATH_ID_PATTERN)

#: F6 window bounds: the server never needs a time zone.
WINDOW_START_MAX_PAST = timedelta(hours=24)
WINDOW_START_MAX_FUTURE = timedelta(days=60)
WINDOW_MAX_LENGTH = timedelta(hours=25)


def is_path_id(value: object) -> bool:
    return isinstance(value, str) and _PATH_ID_RE.fullmatch(value) is not None


def _now() -> datetime:
    """The window-rule clock (tests patch it)."""
    return datetime.now(timezone.utc)


def _has_control(value: str, *, allow_newline: bool) -> bool:
    return any(
        (unicodedata.category(c) == "Cc") and not (allow_newline and c == "\n")
        for c in value
    )


class PortalQuantityFill(_PortalModel):
    mode: Literal["fill_to_full"]


class PortalQuantityGallons(_PortalModel):
    mode: Literal["gallons"]
    #: ``<= capacity_gallons`` is checked by the service once the tank loads.
    gallons: float = Field(..., gt=0)


PortalQuantity = Annotated[
    Union[PortalQuantityFill, PortalQuantityGallons], Field(discriminator="mode")
]


class PortalOrderRequest(_PortalModel):
    """``POST /api/portal/orders``. Unknown fields (incl. scope ids) are 422."""

    client_event_id: str
    customer_tank_id: str = Field(..., pattern=PATH_ID_PATTERN)
    quantity: PortalQuantity
    window_start: AwareDatetime
    window_end: AwareDatetime
    po_number: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("client_event_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("client_event_id must be a UUID") from None

    @field_validator("po_number")
    @classmethod
    def _po_number(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > 64:
            raise ValueError("po_number must be at most 64 characters")
        if _has_control(value, allow_newline=False):
            raise ValueError("po_number must not contain control characters")
        return value

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > 500:
            raise ValueError("notes must be at most 500 characters")
        if _has_control(value, allow_newline=True):
            raise ValueError("notes must not contain control characters")
        return value

    @model_validator(mode="after")
    def _window(self) -> "PortalOrderRequest":
        now = _now()
        if self.window_start < now - WINDOW_START_MAX_PAST:
            raise ValueError("window_start must be no more than 24 hours in the past")
        if self.window_start > now + WINDOW_START_MAX_FUTURE:
            raise ValueError("window_start must be within the next 60 days")
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        if self.window_end - self.window_start > WINDOW_MAX_LENGTH:
            raise ValueError("the delivery window must be at most 25 hours long")
        return self


class PortalCancelRequest(_PortalModel):
    """``POST /api/portal/orders/{order_id}/cancel``: an empty object."""


class PortalOrderTank(_PortalModel):
    customer_tank_id: str
    label: str


class PortalOrder(_PortalModel):
    """A customer's order as the portal shows it (design §4.5)."""

    order_id: str
    status_code: str
    status_label: str
    product_code: Optional[str] = None
    gallons_requested: Optional[float] = None
    fill_to_full: bool = False
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    po_number: Optional[str] = None
    tank: Optional[PortalOrderTank] = None
    created_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    delivered_gallons: Optional[float] = None
    ticket_number: Optional[str] = None
    cancellable: bool = False


class PortalOrderEnvelope(_PortalModel):
    data: PortalOrder
    request_id: str


class PortalOrderListEnvelope(_PortalModel):
    data: List[PortalOrder]
    next_cursor: Optional[str] = None
    limit: int
    request_id: str


# ---------------------------------------------------------------------------
# Tanks (design §7)
# ---------------------------------------------------------------------------


class PortalTankForecast(_PortalModel):
    runout_at: datetime
    days_to_runout: int
    generated_at: datetime


class PortalNextDelivery(_PortalModel):
    order_id: str
    status_label: str
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None


class PortalTank(_PortalModel):
    customer_tank_id: str
    label: str
    product_code: str
    capacity_gallons: float
    current_level_gallons: float
    percent_full: float
    last_reading_at: Optional[datetime] = None
    reading_stale: bool
    forecast: Optional[PortalTankForecast] = None
    next_delivery: Optional[PortalNextDelivery] = None


class PortalTankEnvelope(_PortalModel):
    data: PortalTank
    request_id: str


class PortalTankListEnvelope(_PortalModel):
    data: List[PortalTank]
    next_cursor: Optional[str] = None
    limit: int
    request_id: str


class PortalTankDelivery(_PortalModel):
    order_id: str
    delivered_at: Optional[datetime] = None
    delivered_gallons: Optional[float] = None
    product_code: Optional[str] = None
    ticket_number: Optional[str] = None


class PortalTankDeliveryListEnvelope(_PortalModel):
    data: List[PortalTankDelivery]
    next_cursor: Optional[str] = None
    limit: int
    request_id: str


# ---------------------------------------------------------------------------
# Invoices (design §5, PD12)
# ---------------------------------------------------------------------------


class PortalInvoiceLineItem(_PortalModel):
    product_code: Optional[str] = None
    quantity_gallons: Optional[float] = None
    unit_price_cents: Optional[int] = None
    subtotal_cents: Optional[int] = None


class PortalInvoiceDelivery(_PortalModel):
    """Only these three ``delivery_result`` facts ever reach the portal."""

    delivered_at: Optional[datetime] = None
    actual_gallons: Optional[float] = None
    ticket_number: Optional[str] = None


class PortalInvoicePaymentAttempt(_PortalModel):
    """The newest portal payment attempt (detail only; wired in FEAT-005)."""

    payment_attempt_id: str
    status_code: str
    status_label: str
    amount_cents: int
    created_at: Optional[datetime] = None


class PortalInvoice(_PortalModel):
    invoice_id: str
    invoice_number: Optional[str] = None
    status_code: str
    status_label: str
    issued_at: Optional[datetime] = None
    due_date: Optional[date] = None
    created_at: Optional[datetime] = None
    account_display_name: str
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    amount_paid_cents: int
    remaining_cents: int
    line_items: List[PortalInvoiceLineItem]
    delivery: Optional[PortalInvoiceDelivery] = None
    payment_attempt: Optional[PortalInvoicePaymentAttempt] = None
    payable: bool


class PortalInvoiceEnvelope(_PortalModel):
    data: PortalInvoice
    request_id: str


class PortalInvoiceListEnvelope(_PortalModel):
    data: List[PortalInvoice]
    next_cursor: Optional[str] = None
    limit: int
    request_id: str


__all__ = [
    "PATH_ID_PATTERN",
    "PortalInvoice",
    "PortalInvoiceDelivery",
    "PortalInvoiceEnvelope",
    "PortalInvoiceLineItem",
    "PortalInvoiceListEnvelope",
    "PortalInvoicePaymentAttempt",
    "PortalCancelRequest",
    "PortalNextDelivery",
    "PortalOrder",
    "PortalOrderEnvelope",
    "PortalOrderListEnvelope",
    "PortalOrderRequest",
    "PortalOrderTank",
    "PortalQuantityFill",
    "PortalQuantityGallons",
    "PortalTank",
    "PortalTankDelivery",
    "PortalTankDeliveryListEnvelope",
    "PortalTankEnvelope",
    "PortalTankForecast",
    "PortalTankListEnvelope",
    "is_path_id",
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
