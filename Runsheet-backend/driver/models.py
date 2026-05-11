"""
Pydantic v2 models for the Driver Communication API.

Defines validated request models for driver-facing endpoints:
acknowledgment, rejection, job-thread messaging, exception reporting,
and proof of delivery. These models are used for request validation
in REST endpoints and internal service communication.

Requirements: 5.1, 5.3, 6.1, 7.1, 7.3, 8.1
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel

from Agents.overlay.data_contracts import Severity


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ExceptionType(str, Enum):
    """Type of exception a driver can report from the field.

    Validates: Requirement 7.3
    """

    ROAD_CLOSURE = "road_closure"
    VEHICLE_BREAKDOWN = "vehicle_breakdown"
    CUSTOMER_UNAVAILABLE = "customer_unavailable"
    ACCESS_DENIED = "access_denied"
    WEATHER = "weather"
    CARGO_DAMAGE = "cargo_damage"
    OTHER = "other"


class DeliveryRefusalReason(str, Enum):
    """Reason codes for a refused delivery workflow."""

    CUSTOMER_REFUSED = "customer_refused"
    CUSTOMER_UNAVAILABLE = "customer_unavailable"
    ACCESS_DENIED = "access_denied"
    UNSAFE_SITE = "unsafe_site"
    WRONG_PRODUCT = "wrong_product"
    INSUFFICIENT_CAPACITY = "insufficient_capacity"
    PAYMENT_HOLD = "payment_hold"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Shared models
# ---------------------------------------------------------------------------


class GeoPoint(BaseModel):
    """Geographic coordinate pair (WGS 84).

    Validates: Requirements 7.1, 8.1
    """

    lat: float
    lng: float


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AckRequest(BaseModel):
    """Request body for driver job acknowledgment.

    Validates: Requirement 5.1
    """

    device_id: Optional[str] = None


class RejectRequest(BaseModel):
    """Request body for driver job rejection.

    Validates: Requirement 5.3
    """

    reason: str


class MessageRequest(BaseModel):
    """Request body for posting a message to a job thread.

    Validates: Requirement 6.1
    """

    body: str
    sender_id: str
    sender_role: str  # "driver" or "dispatcher"


class ExceptionRequest(BaseModel):
    """Request body for reporting a field exception.

    Validates: Requirements 7.1, 7.3
    """

    exception_type: ExceptionType
    severity: Severity
    note: str
    location: Optional[GeoPoint] = None
    media_refs: Optional[list[str]] = None


class PODRequest(BaseModel):
    """Request body for submitting proof of delivery.

    Drivers now submit proof-of-delivery artifacts as ``file_ref`` values
    returned from ``POST /api/driver/pod/uploads/presign`` rather than as
    raw public URLs:

    * ``signature_ref`` — the presigned-upload file_ref for the signature.
    * ``photo_refs`` — zero or more presigned-upload file_refs for photos.
    * ``meter_ticket_ref`` — optional presigned-upload file_ref for the
      meter-ticket image (used by the OCR pipeline).

    The legacy ``signature_url`` and ``photo_urls`` fields remain accepted
    for backward compatibility but are **deprecated**. When both the
    ``*_ref`` and ``*_url`` variants are supplied, the server prefers the
    file_ref variant and ignores the URL. Any file_ref that does not belong
    to the submitting tenant is rejected with HTTP 403 via
    :class:`FileStorageService`'s tenant-prefix check.

    The optional ``delivered_gallons`` field is a driver-entered override
    for the gallon count. When absent and a ``meter_ticket_ref`` is
    supplied, the POD endpoint triggers :class:`MeterTicketOCRService` to
    extract the value automatically (Req 4.2.4). When present, the
    driver's value is treated as authoritative (``delivered_gallons_source
    = "manual"``, Req 4.2.5).

    Validates: Requirements 8.1, 4.1.4, 4.1.6, 4.2.4, 4.2.5
    """

    recipient_name: str
    customer_id: Optional[str] = None
    # Preferred: tenant-scoped file_refs returned by the presign endpoint.
    signature_ref: Optional[str] = None
    photo_refs: Optional[list[str]] = None
    meter_ticket_ref: Optional[str] = None
    # Deprecated: raw URLs retained for backward compatibility.
    signature_url: Optional[str] = None
    photo_urls: Optional[list[str]] = None
    # Optional driver-entered gallon count. Takes precedence over OCR when
    # supplied; otherwise OCR will extract it from ``meter_ticket_ref``.
    delivered_gallons: Optional[float] = None
    geotag: GeoPoint
    timestamp: str  # ISO 8601
    otp: Optional[str] = None
    refused_delivery: bool = False
    refusal_reason_code: Optional[DeliveryRefusalReason] = None
    refusal_note: Optional[str] = None


class PODPresignUploadRequest(BaseModel):
    """Request body for requesting a presigned POD upload URL.

    The caller specifies the ``category`` of the artifact it intends to
    upload (signature, photo, meter_ticket, or bol) and the ``content_type``
    header it will send on the PUT. The server responds with a short-lived
    presigned URL and the ``file_ref`` that the driver will later attach to
    the POD submission.

    Validates: Requirements 4.1.3, 4.1.5
    """

    category: str
    content_type: str
