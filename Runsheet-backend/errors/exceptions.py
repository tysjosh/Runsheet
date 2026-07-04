"""
Exception classes for the Runsheet backend.

This module provides the AppException class and convenience factory
functions for creating application-specific exceptions with proper
error codes and HTTP status codes.

Validates: Requirement 2.2 - Define a catalog of error codes covering
validation errors, authentication errors, external service failures,
and internal errors.
"""

from typing import Any, Optional

from errors.codes import ErrorCode, get_default_status_code


class AppException(Exception):
    """
    Base exception class for all application-specific errors.
    
    This exception provides structured error information including:
    - error_code: A standardized error code from the ErrorCode enum
    - message: A human-readable error message
    - status_code: The HTTP status code to return
    - details: Optional additional context (e.g., field-level errors)
    
    Example:
        raise AppException(
            error_code=ErrorCode.VALIDATION_ERROR,
            message="Invalid latitude value",
            status_code=400,
            details={"field": "latitude", "reason": "Must be between -90 and 90"}
        )
    """
    
    def __init__(
        self,
        error_code: ErrorCode,
        message: str,
        status_code: Optional[int] = None,
        details: Optional[dict[str, Any]] = None
    ):
        """
        Initialize an AppException.
        
        Args:
            error_code: The error code from the ErrorCode enum
            message: A human-readable error message
            status_code: The HTTP status code (defaults to the error code's default)
            details: Optional dictionary with additional error context
        """
        self.error_code = error_code
        self.message = message
        self.status_code = status_code or get_default_status_code(error_code)
        self.details = details
        super().__init__(message)
    
    def to_dict(self) -> dict[str, Any]:
        """
        Convert the exception to a dictionary for JSON serialization.
        
        Returns:
            Dictionary containing error_code, message, and details
        """
        result = {
            "error_code": self.error_code.value,
            "message": self.message,
        }
        if self.details is not None:
            result["details"] = self.details
        return result
    
    def __repr__(self) -> str:
        return (
            f"AppException(error_code={self.error_code.value!r}, "
            f"message={self.message!r}, status_code={self.status_code}, "
            f"details={self.details!r})"
        )


# Convenience factory functions for common error types

def validation_error(
    message: str,
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a validation error exception."""
    return AppException(
        error_code=ErrorCode.VALIDATION_ERROR,
        message=message,
        details=details
    )


def invalid_request(
    message: str,
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an invalid request exception."""
    return AppException(
        error_code=ErrorCode.INVALID_REQUEST,
        message=message,
        details=details
    )


def resource_not_found(
    message: str,
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a resource not found exception."""
    return AppException(
        error_code=ErrorCode.RESOURCE_NOT_FOUND,
        message=message,
        details=details
    )


def unauthorized(
    message: str = "Authentication required",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an unauthorized exception."""
    return AppException(
        error_code=ErrorCode.UNAUTHORIZED,
        message=message,
        details=details
    )


def forbidden(
    message: str = "Insufficient permissions",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a forbidden exception."""
    return AppException(
        error_code=ErrorCode.FORBIDDEN,
        message=message,
        details=details
    )


def rate_limited(
    message: str = "Too many requests",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a rate limited exception."""
    return AppException(
        error_code=ErrorCode.RATE_LIMITED,
        message=message,
        details=details
    )


def elasticsearch_unavailable(
    message: str = "Database connection failed",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an Elasticsearch unavailable exception."""
    return AppException(
        error_code=ErrorCode.ELASTICSEARCH_UNAVAILABLE,
        message=message,
        details=details
    )


def ai_service_unavailable(
    message: str = "AI service unavailable",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an AI service unavailable exception."""
    return AppException(
        error_code=ErrorCode.AI_SERVICE_UNAVAILABLE,
        message=message,
        details=details
    )


def session_store_unavailable(
    message: str = "Session store unavailable",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a session store unavailable exception."""
    return AppException(
        error_code=ErrorCode.SESSION_STORE_UNAVAILABLE,
        message=message,
        details=details
    )


def internal_error(
    message: str = "An unexpected error occurred",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an internal error exception."""
    return AppException(
        error_code=ErrorCode.INTERNAL_ERROR,
        message=message,
        details=details
    )


def circuit_open(
    message: str = "Service temporarily unavailable",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a circuit open exception."""
    return AppException(
        error_code=ErrorCode.CIRCUIT_OPEN,
        message=message,
        details=details
    )

def webhook_signature_invalid(
    message: str = "Webhook signature verification failed",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a webhook signature invalid exception."""
    return AppException(
        error_code=ErrorCode.WEBHOOK_SIGNATURE_INVALID,
        message=message,
        details=details
    )


def webhook_schema_unknown(
    message: str = "Unknown webhook schema version",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a webhook schema unknown exception."""
    return AppException(
        error_code=ErrorCode.WEBHOOK_SCHEMA_UNKNOWN,
        message=message,
        details=details
    )


def tenant_not_found(
    message: str = "Tenant not found",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a tenant not found exception."""
    return AppException(
        error_code=ErrorCode.TENANT_NOT_FOUND,
        message=message,
        details=details
    )


def tenant_disabled(
    message: str = "Ops intelligence is not enabled for this tenant",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a tenant disabled exception."""
    return AppException(
        error_code=ErrorCode.TENANT_DISABLED,
        message=message,
        details=details
    )


def poison_queue_max_retries(
    message: str = "Event exceeded maximum retry count",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a poison queue max retries exceeded exception."""
    return AppException(
        error_code=ErrorCode.POISON_QUEUE_MAX_RETRIES,
        message=message,
        details=details
    )


def drift_threshold_exceeded(
    message: str = "Source-replica drift exceeds threshold",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a drift threshold exceeded exception."""
    return AppException(
        error_code=ErrorCode.DRIFT_THRESHOLD_EXCEEDED,
        message=message,
        details=details
    )


def backfill_in_progress(
    message: str = "A backfill job is already running for this tenant",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a backfill in progress exception."""
    return AppException(
        error_code=ErrorCode.BACKFILL_IN_PROGRESS,
        message=message,
        details=details
    )


# --- Order Intake Pipeline factory functions ---


def conflict(
    message: str,
    error_code: str = "INVALID_STATUS_TRANSITION",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a conflict exception (409) using a string error code lookup.

    This is a convenience helper used by the order state machine and
    other pipeline components that raise 409 conflicts with specific
    error codes.
    """
    code = ErrorCode(error_code.upper()) if isinstance(error_code, str) else error_code
    return AppException(
        error_code=code,
        message=message,
        details=details
    )


def invalid_customer_tank_ref(
    message: str = "Referenced customer tank does not exist or belongs to another tenant",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an invalid customer tank reference exception."""
    return AppException(
        error_code=ErrorCode.INVALID_CUSTOMER_TANK_REF,
        message=message,
        details=details
    )


def missing_volume(
    message: str = "Order must specify gallons_requested > 0 or fill_to_full = true",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing volume exception."""
    return AppException(
        error_code=ErrorCode.MISSING_VOLUME,
        message=message,
        details=details
    )


def invalid_delivery_window(
    message: str = "Delivery window is invalid — end must be after start",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an invalid delivery window exception."""
    return AppException(
        error_code=ErrorCode.INVALID_DELIVERY_WINDOW,
        message=message,
        details=details
    )


def missing_delivery_window(
    message: str = "Order lacks a delivery window required for the target status",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing delivery window exception."""
    return AppException(
        error_code=ErrorCode.MISSING_DELIVERY_WINDOW,
        message=message,
        details=details
    )


def missing_product_code(
    message: str = "Non-legacy intake channel order must carry a product code",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing product code exception."""
    return AppException(
        error_code=ErrorCode.MISSING_PRODUCT_CODE,
        message=message,
        details=details
    )


def missing_client_event_id(
    message: str = "Dispatcher intake request must include a client_event_id",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing client event ID exception."""
    return AppException(
        error_code=ErrorCode.MISSING_CLIENT_EVENT_ID,
        message=message,
        details=details
    )


def missing_hold_reason(
    message: str = "Order placed on hold must include a hold_reason",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing hold reason exception."""
    return AppException(
        error_code=ErrorCode.MISSING_HOLD_REASON,
        message=message,
        details=details
    )


def invalid_status_transition(
    message: str = "Requested status transition is not allowed",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an invalid status transition exception."""
    return AppException(
        error_code=ErrorCode.INVALID_STATUS_TRANSITION,
        message=message,
        details=details
    )


def channel_disabled(
    message: str = "Intake channel is disabled and cannot accept orders",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a channel disabled exception."""
    return AppException(
        error_code=ErrorCode.CHANNEL_DISABLED,
        message=message,
        details=details
    )


def insufficient_role(
    message: str = "Caller lacks the required role for this operation",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an insufficient role exception."""
    return AppException(
        error_code=ErrorCode.INSUFFICIENT_ROLE,
        message=message,
        details=details
    )


def driver_unavailable(
    message: str = "Driver is off_duty or inactive and cannot be assigned",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a driver unavailable exception."""
    return AppException(
        error_code=ErrorCode.DRIVER_UNAVAILABLE,
        message=message,
        details=details
    )


def legacy_route_sunset(
    message: str = "This legacy route has been sunset — use the new order surface",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a legacy route sunset exception."""
    return AppException(
        error_code=ErrorCode.LEGACY_ROUTE_SUNSET,
        message=message,
        details=details
    )


def security_tenant_id_mismatch(
    message: str = "Payload tenant_id does not match the channel's tenant_id",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a security tenant ID mismatch exception."""
    return AppException(
        error_code=ErrorCode.SECURITY_TENANT_ID_MISMATCH,
        message=message,
        details=details
    )


# --- Dinee Voice Integration factory functions ---
#
# These envelopes back Surface A (voice order submission) and Surface B
# (read/driver endpoints). Per Requirements 9.3 and 10.6, rejection
# envelopes MUST NOT carry tenant data or credential values: the factories
# below take fixed, non-sensitive default messages and only ever attach
# caller-provided `details` that the call sites keep free of tenant
# identifiers, API keys, HMAC secrets, or signatures.


def voice_replay_window_exceeded(
    message: str = "Request timestamp is missing, invalid, or outside the allowed replay window",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a voice replay-window exceeded exception (HTTP 401)."""
    return AppException(
        error_code=ErrorCode.VOICE_REPLAY_WINDOW_EXCEEDED,
        message=message,
        details=details
    )


def missing_idempotency_key(
    message: str = "The X-Idempotency-Key header is required",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a missing idempotency key exception (HTTP 400)."""
    return AppException(
        error_code=ErrorCode.MISSING_IDEMPOTENCY_KEY,
        message=message,
        details=details
    )


def idempotency_conflict(
    message: str = "The idempotency key was reused with a different request body",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an idempotency conflict exception (HTTP 409)."""
    return AppException(
        error_code=ErrorCode.IDEMPOTENCY_CONFLICT,
        message=message,
        details=details
    )


def unsupported_schema_version(
    message: str = "The X-Schema-Version header is missing or unsupported",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create an unsupported schema version exception (HTTP 422)."""
    return AppException(
        error_code=ErrorCode.UNSUPPORTED_SCHEMA_VERSION,
        message=message,
        details=details
    )


def voice_payload_invalid(
    missing_fields: Optional[list[str]] = None,
    message: str = "The voice submission payload is missing required fields",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a voice payload validation exception (HTTP 422).

    The response body identifies the absent required fields via
    ``details.missing_fields`` (Requirement 7.3). When ``details`` is not
    supplied, it is built from ``missing_fields``; the field names are the
    only context carried and never include tenant data or credentials.
    """
    if details is None:
        details = {"missing_fields": list(missing_fields or [])}
    return AppException(
        error_code=ErrorCode.VOICE_PAYLOAD_INVALID,
        message=message,
        details=details
    )


def voice_unauthorized(
    message: str = "Authentication required",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a voice unauthorized exception (HTTP 401)."""
    return AppException(
        error_code=ErrorCode.VOICE_UNAUTHORIZED,
        message=message,
        details=details
    )


def voice_tenant_mismatch(
    message: str = "The tenant header does not match the authenticated credential",
    details: Optional[dict[str, Any]] = None
) -> AppException:
    """Create a voice tenant mismatch exception (HTTP 403)."""
    return AppException(
        error_code=ErrorCode.VOICE_TENANT_MISMATCH,
        message=message,
        details=details
    )
