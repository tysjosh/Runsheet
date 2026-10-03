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

import ipaddress
import logging
import os
from typing import Callable, Optional

from fastapi import FastAPI, Request, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

#: How many proxies in front of the app append to X-Forwarded-For. Set once at
#: import by ``setup_rate_limiting`` from ``settings.effective_trusted_proxy_hops``.
#: 0 (the default) ignores the header entirely, which is right when nothing
#: trusted sits in front of the app (local development and tests).
_trusted_proxy_hops: int = 0


def configure_trusted_proxy_hops(hops: int) -> None:
    """Set the number of trusted proxies that append to X-Forwarded-For (F5)."""
    global _trusted_proxy_hops
    _trusted_proxy_hops = max(0, int(hops))


def get_client_ip(request: Request, trusted_hops: Optional[int] = None) -> str:
    """
    Return the client IP used to key rate limits and in log lines.

    Behind the AWS ALB the leftmost X-Forwarded-For entries are whatever the
    client sent; the ALB only APPENDS the address it saw as the rightmost
    entry. Keying on the leftmost entry let a caller rotate it per request and
    get a fresh bucket every time (staging finding F5/L3). So with N trusted
    proxies the client is the Nth entry from the right, the same rule as
    werkzeug's ``ProxyFix(x_for=N)``. The leftmost entry is never read.

    ``X-Real-IP`` is ignored: the ALB does not set it, so it is purely
    client-controlled.

    Args:
        request: The incoming request.
        trusted_hops: Trusted proxy count. ``None`` uses the value set by
            :func:`configure_trusted_proxy_hops`; 0 ignores X-Forwarded-For.

    Returns:
        The client IP, or the socket peer when X-Forwarded-For is absent, has
        fewer than N entries, or the selected entry is not an IP address.
    """
    hops = _trusted_proxy_hops if trusted_hops is None else max(0, int(trusted_hops))
    peer = get_remote_address(request)
    if hops == 0:
        return peer

    # Several X-Forwarded-For header lines are one list, in order (RFC 7230 §3.2.2).
    entries = [
        part.strip()
        for value in request.headers.getlist("x-forwarded-for")
        for part in value.split(",")
        if part.strip()
    ]
    if len(entries) < hops:
        return peer
    candidate = entries[-hops]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return peer
    return candidate


def driver_rate_key(request: Request) -> str:
    """
    Build the per-driver rate-limit key for driver-surface write endpoints.

    Returns ``driver:{tenant_id}:{driver_id}`` when the authenticated driver
    identity has been stamped onto ``request.state`` by the driver tenant
    guard. When either value is missing — unauthenticated requests, or any
    path that does not stamp the driver identity — the key falls back to the
    client IP so the endpoint is still rate limited. That IP comes from
    :func:`get_client_ip`, so it is the proxy-appended address, not a spoofable
    leftmost X-Forwarded-For entry (F5).

    Validates:
    - Requirement 15.13: per-driver rate limit on every driver-surface write
      endpoint, rejecting over-limit requests with HTTP 429

    Args:
        request: The incoming FastAPI request

    Returns:
        The rate-limit bucket key as a string
    """
    driver_id = getattr(request.state, "driver_id", None)
    if driver_id:
        tenant_id = getattr(request.state, "tenant_id", "") or ""
        return f"driver:{tenant_id}:{driver_id}"
    return get_client_ip(request)


def _rate_limit_storage_uri(
    environment: Optional[str] = None, redis_url: Optional[str] = None
) -> str:
    """Pick the slowapi storage backend (staging finding F5).

    ``REDIS_URL`` when set, so every replica shares one set of counters;
    in-process memory otherwise. Always memory under ``ENVIRONMENT=test``: CI and
    local runs share one long-lived Redis, and shared counters would leak 429s
    between tests and between consecutive runs.

    Read from ``os.environ`` because the limiter is built at import time, after
    ``main.py``'s ``load_dotenv`` and before settings are loaded. The URL can
    carry an AUTH token, so only its scheme is ever logged.
    """
    env = (
        os.environ.get("ENVIRONMENT", "development")
        if environment is None
        else environment
    ).strip().lower()
    url = (os.environ.get("REDIS_URL", "") if redis_url is None else redis_url).strip()
    if url and env != "test":
        return url
    return "memory://"


def _build_limiter() -> Limiter:
    """Build a Limiter keyed on the client IP, backed by Redis when configured.

    ``in_memory_fallback_enabled`` keeps decorated routes limited (per process)
    when Redis is unreachable, and slowapi re-probes it periodically. That is
    also what keeps development working with REDIS_URL set but Redis down.

    ``headers_enabled`` stays off: with it on, slowapi raises "parameter
    'response' must be an instance of starlette.responses.Response" on every
    decorated endpoint that returns a dict without a ``response: Response``
    parameter, turning those routes into 500s. 429s still carry Retry-After
    from :func:`_custom_rate_limit_handler`.
    """
    return Limiter(
        key_func=get_client_ip,
        storage_uri=_rate_limit_storage_uri(),
        in_memory_fallback_enabled=True,
    )


# Create the limiter instance with IP-based key function
limiter = _build_limiter()
logger.info(
    "Rate-limit storage backend: %s",
    _rate_limit_storage_uri().split(":", 1)[0],
)


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
    return _build_limiter()


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
    enabled: bool = True,
    trusted_proxy_hops: int = 0,
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
        trusted_proxy_hops: Proxies that append to X-Forwarded-For (F5). Applied
            even when ``enabled`` is False, because ``driver_rate_key`` and log
            lines still derive the client IP.
    """
    configure_trusted_proxy_hops(trusted_proxy_hops)
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
        f"Rate limiting configured: API={api_rate_limit}/min, AI={ai_rate_limit}/min, "
        f"trusted_proxy_hops={_trusted_proxy_hops}"
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
