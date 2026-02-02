"""
Request ID middleware for request correlation.

This middleware generates or extracts a unique request ID for each incoming
request, enabling request tracing across logs and error responses.

Validates: Requirement 5.2 - Generate a unique request_id and include it
in all log entries for that request.
"""

import uuid
from contextvars import ContextVar
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

# Context variable for storing request_id across async contexts
# This allows access to the request_id from anywhere in the request lifecycle
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# Header name for request ID
REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Middleware that generates or extracts a request ID for each request.
    
    The request ID is:
    1. Extracted from the X-Request-ID header if present
    2. Generated as a new UUID if not present
    3. Stored in request.state for use by error handlers
    4. Stored in a context variable for use by logging
    5. Added to the response headers
    
    This enables end-to-end request tracing and correlation of logs
    with specific requests.
    """
    
    def __init__(self, app: ASGIApp):
        """
        Initialize the middleware.
        
        Args:
            app: The ASGI application to wrap
        """
        super().__init__(app)
    
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Response]
    ) -> Response:
        """
        Process the request and add request ID correlation.
        
        Args:
            request: The incoming FastAPI request
            call_next: The next middleware or route handler
            
        Returns:
            The response with X-Request-ID header added
        """
        # Step 1: Check for existing X-Request-ID header
        request_id = request.headers.get(REQUEST_ID_HEADER)
        
        # Step 2: Generate new UUID if not present
        if not request_id:
            request_id = str(uuid.uuid4())
        
        # Step 3: Store in request state (for error handlers)
        request.state.request_id = request_id
        
        # Step 4: Store in context variable (for logging)
        token = request_id_var.set(request_id)
        
        try:
            # Process the request
            response = await call_next(request)
            
            # Step 5: Add request_id to response headers
            response.headers[REQUEST_ID_HEADER] = request_id
            
            return response
        finally:
            # Reset the context variable to avoid leaking between requests
            request_id_var.reset(token)


def get_request_id() -> str:
    """
    Get the current request ID from the context variable.
    
    This function can be called from anywhere in the request lifecycle
    to get the current request's ID for logging or other purposes.
    
    Returns:
        The current request ID, or empty string if not in a request context
    """
    return request_id_var.get()
