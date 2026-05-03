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

    Validates: Requirement 8.1
    """

    recipient_name: str
    signature_url: str
    photo_urls: list[str]
    geotag: GeoPoint
    timestamp: str  # ISO 8601
    otp: Optional[str] = None
