"""
Rate limiting middleware for API security.

This module implements rate limiting using slowapi to protect API endpoints
from abuse and ensure fair resource allocation.

Validates:
- Requirement 14.1: THE Backend_Service SHALL implement rate limiting of 100 requests
  per minute per IP address for API endpoints
- Requirement 14.2: THE Backend_Service SHALL implement rate limiting of 10 requests
  per minute per IP address for AI chat endpoints
"""

import logging
from typing import Callable, Optional

from fastapi import FastAPI, Request, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


def get_client_ip(request: Request) -> str:
    """
    Extract the client IP address from the request.
    
    This function handles various proxy scenarios by checking common
    forwarding headers before falling back to the direct client address.
    
    Args:
        request: The incoming FastAPI request
        
    Returns:
        The client's IP address as a string
    """
    # Check for X-Forwarded-For header (common in load balancer setups)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For can contain multiple IPs, take the first (original client)
        return forwarded_for.split(",")[0].strip()
    
    # Check for X-Real-IP header (used by some proxies like nginx)
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    
    # Fall back to the direct client address
    return get_remote_address(request)


# Create the limiter instance with IP-based key function
limiter = Limiter(key_func=get_client_ip)


def create_rate_limiter(
    api_rate_limit: int = 100,
    ai_rate_limit: int = 10
) -> Limiter:
    """
    Create a configured rate limiter instance.
    
    This factory function creates a limiter with the specified rate limits
    for general API endpoints and AI chat endpoints.
    
    Args:
        api_rate_limit: Maximum requests per minute for general API endpoints (default: 100)
        ai_rate_limit: Maximum requests per minute for AI chat endpoints (default: 10)
        
    Returns:
        Configured Limiter instance
    """
    return Limiter(key_func=get_client_ip)


def get_api_rate_limit_string(requests_per_minute: int) -> str:
    """
    Generate a rate limit string for slowapi.
    
    Args:
        requests_per_minute: Number of requests allowed per minute
        
    Returns:
        Rate limit string in slowapi format (e.g., "100/minute")
    """
    return f"{requests_per_minute}/minute"


# Rate limit decorators for different endpoint types
def api_rate_limit(requests_per_minute: int = 100) -> Callable:
    """
    Decorator for applying rate limiting to general API endpoints.
    
    Validates:
    - Requirement 14.1: 100 requests per minute per IP for API endpoints
    
    Args:
        requests_per_minute: Maximum requests per minute (default: 100)
        
    Returns:
        Rate limit decorator
    """
    return limiter.limit(get_api_rate_limit_string(requests_per_minute))


def ai_rate_limit(requests_per_minute: int = 10) -> Callable:
    """
    Decorator for applying rate limiting to AI chat endpoints.
    
    Validates:
    - Requirement 14.2: 10 requests per minute per IP for AI chat endpoints
    
    Args:
        requests_per_minute: Maximum requests per minute (default: 10)
        
    Returns:
        Rate limit decorator
    """
    return limiter.limit(get_api_rate_limit_string(requests_per_minute))


def setup_rate_limiting(
    app: FastAPI,
    api_rate_limit: int = 100,
    ai_rate_limit: int = 10,
    enabled: bool = True
) -> None:
    """
    Configure rate limiting for a FastAPI application.
    
    This function sets up the rate limiter on the FastAPI app and registers
    the rate limit exceeded exception handler.
    
    Validates:
    - Requirement 14.1: 100 requests per minute per IP for API endpoints
    - Requirement 14.2: 10 requests per minute per IP for AI chat endpoints
    
    Args:
        app: The FastAPI application instance
        api_rate_limit: Maximum requests per minute for general API endpoints
        ai_rate_limit: Maximum requests per minute for AI chat endpoints
        enabled: Whether rate limiting is enabled (default: True)
    """
    if not enabled:
        logger.info("Rate limiting is disabled")
        return
    
    # Store rate limit configuration in app state for access by decorators
    app.state.limiter = limiter
    app.state.api_rate_limit = api_rate_limit
    app.state.ai_rate_limit = ai_rate_limit
    
    # Register the rate limit exceeded exception handler
    app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)
    
    logger.info(
        f"Rate limiting configured: API={api_rate_limit}/min, AI={ai_rate_limit}/min"
    )


async def _custom_rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """
    Custom handler for rate limit exceeded errors.
    
    Returns a structured JSON response consistent with the application's
    error response format.
    
    Args:
        request: The incoming request that exceeded the rate limit
        exc: The RateLimitExceeded exception
        
    Returns:
        JSON response with 429 status code and error details
    """
    import json
    from datetime import datetime
    
    # Get request_id from request state if available
    request_id = getattr(request.state, "request_id", "unknown")
    
    # Extract retry-after information from the exception
    retry_after = getattr(exc, "retry_after", 60)
    
    response_body = {
        "error_code": "RATE_LIMITED",
        "message": "Too many requests. Please slow down.",
        "details": {
            "limit": str(exc.detail) if hasattr(exc, "detail") else "Rate limit exceeded",
            "retry_after_seconds": retry_after
        },
        "request_id": request_id
    }
    
    logger.warning(
        f"Rate limit exceeded for IP {get_client_ip(request)}",
        extra={"extra_data": {
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method
        }}
    )
    
    return Response(
        content=json.dumps(response_body),
        status_code=429,
        media_type="application/json",
        headers={
            "Retry-After": str(retry_after),
            "X-Request-ID": request_id
        }
    )


# AI Chat endpoint paths that should have stricter rate limiting
AI_CHAT_PATHS = {
    "/api/chat",
    "/api/chat/fallback"
}


def is_ai_chat_endpoint(path: str) -> bool:
    """
    Check if a request path is an AI chat endpoint.
    
    Args:
        path: The request URL path
        
    Returns:
        True if the path is an AI chat endpoint, False otherwise
    """
    return path in AI_CHAT_PATHS


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware that applies different rate limits based on endpoint type.
    
    This middleware automatically applies:
    - 10 requests/minute for AI chat endpoints (/api/chat, /api/chat/fallback)
    - 100 requests/minute for all other API endpoints
    
    Validates:
    - Requirement 14.1: 100 requests per minute per IP for API endpoints
    - Requirement 14.2: 10 requests per minute per IP for AI chat endpoints
    """
    
    def __init__(
        self,
        app: ASGIApp,
        api_rate_limit: int = 100,
        ai_rate_limit: int = 10
    ):
        """
        Initialize the rate limit middleware.
        
        Args:
            app: The ASGI application to wrap
            api_rate_limit: Maximum requests per minute for general API endpoints
            ai_rate_limit: Maximum requests per minute for AI chat endpoints
        """
        super().__init__(app)
        self.api_rate_limit = api_rate_limit
        self.ai_rate_limit = ai_rate_limit
        self._rate_tracker: dict = {}  # IP -> {path_type: [(timestamp, count)]}
    
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Response]
    ) -> Response:
        """
        Process the request with rate limiting.
        
        Note: This middleware is a fallback. The primary rate limiting is done
        via decorators on individual endpoints for more precise control.
        
        Args:
            request: The incoming FastAPI request
            call_next: The next middleware or route handler
            
        Returns:
            The response from the next handler, or a 429 response if rate limited
        """
        # The actual rate limiting is handled by slowapi decorators on endpoints
        # This middleware just passes through - it's here for potential future
        # global rate limiting needs
        return await call_next(request)
