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
