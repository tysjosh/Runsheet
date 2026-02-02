"""
Exception handlers for the Runsheet backend.

This module provides FastAPI exception handlers that convert exceptions
to structured JSON error responses with consistent format.

Validates: Requirement 2.1 - Return a structured JSON response containing
error_code, message, details, and request_id fields.

Validates: Requirement 2.3 - Log the full stack trace and return a generic
error response without exposing internal details.
"""

import logging
import traceback
import uuid
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from errors.codes import ErrorCode
from errors.exceptions import AppException

logger = logging.getLogger(__name__)


class ErrorResponse(BaseModel):
    """
    Structured error response model.
    
    All error responses from the API follow this format for consistency
    and to enable programmatic error handling by clients.
    """
    error_code: str
    message: str
    details: Optional[dict[str, Any]] = None
    request_id: str


def get_request_id(request: Request) -> str:
    """
    Get the request ID from the request state or generate a new one.
    
    The request_id middleware (Task 2.3) will set this value.
    For now, we check if it exists and generate a UUID if not.
    
    Args:
        request: The FastAPI request object
        
    Returns:
        The request ID string
    """
    # Try to get request_id from request state (set by middleware in Task 2.3)
    if hasattr(request.state, "request_id"):
        return request.state.request_id
    
    # Fallback: generate a new UUID if middleware hasn't set it
    return str(uuid.uuid4())


async def handle_app_exception(request: Request, exc: AppException) -> JSONResponse:
    """
    Handle known application exceptions and convert to structured response.
    
    This handler processes AppException instances, which represent expected
    error conditions with proper error codes and messages.
    
    Args:
        request: The FastAPI request object
        exc: The AppException that was raised
        
    Returns:
        JSONResponse with structured error format
        
    Validates: Requirement 2.1 - Return structured JSON response with
    error_code, message, details, and request_id fields.
    """
    request_id = get_request_id(request)
    
    # Log the error with context
    logger.warning(
        "Application error occurred",
        extra={
            "error_code": exc.error_code.value,
            "error_message": exc.message,
            "status_code": exc.status_code,
            "details": exc.details,
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
        }
    )
    
    # Build the error response
    error_response = ErrorResponse(
        error_code=exc.error_code.value,
        message=exc.message,
        details=exc.details,
        request_id=request_id,
    )
    
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response.model_dump(exclude_none=True),
    )


async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    """
    Handle unexpected exceptions safely without exposing internal details.
    
    This handler catches all unhandled exceptions, logs the full stack trace
    for debugging, and returns a generic error response to the client.
    
    Args:
        request: The FastAPI request object
        exc: The unexpected exception that was raised
        
    Returns:
        JSONResponse with generic error message (no internal details exposed)
        
    Validates: Requirement 2.3 - Log the full stack trace and return a
    generic error response without exposing internal details.
    """
    request_id = get_request_id(request)
    
    # Log the full stack trace for debugging
    logger.error(
        "Unexpected error occurred",
        extra={
            "error_code": ErrorCode.INTERNAL_ERROR.value,
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "stack_trace": traceback.format_exc(),
        },
        exc_info=True,
    )
    
    # Return a generic error response without exposing internal details
    error_response = ErrorResponse(
        error_code=ErrorCode.INTERNAL_ERROR.value,
        message="An unexpected error occurred. Please try again later.",
        details=None,  # Never expose internal details
        request_id=request_id,
    )
    
    return JSONResponse(
        status_code=500,
        content=error_response.model_dump(exclude_none=True),
    )


def register_exception_handlers(app) -> None:
    """
    Register all exception handlers with the FastAPI application.
    
    This function should be called during application startup to ensure
    all exceptions are handled consistently.
    
    Args:
        app: The FastAPI application instance
    """
    # Register handler for known application exceptions
    app.add_exception_handler(AppException, handle_app_exception)
    
    # Register handler for all other unexpected exceptions
    # Note: This catches Exception, which is the base class for most errors
    app.add_exception_handler(Exception, handle_unexpected_exception)
    
    logger.info("Exception handlers registered successfully")
