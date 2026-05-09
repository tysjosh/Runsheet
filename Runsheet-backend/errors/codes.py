"""
Error code catalog for the Runsheet backend.

This module defines all error codes used throughout the application,
covering validation errors, authentication errors, external service
failures, and internal errors.

Validates: Requirement 2.2 - Define a catalog of error codes covering
validation errors, authentication errors, external service failures,
and internal errors.
"""

from enum import Enum


class ErrorCode(str, Enum):
    """
    Enumeration of all error codes used in the application.
    
    Each error code maps to a specific HTTP status code and error category:
    - Validation errors (4xx): Client request issues
    - Authentication errors (4xx): Auth/authz failures
    - External service errors (5xx): Dependency failures
    - Internal errors (5xx): Server-side issues
    """
    
    # Validation errors (4xx)
    VALIDATION_ERROR = "VALIDATION_ERROR"
    """Request payload validation failed (HTTP 400)"""
    
    INVALID_REQUEST = "INVALID_REQUEST"
    """Malformed request structure (HTTP 400)"""
    
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    """Requested resource does not exist (HTTP 404)"""
    
    # Authentication errors (4xx)
    UNAUTHORIZED = "UNAUTHORIZED"
    """Authentication required (HTTP 401)"""
    
    FORBIDDEN = "FORBIDDEN"
    """Insufficient permissions (HTTP 403)"""
    
    RATE_LIMITED = "RATE_LIMITED"
    """Too many requests (HTTP 429)"""
    
    # External service errors (5xx)
    ELASTICSEARCH_UNAVAILABLE = "ELASTICSEARCH_UNAVAILABLE"
    """Database connection failed (HTTP 503)"""
    
    AI_SERVICE_UNAVAILABLE = "AI_SERVICE_UNAVAILABLE"
    """Gemini API unavailable (HTTP 503)"""
    
    SESSION_STORE_UNAVAILABLE = "SESSION_STORE_UNAVAILABLE"
    """Redis/DynamoDB unavailable (HTTP 503)"""
    
    # Ops Intelligence errors (4xx)
    WEBHOOK_SIGNATURE_INVALID = "WEBHOOK_SIGNATURE_INVALID"
    """Webhook HMAC-SHA256 signature verification failed (HTTP 401)"""
    
    WEBHOOK_SCHEMA_UNKNOWN = "WEBHOOK_SCHEMA_UNKNOWN"
    """Webhook payload contains unrecognized schema version (HTTP 200, routed to poison queue)"""
    
    TENANT_NOT_FOUND = "TENANT_NOT_FOUND"
    """Tenant does not exist (HTTP 404)"""
    
    TENANT_DISABLED = "TENANT_DISABLED"
    """Tenant ops intelligence is disabled via feature flag (HTTP 404)"""
    
    # Ops Intelligence errors (5xx / operational)
    POISON_QUEUE_MAX_RETRIES = "POISON_QUEUE_MAX_RETRIES"
    """Poison queue event exceeded maximum retry count (HTTP 422)"""
    
    DRIFT_THRESHOLD_EXCEEDED = "DRIFT_THRESHOLD_EXCEEDED"
    """Source-replica drift exceeds configured threshold (HTTP 409)"""
    
    BACKFILL_IN_PROGRESS = "BACKFILL_IN_PROGRESS"
    """A backfill job is already running for this tenant (HTTP 409)"""
    
    # Order Intake Pipeline errors (4xx / conflict)
    INVALID_CUSTOMER_TANK_REF = "INVALID_CUSTOMER_TANK_REF"
    """Referenced customer tank does not exist or belongs to another tenant (HTTP 400)"""

    MISSING_VOLUME = "MISSING_VOLUME"
    """Order has no gallons_requested and fill_to_full is false (HTTP 400)"""

    INVALID_DELIVERY_WINDOW = "INVALID_DELIVERY_WINDOW"
    """Delivery window is invalid — end is before or equal to start (HTTP 400)"""

    MISSING_DELIVERY_WINDOW = "MISSING_DELIVERY_WINDOW"
    """Order lacks a delivery window required for the target status (HTTP 409)"""

    MISSING_PRODUCT_CODE = "MISSING_PRODUCT_CODE"
    """Non-legacy intake channel order is missing a product code (HTTP 400)"""

    MISSING_CLIENT_EVENT_ID = "MISSING_CLIENT_EVENT_ID"
    """Dispatcher intake request is missing the required client_event_id (HTTP 400)"""

    MISSING_HOLD_REASON = "MISSING_HOLD_REASON"
    """Order placed on hold without a hold_reason (HTTP 400)"""

    INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"
    """Requested status transition is not allowed by the order state machine (HTTP 409)"""

    CHANNEL_DISABLED = "CHANNEL_DISABLED"
    """Intake channel is disabled and cannot accept orders (HTTP 403)"""

    INSUFFICIENT_ROLE = "INSUFFICIENT_ROLE"
    """Caller lacks the required role for this operation (HTTP 403)"""

    DRIVER_UNAVAILABLE = "DRIVER_UNAVAILABLE"
    """Driver is off_duty or inactive and cannot be assigned (HTTP 409)"""

    LEGACY_ROUTE_SUNSET = "LEGACY_ROUTE_SUNSET"
    """Legacy route has been sunset and is no longer available (HTTP 410)"""

    SECURITY_TENANT_ID_MISMATCH = "SECURITY_TENANT_ID_MISMATCH"
    """Payload tenant_id does not match the channel's tenant_id (HTTP 403)"""

    # Internal errors (5xx)
    INTERNAL_ERROR = "INTERNAL_ERROR"
    """Unexpected server error (HTTP 500)"""
    
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    """Circuit breaker is open (HTTP 503)"""


# Mapping of error codes to their default HTTP status codes
ERROR_CODE_STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.ELASTICSEARCH_UNAVAILABLE: 503,
    ErrorCode.AI_SERVICE_UNAVAILABLE: 503,
    ErrorCode.SESSION_STORE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.CIRCUIT_OPEN: 503,
    # Ops Intelligence error codes
    ErrorCode.WEBHOOK_SIGNATURE_INVALID: 401,
    ErrorCode.WEBHOOK_SCHEMA_UNKNOWN: 200,
    ErrorCode.TENANT_NOT_FOUND: 404,
    ErrorCode.TENANT_DISABLED: 404,
    ErrorCode.POISON_QUEUE_MAX_RETRIES: 422,
    ErrorCode.DRIFT_THRESHOLD_EXCEEDED: 409,
    ErrorCode.BACKFILL_IN_PROGRESS: 409,
    # Order Intake Pipeline error codes
    ErrorCode.INVALID_CUSTOMER_TANK_REF: 400,
    ErrorCode.MISSING_VOLUME: 400,
    ErrorCode.INVALID_DELIVERY_WINDOW: 400,
    ErrorCode.MISSING_DELIVERY_WINDOW: 409,
    ErrorCode.MISSING_PRODUCT_CODE: 400,
    ErrorCode.MISSING_CLIENT_EVENT_ID: 400,
    ErrorCode.MISSING_HOLD_REASON: 400,
    ErrorCode.INVALID_STATUS_TRANSITION: 409,
    ErrorCode.CHANNEL_DISABLED: 403,
    ErrorCode.INSUFFICIENT_ROLE: 403,
    ErrorCode.DRIVER_UNAVAILABLE: 409,
    ErrorCode.LEGACY_ROUTE_SUNSET: 410,
    ErrorCode.SECURITY_TENANT_ID_MISMATCH: 403,
}


def get_default_status_code(error_code: ErrorCode) -> int:
    """
    Get the default HTTP status code for an error code.
    
    Args:
        error_code: The error code to look up
        
    Returns:
        The default HTTP status code for the error code
    """
    return ERROR_CODE_STATUS_MAP.get(error_code, 500)
