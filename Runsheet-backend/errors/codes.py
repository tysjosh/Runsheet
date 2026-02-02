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
