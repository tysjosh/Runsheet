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
from http import HTTPStatus
from typing import Any, Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from errors.codes import ErrorCode
from errors.exceptions import AppException, error_code_value
from schemas.common import ErrorResponse

logger = logging.getLogger(__name__)


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


#: How far down ``__cause__``/``__context__`` the query-value check looks.
_CHAIN_DEPTH = 10


def _query_value_error_in_chain(exc: BaseException) -> Optional[BaseException]:
    """The ``InvalidQueryValueError`` at or under ``exc``, if any (N5).

    Endpoints commonly wrap service calls in ``except Exception: raise
    internal_error(...)``, which turns a client's bad filter value into a 500.
    Walking the chain lets those endpoints answer 400 without each one being
    changed. Cycle-safe and bounded to :data:`_CHAIN_DEPTH` links.
    """
    # Imported lazily: persistence imports SQLAlchemy and must not load with
    # the error package.
    from persistence.document_query import InvalidQueryValueError

    seen: set = set()
    current: Optional[BaseException] = exc
    for _ in range(_CHAIN_DEPTH):
        if current is None or id(current) in seen:
            return None
        if isinstance(current, InvalidQueryValueError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _query_value_error_response(
    request: Request, request_id: str, found: BaseException
) -> JSONResponse:
    """400 VALIDATION_ERROR for a control character in a filter value."""
    logger.warning(
        "Rejected query value with a control character",
        extra={
            "error_code": ErrorCode.VALIDATION_ERROR.value,
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
        },
    )
    error_response = ErrorResponse(
        error_code=ErrorCode.VALIDATION_ERROR.value,
        # The fixed, safe message; the offending value is never echoed.
        message=getattr(found, "safe_message", "Invalid filter value."),
        details={"reason": getattr(found, "reason", "control_character")},
        request_id=request_id,
    )
    return JSONResponse(
        status_code=400,
        content=error_response.model_dump(exclude_none=True),
    )


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

    if exc.status_code >= 500:
        found = _query_value_error_in_chain(exc)
        if found is not None:
            return _query_value_error_response(request, request_id, found)
    
    # Log the error with context
    logger.warning(
        "Application error occurred",
        extra={
            "error_code": error_code_value(exc.error_code),
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
        error_code=error_code_value(exc.error_code),
        message=exc.message,
        details=exc.details,
        request_id=request_id,
    )
    
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response.model_dump(exclude_none=True),
        headers=getattr(exc, "headers", None),
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

    found = _query_value_error_in_chain(exc)
    if found is not None:
        return _query_value_error_response(request, request_id, found)
    
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


#: Error code for a framework or raw ``HTTPException`` whose detail carries none.
_HTTP_STATUS_ERROR_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.VALIDATION_ERROR,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.RESOURCE_NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    429: ErrorCode.RATE_LIMITED,
}

_GENERIC_5XX_MESSAGE = "An unexpected error occurred. Please try again later."


def _status_phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Request failed"


def _status_error_code(status_code: int) -> ErrorCode:
    if status_code in _HTTP_STATUS_ERROR_CODES:
        return _HTTP_STATUS_ERROR_CODES[status_code]
    return ErrorCode.INTERNAL_ERROR if status_code >= 500 else ErrorCode.VALIDATION_ERROR


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    """Render a Starlette/FastAPI ``HTTPException`` as the standard envelope (OI-35).

    Covers the framework's own 404/405 and the raw ``raise HTTPException``
    sites not yet migrated to ``AppException``. The status code and headers
    are kept. A dict ``detail`` carrying ``error_code`` keeps that code and its
    ``message``/``details``; any other detail becomes the message. A 5xx never
    echoes its detail.
    """
    request_id = get_request_id(request)
    status_code = exc.status_code
    headers = getattr(exc, "headers", None)

    # 204/304 carry no body, matching Starlette's default handler.
    if status_code in (204, 304):
        return Response(status_code=status_code, headers=headers)

    detail = exc.detail
    error_code: str = _status_error_code(status_code).value
    message: str
    details: Optional[dict[str, Any]] = None
    if isinstance(detail, dict) and detail.get("error_code"):
        error_code = str(detail["error_code"])
        message = str(detail.get("message") or _status_phrase(status_code))
        if isinstance(detail.get("details"), dict):
            details = detail["details"]
        else:
            extra = {k: v for k, v in detail.items() if k not in ("error_code", "message")}
            details = extra or None
    else:
        message = str(detail) if detail else _status_phrase(status_code)

    if status_code >= 500:
        message = _GENERIC_5XX_MESSAGE
        details = None

    logger.warning(
        "HTTP error response",
        extra={
            "error_code": error_code,
            "status_code": status_code,
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
        },
    )
    error_response = ErrorResponse(
        error_code=error_code,
        message=message,
        details=details,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=error_response.model_dump(exclude_none=True),
        headers=headers,
    )


async def handle_request_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 ``VALIDATION_ERROR`` envelope for a request that fails its schema (OI-35).

    Only ``loc``, ``msg`` and ``type`` are kept per error. FastAPI's default
    body also carries ``input`` (for a missing field, the whole request body,
    which can hold a PIN or a credential), ``ctx`` and ``url``; none of those
    is returned.
    """
    request_id = get_request_id(request)
    errors = [
        {
            "loc": [str(part) if not isinstance(part, int) else part for part in err.get("loc", ())],
            "msg": str(err.get("msg", "")),
            "type": str(err.get("type", "")),
        }
        for err in exc.errors()
    ]
    logger.warning(
        "Request validation failed",
        extra={
            "error_code": ErrorCode.VALIDATION_ERROR.value,
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
            "error_count": len(errors),
        },
    )
    error_response = ErrorResponse(
        error_code=ErrorCode.VALIDATION_ERROR.value,
        message="Request validation failed",
        details={"errors": errors},
        request_id=request_id,
    )
    return JSONResponse(
        status_code=422,
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

    # Framework 404/405, raw HTTPExceptions and request-schema 422s use the
    # same envelope (OI-35).
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
    
    # Register handler for all other unexpected exceptions
    # Note: This catches Exception, which is the base class for most errors
    app.add_exception_handler(Exception, handle_unexpected_exception)
    
    logger.info("Exception handlers registered successfully")
